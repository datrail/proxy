"""Helpers for the proxy-envoy-grpc suite: a judge that reads bodies, and
ext_authz's `Check` and the refusals both callouts answer with."""

from pathlib import Path

import grpc
from envoy.service.auth.v3 import external_auth_pb2, external_auth_pb2_grpc
from google.rpc import code_pb2

from proxy.core.judge import Allow, Deny, NeedsBody

# A header value, which must never reach the log.
SECRET = "Bearer agent-secret"


class BodyJudge:
    """Needs every body, and denies one that says so."""

    def __init__(self, raises=False) -> None:
        self.raises = raises
        self.bodies: list[bytes] = []

    def judge_headers(self, request):
        return NeedsBody()

    def judge_body(self, request, body):
        if self.raises:
            raise ValueError(SECRET)
        self.bodies.append(body.data)
        return Deny() if b"deny" in body.data else Allow()


def get_pseudo_headers(target: str) -> dict[str, str]:
    return {":method": "POST", ":scheme": "http", ":authority": target, ":path": "/mcp"}


async def send_check(path: Path, headers: dict[str, str]):
    """ext_authz's `Check` for a request with `headers`, `:authority`
    included, over the socket at `path`."""
    request = external_auth_pb2.CheckRequest()
    http = request.attributes.request.http
    http.host = headers[":authority"]
    http.headers.update(headers)
    async with grpc.aio.insecure_channel(f"unix:{path}") as channel:
        return await external_auth_pb2_grpc.AuthorizationStub(channel).Check(request)


def apply_check_response(agent: dict[str, str], response) -> dict[str, str]:
    """The agent's headers with `Check`'s response applied, as Envoy applies
    it."""
    ok = response.ok_response
    sent = {k: v for k, v in agent.items() if k not in set(ok.headers_to_remove)}
    sent.update({h.header.key: h.header.value for h in ok.headers})
    return sent


def assert_check_denied(response):
    assert response.status.code == code_pb2.PERMISSION_DENIED
    denied = response.denied_response
    assert denied.status.code == 403
    assert denied.body == "request refused by policy\n"
    assert {h.header.key: h.header.value for h in denied.headers} == {
        "content-type": "text/plain"
    }
    assert not response.HasField("ok_response")


def assert_immediately_denied(response):
    """ext_proc's refusal, which ends the stream."""
    immediate = response.immediate_response
    assert immediate.status.code == 403
    assert immediate.body == b"request refused by policy\n"
    assert {
        h.header.key: h.header.raw_value for h in immediate.headers.set_headers
    } == {"content-type": b"text/plain"}
