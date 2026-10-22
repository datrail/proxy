"""The ext_proc adapter: Envoy's `Process` stream, answered for one request.

Envoy's ext_proc filter opens one stream per request and sends its parts as
they come, each waiting for an answer. The request's headers are judged and
decided on; the answer sets and removes headers, or refuses the request with
`403`. When the judge needs the body, it is read in the body mode Envoy is
configured with, which the first message names:

- `BUFFERED`, `BUFFERED_PARTIAL`, and `STREAMED` without
  `send_body_without_waiting_for_header_response`: the headers are answered at
  once, and the body's parts held until the verdict. Envoy sends nothing
  upstream, headers included, until the body's parts are answered.
- `STREAMED` with it, and `FULL_DUPLEX_STREAMED`: the headers' answer is held
  too, so nothing goes upstream before the verdict.
- `NONE`: no body will come, so it is judged as not sent.

When trailers follow a body, no part of it ends the stream, and Envoy sends
the trailers only once the body's parts are answered. `BUFFERED` sends the
whole body in one part, so it's judged as it comes; `BUFFERED_PARTIAL` marks
it as it marks a body cut short, so it's judged as truncated; in `STREAMED`
nothing says the body ended, so the request waits until Envoy gives up.
`FULL_DUPLEX_STREAMED` sends the trailers without waiting, and they end it.

Every other part is answered unchanged, so a stream never stalls.
"""

from envoy.config.core.v3.base_pb2 import HeaderValue, HeaderValueOption
from envoy.extensions.filters.http.ext_proc.v3.processing_mode_pb2 import (
    ProcessingMode,
)
from envoy.service.ext_proc.v3 import (
    external_processor_pb2,
    external_processor_pb2_grpc,
)
from envoy.type.v3 import http_status_pb2

from proxy.core.body import BodyQuota, Reason
from proxy.core.judge import (
    DENIED_BODY,
    DENIED_CONTENT_TYPE,
    DENIED_STATUS,
    Allow,
    Deny,
    NeedsBody,
)
from proxy.core.request import Request, build_request
from proxy.envoy_grpc.headers import read_header_map
from proxy.envoy_grpc.pipeline import Outcome, Pipeline, log_failure

_Mode = ProcessingMode.BodySendMode
_FULL_DUPLEX = _Mode.FULL_DUPLEX_STREAMED
# The body modes that send the extension the body.
_READABLE = {_Mode.BUFFERED, _Mode.BUFFERED_PARTIAL, _Mode.STREAMED, _FULL_DUPLEX}

# The answer to each part of the stream, by its field in `ProcessingRequest`.
_RESPONSE_TYPES = {
    "request_headers": external_processor_pb2.HeadersResponse,
    "response_headers": external_processor_pb2.HeadersResponse,
    "request_body": external_processor_pb2.BodyResponse,
    "response_body": external_processor_pb2.BodyResponse,
    "request_trailers": external_processor_pb2.TrailersResponse,
    "response_trailers": external_processor_pb2.TrailersResponse,
}


class ExtProc(external_processor_pb2_grpc.ExternalProcessorServicer):
    """Answers Envoy's `Process` stream with the outcome for the request."""

    def __init__(self, pipeline: Pipeline) -> None:
        self._pipeline = pipeline

    async def Process(self, request_iterator, context):
        stream = _Stream(self._pipeline)
        try:
            async for message in request_iterator:
                part = message.WhichOneof("request")
                if part not in _RESPONSE_TYPES:
                    continue
                for response in await stream.answer(message, part):
                    yield response
                if stream.ended:
                    return
        finally:
            stream.close()


