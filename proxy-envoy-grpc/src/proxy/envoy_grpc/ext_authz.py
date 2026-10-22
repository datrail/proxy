"""The ext_authz adapter: Envoy's `Check`, answered for one request.

Envoy's ext_authz filter sends every request it forwards to `Check`, with
its body when configured to (`with_request_body`). The answer is OK with the
headers to set and remove, or `403` when the request is refused.
"""

import logging

from envoy.config.core.v3.base_pb2 import HeaderValue, HeaderValueOption
from envoy.service.auth.v3 import external_auth_pb2, external_auth_pb2_grpc
from envoy.type.v3 import http_status_pb2
from google.rpc import code_pb2, status_pb2

from proxy.core.body import BodyQuota, Reason
from proxy.core.judge import (
    DENIED_BODY,
    DENIED_CONTENT_TYPE,
    DENIED_STATUS,
    Deny,
    NeedsBody,
)
from proxy.core.request import Request, build_request
from proxy.envoy_grpc.decision import Decision
from proxy.envoy_grpc.headers import read_header_map
from proxy.envoy_grpc.pipeline import Outcome, Pipeline

log = logging.getLogger(__name__)

# Added by Envoy when it sends the body: how it came, not what the agent sent.
# `true` when truncated, `false` when whole, absent when no body was sent.
_PARTIAL_BODY_HEADER = "x-envoy-auth-partial-body"


class ExtAuthz(external_auth_pb2_grpc.AuthorizationServicer):
    """Answers Envoy's `Check` with the outcome for the request."""

    def __init__(self, pipeline: Pipeline) -> None:
        self._pipeline = pipeline
        self._warned_unpacked = False

    async def Check(self, request, context):
        http = request.attributes.request.http
        # Every header's name, for `decide`: read while building the request,
        # so a failure there is judged.
        names: list[str] = []

        def build() -> Request:
            headers = _get_headers(http)
            names.extend(name for name, _ in headers)
            return _build_request(http, headers)

        built, verdict = self._pipeline.judge_headers(build)
        if isinstance(verdict, NeedsBody):
            with self._pipeline.open_quota() as quota:
                verdict = await self._pipeline.judge_body(
                    built, lambda: self._read_body(http, quota), quota
                )
        return _answer(self._pipeline.get_outcome(verdict, http.host, names))

    def _read_body(self, http, quota: BodyQuota) -> bytes | Reason:
        if http.body:
            # Without `pack_as_bytes`, Envoy replaces every byte that isn't
            # UTF-8 with `!`, so the body may not be the agent's.
            if not self._warned_unpacked:
                self._warned_unpacked = True
                log.warning(
                    "Envoy sent a request body as a string, which may have "
                    "changed it; set pack_as_bytes in ext_authz's "
                    "with_request_body. Judged as not sent."
                )
            return Reason.NOT_SENT
        headers = dict(_get_headers(http))
        partial = headers.get(_PARTIAL_BODY_HEADER)
        if partial == "true":
            return Reason.TRUNCATED
        if partial is None:
            # No body, or one Envoy wasn't configured to send. Only a length
            # tells them apart: Envoy drops `transfer-encoding` from the check.
            if int(headers.get("content-length") or 0) > 0:
                return Reason.NOT_SENT
            return b""
        reason = quota.take(len(http.raw_body))
        if reason is not None:
            return reason
        return http.raw_body


def _build_request(http, headers: list[tuple[str, str]]) -> Request:
    return build_request(
        method=http.method,
        scheme=http.scheme,
        authority=http.host,
        path=http.path,
        headers=(
            (name, value) for name, value in headers if name != _PARTIAL_BODY_HEADER
        ),
    )


def _get_headers(http) -> list[tuple[str, str]]:
    """`headers`, or `header_map` when Envoy sends that instead
    (`encode_raw_headers`)."""
    if http.headers:
        return list(http.headers.items())
    return read_header_map(http.header_map)


def _answer(outcome: Outcome) -> external_auth_pb2.CheckResponse:
    if isinstance(outcome.verdict, Deny):
        return _build_denied_response()
    return _build_ok_response(outcome.decision)


def _build_ok_response(decision: Decision) -> external_auth_pb2.CheckResponse:
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


def _build_denied_response() -> external_auth_pb2.CheckResponse:
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
