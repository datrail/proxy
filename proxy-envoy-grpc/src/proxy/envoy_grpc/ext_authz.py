"""The ext_authz adapter: Envoy's `Check`, answered for one request.

Envoy's ext_authz filter sends every request it forwards to `Check`. The
answer is OK with the headers to set and remove, or `403` when the request is
refused.
"""

from collections.abc import Mapping

from envoy.config.core.v3.base_pb2 import HeaderValue, HeaderValueOption
from envoy.service.auth.v3 import external_auth_pb2, external_auth_pb2_grpc
from envoy.type.v3 import http_status_pb2
from google.rpc import code_pb2, status_pb2

from proxy.core.judge import (
    DENIED_BODY,
    DENIED_CONTENT_TYPE,
    DENIED_STATUS,
    Allow,
    Deny,
    Judge,
)
from proxy.core.request import Request, build_request
from proxy.core.xrail_auth import TicketHeaders, TicketHolder, outbound_headers
from proxy.envoy_grpc.decision import Decision, decide
from proxy.envoy_grpc.outcome import Outcome, get_outcome
from proxy.envoy_grpc.settings import ProtectedHost

# Added by Envoy when it sends the body: how it came, not what the agent sent.
_PARTIAL_BODY_HEADER = "x-envoy-auth-partial-body"


class ExtAuthz(external_auth_pb2_grpc.AuthorizationServicer):
    """Answers Envoy's `Check` with the outcome for the request."""

    def __init__(
        self,
        holder: TicketHolder | None,
        protected_hosts: Mapping[str, ProtectedHost],
        judge: Judge,
        on_judge_failure: Allow | Deny,
    ) -> None:
        self._holder = holder
        self._protected_hosts = protected_hosts
        self._ticket_headers = TicketHeaders(holder)
        self._judge = judge
        self._on_judge_failure = on_judge_failure

    async def Check(self, request, context):
        http = request.attributes.request.http
        outcome = get_outcome(
            lambda: self._judge.judge_headers(_build_request(http)),
            lambda: self._decide(request),
            self._on_judge_failure,
        )
        return _answer(outcome)

    def _decide(self, request) -> Decision:
        http = request.attributes.request.http
        names = list(http.headers) or [h.key for h in http.header_map.headers]
        xrail_headers = outbound_headers(self._holder)
        decision = decide(http.host, names, self._protected_hosts, xrail_headers)
        if decision.headers:
            self._ticket_headers.report(http.host, xrail_headers)
        return decision


def _build_request(http) -> Request:
    return build_request(
        method=http.method,
        scheme=http.scheme,
        authority=http.host,
        path=http.path,
        headers=(
            (name, value)
            for name, value in _get_headers(http)
            if name != _PARTIAL_BODY_HEADER
        ),
    )


def _get_headers(http) -> list[tuple[str, str]]:
    """`headers`, or `header_map` when Envoy sends that instead
    (`encode_raw_headers`)."""
    if http.headers:
        return list(http.headers.items())
    return [
        (h.key, h.value or h.raw_value.decode("latin-1"))
        for h in http.header_map.headers
    ]


def _answer(outcome: Outcome) -> external_auth_pb2.CheckResponse:
    if isinstance(outcome.verdict, Deny):
        return _denied()
    return _ok(outcome.decision)


def _ok(decision: Decision) -> external_auth_pb2.CheckResponse:
    return external_auth_pb2.CheckResponse(
        status=status_pb2.Status(code=code_pb2.OK),
        ok_response=external_auth_pb2.OkHttpResponse(
            headers=[
                HeaderValueOption(
                    header=HeaderValue(key=name, value=value),
                    append_action=HeaderValueOption.OVERWRITE_IF_EXISTS_OR_ADD,
                )
                for name, value in decision.headers.items()
            ],
            headers_to_remove=sorted(decision.remove),
        ),
    )


def _denied() -> external_auth_pb2.CheckResponse:
    return external_auth_pb2.CheckResponse(
        status=status_pb2.Status(code=code_pb2.PERMISSION_DENIED),
        denied_response=external_auth_pb2.DeniedHttpResponse(
            status=http_status_pb2.HttpStatus(code=DENIED_STATUS),
            headers=[
                HeaderValueOption(
                    header=HeaderValue(key="content-type", value=DENIED_CONTENT_TYPE),
                    append_action=HeaderValueOption.OVERWRITE_IF_EXISTS_OR_ADD,
                )
            ],
            body=DENIED_BODY,
        ),
    )