class _Stream:
    """One request's stream: what was configured, and the body parts held
    while the judge waits for the body."""

    def __init__(self, pipeline: Pipeline) -> None:
        self._pipeline = pipeline
        self._mode = _Mode.NONE
        self._holds_headers = False
        # The body parts Envoy forwards only as they're sent back.
        self._echoed: set[str] = set()
        self._target = ""
        self._names: list[str] = []
        # Set while the judge waits for the body.
        self._request: Request | None = None
        self._quota: BodyQuota | None = None
        self._held: list[tuple[object, str]] = []
        self.ended = False

    async def answer(
        self, message, part: str
    ) -> list[external_processor_pb2.ProcessingResponse]:
        """The responses `message` releases: none while it's held, several
        when it releases what was held."""
        if message.HasField("protocol_config"):
            self._read_config(message.protocol_config)
        waiting = self._quota is not None
        try:
            if part == "request_headers":
                return await self._answer_headers(message.request_headers)
            if waiting and part in ("request_body", "request_trailers"):
                self._held.append((message, part))
                return await self._read(message, part)
            return [_continue(message, part, self._echoed)]
        except Exception as exc:  # noqa: BLE001 - before the verdict, or after
            if waiting:
                return self._release(self._pipeline.get_failure_verdict(exc))
            # Past the verdict, so as if this extension were down.
            log_failure(
                f"could not answer Envoy's {part}", exc, "continuing it unchanged"
            )
            return [_continue(message, part, self._echoed)]

    def close(self) -> None:
        if self._quota is not None:
            self._quota.close()
            self._quota = None

    def _read_config(self, protocol_config) -> None:
        self._mode = protocol_config.request_body_mode
        self._holds_headers = self._mode == _FULL_DUPLEX or (
            self._mode == _Mode.STREAMED
            and protocol_config.send_body_without_waiting_for_header_response
        )
        self._echoed = _get_echoed_parts(protocol_config)

    async def _answer_headers(
        self, http_headers
    ) -> list[external_processor_pb2.ProcessingResponse]:
        def build() -> Request:
            # Read while building the request, so a failure there is judged.
            headers = read_header_map(http_headers.headers)
            self._target = _get_pseudo_header(headers, ":authority")
            self._names = [name for name, _ in headers]
            return _build_request(headers)

        request, verdict = self._pipeline.judge_headers(build)
        if isinstance(verdict, NeedsBody):
            if http_headers.end_of_stream:
                verdict = await self._pipeline.judge_body(request, lambda: b"")
            elif self._mode not in _READABLE:
                verdict = await self._pipeline.judge_body(
                    request, lambda: Reason.NOT_SENT
                )
            else:
                self._request = request
                self._quota = self._pipeline.open_quota()
                if self._holds_headers:
                    return []
                # Envoy holds the body until its parts are answered.
                return [self._answer_verdict(Allow())]
        return [self._answer_verdict(verdict)]

    async def _read(
        self, message, part: str
    ) -> list[external_processor_pb2.ProcessingResponse]:
        """Judge the body once `message`, already held, ends it."""
        if part == "request_body":
            body = message.request_body
            reason = self._quota.take(len(body.body))
            # The buffered modes send the body in one part, which doesn't end
            # the stream when trailers follow or, in `BUFFERED_PARTIAL`, when
            # it was cut short: the two look the same.
            partial = self._mode == _Mode.BUFFERED_PARTIAL
            if reason is None and partial and not body.end_of_stream:
                reason = Reason.TRUNCATED
            if reason is not None:
                return self._release(
                    await self._pipeline.judge_body(self._request, lambda: reason)
                )
            if not body.end_of_stream and self._mode != _Mode.BUFFERED:
                return []
        # The body ended: on its last part, in its one part, or with the trailers.
        data = b"".join(
            m.request_body.body for m, p in self._held if p == "request_body"
        )
        return self._release(
            await self._pipeline.judge_body(self._request, lambda: data, self._quota)
        )

    def _release(
        self, verdict: Allow | Deny
    ) -> list[external_processor_pb2.ProcessingResponse]:
        """Answer what was held, now that the verdict is in."""
        self.close()
        held, self._held = self._held, []
        if isinstance(verdict, Deny) or self._holds_headers:
            responses = [self._answer_verdict(verdict)]
        else:
            responses = []
        if self.ended:
            return responses
        return responses + [_continue(m, p, self._echoed) for m, p in held]

    def _answer_verdict(
        self, verdict: Allow | Deny
    ) -> external_processor_pb2.ProcessingResponse:
        """The answer to the headers, or, for a refusal, to the stream."""
        if isinstance(verdict, Deny):
            self.ended = True
        return _answer(self._pipeline.get_outcome(verdict, self._target, self._names))


def _get_echoed_parts(protocol_config) -> set[str]:
    modes = {
        "request_body": protocol_config.request_body_mode,
        "response_body": protocol_config.response_body_mode,
    }
    return {part for part, mode in modes.items() if mode == _FULL_DUPLEX}


def _get_pseudo_header(headers: list[tuple[str, str]], name: str) -> str:
    return next((value for key, value in headers if key == name), "")


def _build_request(headers: list[tuple[str, str]]) -> Request:
    return build_request(
        method=_get_pseudo_header(headers, ":method"),
        scheme=_get_pseudo_header(headers, ":scheme"),
        authority=_get_pseudo_header(headers, ":authority"),
        path=_get_pseudo_header(headers, ":path"),
        headers=headers,
    )


def _answer(outcome: Outcome) -> external_processor_pb2.ProcessingResponse:
    if isinstance(outcome.verdict, Deny):
        return _build_denied_response()
    decision = outcome.decision
    if not decision.headers and not decision.remove:
        return external_processor_pb2.ProcessingResponse(
            request_headers=external_processor_pb2.HeadersResponse()
        )
    mutation = external_processor_pb2.HeaderMutation(
        set_headers=[
            HeaderValueOption(
                header=HeaderValue(key=name, raw_value=value.encode()),
                append_action=HeaderValueOption.OVERWRITE_IF_EXISTS_OR_ADD,
            )
            for name, value in decision.headers.items()
        ],
        remove_headers=sorted(decision.remove),
    )
    return external_processor_pb2.ProcessingResponse(
        request_headers=external_processor_pb2.HeadersResponse(
            response=external_processor_pb2.CommonResponse(
                header_mutation=mutation,
                # Envoy picks the route again from `x-rail-upstream`.
                clear_route_cache=True,
            )
        )
    )


def _build_denied_response() -> external_processor_pb2.ProcessingResponse:
    return external_processor_pb2.ProcessingResponse(
        immediate_response=external_processor_pb2.ImmediateResponse(
            status=http_status_pb2.HttpStatus(code=DENIED_STATUS),
            headers=external_processor_pb2.HeaderMutation(
                set_headers=[
                    HeaderValueOption(
                        header=HeaderValue(
                            key="content-type", raw_value=DENIED_CONTENT_TYPE.encode()
                        ),
                        append_action=HeaderValueOption.OVERWRITE_IF_EXISTS_OR_ADD,
                    )
                ]
            ),
            body=DENIED_BODY.encode(),
        )
    )


def _continue(
    message, part: str, echoed: set[str]
) -> external_processor_pb2.ProcessingResponse:
    """`part` answered unchanged."""
    response = _RESPONSE_TYPES[part]()
    if part in echoed:
        body = getattr(message, part)
        streamed = response.response.body_mutation.streamed_response
        streamed.body = body.body
        streamed.end_of_stream = body.end_of_stream
    return external_processor_pb2.ProcessingResponse(**{part: response})
