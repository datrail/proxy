"""The ext_proc adapter: Envoy's `Process` stream, answered for one request.

Envoy's ext_proc filter opens one stream per request and sends its parts as
they come, each waiting for an answer. The request's headers are judged and
decided on; the answer sets and removes headers, or refuses the request with
`403`. Every other part is answered unchanged, so a stream never stalls.
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

from proxy.core.judge import DENIED_BODY, DENIED_CONTENT_TYPE, DENIED_STATUS, Deny
from proxy.core.request import Request, build_request
from proxy.envoy_grpc.outcome import Outcome, Pipeline, log_failure

_FULL_DUPLEX = ProcessingMode.BodySendMode.FULL_DUPLEX_STREAMED

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
        # The body parts Envoy forwards only as they're sent back.
        echoed: set[str] = set()
        async for message in request_iterator:
            part = message.WhichOneof("request")
            if part not in _RESPONSE_TYPES:
                continue
            if message.HasField("protocol_config"):
                echoed = _get_echoed_parts(message.protocol_config)
            try:
                if part == "request_headers":
                    response = self._answer_headers(message.request_headers)
                else:
                    response = _continue(message, part, echoed)
            except Exception as exc:  # noqa: BLE001 - every failure is continued
                # Past the verdict, so as if this extension were down.
                log_failure(
                    f"could not answer Envoy's {part}", exc, "continuing it unchanged"
                )
                response = _continue(message, part, echoed)
            yield response
            if response.HasField("immediate_response"):
                return

    def _answer_headers(
        self, http_headers
    ) -> external_processor_pb2.ProcessingResponse:
        headers = _get_headers(http_headers)
        outcome = self._pipeline.get_outcome(
            lambda: _build_request(headers),
            _get_pseudo_header(headers, ":authority"),
            [name for name, _ in headers],
        )
        return _answer(outcome)


def _get_echoed_parts(protocol_config) -> set[str]:
    modes = {
        "request_body": protocol_config.request_body_mode,
        "response_body": protocol_config.response_body_mode,
    }
    return {part for part, mode in modes.items() if mode == _FULL_DUPLEX}


def _get_headers(http_headers) -> list[tuple[str, str]]:
    return [
        (h.key, h.raw_value.decode("latin-1") if h.raw_value else h.value)
        for h in http_headers.headers.headers
    ]


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
        return _denied()
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


def _denied() -> external_processor_pb2.ProcessingResponse:
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
