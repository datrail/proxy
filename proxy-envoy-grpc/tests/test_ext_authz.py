"""The ext_authz adapter: the request it builds, the judge, and the answer."""

import pytest
from envoy.config.core.v3.base_pb2 import HeaderValue
from envoy.service.auth.v3 import external_auth_pb2
from google.rpc import code_pb2

from core_support import wound_holder
from proxy.core.judge import Allow, AllowAll, Deny, NeedsBody
from proxy.envoy_grpc import ext_authz
from proxy.envoy_grpc import settings as envoy_settings

_SECRET = "Bearer agent-secret"


def _protected(monkeypatch):
    monkeypatch.setenv("RAIL_PROXY_PROTECTED_HOSTS", "mcp.example.com")
    return envoy_settings.get_protected_hosts(plugin_on=False)


def _check_request(headers: dict[str, str]) -> external_auth_pb2.CheckRequest:
    request = external_auth_pb2.CheckRequest()
    http = request.attributes.request.http
    http.method = "POST"
    http.scheme = "http"
    http.host = "mcp.example.com"
    http.path = "/mcp?session=1"
    http.headers.update(
        {":authority": "mcp.example.com", ":path": "/mcp?session=1", **headers}
    )
    return request


async def _check(monkeypatch, judge, on_judge_failure, headers=None):
    servicer = ext_authz.ExtAuthz(
        wound_holder(ticket="the-ticket"),
        _protected(monkeypatch),
        judge,
        on_judge_failure,
    )
    return await servicer.Check(_check_request(headers or {}), None)


def _assert_denied(response):
    assert response.status.code == code_pb2.PERMISSION_DENIED
    denied = response.denied_response
    assert denied.status.code == 403
    assert denied.body == "request refused by policy\n"
    assert {h.header.key: h.header.value for h in denied.headers} == {
        "content-type": "text/plain"
    }
    assert not response.HasField("ok_response")


def _assert_forwarded_with_the_ticket(response):
    assert response.status.code == code_pb2.OK
    set_headers = {h.header.key: h.header.value for h in response.ok_response.headers}
    assert set_headers["x-rail"] == "the-ticket"


class _Recording:
    def __init__(self) -> None:
        self.seen = None

    def judge_headers(self, request):
        self.seen = request
        return Allow()


class _Raising:
    def judge_headers(self, request):
        # The message is a header value, as an exception's message can be.
        raise ValueError(_SECRET)


class _Answering:
    def __init__(self, verdict) -> None:
        self.verdict = verdict

    def judge_headers(self, request):
        return self.verdict


@pytest.mark.asyncio
async def test_the_judge_sees_the_request_without_x_rail_or_envoy_headers(
    monkeypatch,
):
    judge = _Recording()

    await _check(
        monkeypatch,
        judge,
        Deny(),
        {
            "x-rail": "forged",
            "x-envoy-auth-partial-body": "false",
            "authorization": _SECRET,
        },
    )

    request = judge.seen
    assert (request.method, request.scheme) == ("POST", "http")
    assert (request.authority, request.path) == ("mcp.example.com", "/mcp?session=1")
    assert dict(request.headers) == {"authorization": _SECRET}


@pytest.mark.asyncio
async def test_the_judge_sees_headers_sent_as_a_header_map(monkeypatch):
    """Envoy's `encode_raw_headers` sends `header_map` instead of `headers`."""
    judge = _Recording()
    servicer = ext_authz.ExtAuthz(None, _protected(monkeypatch), judge, Deny())
    request = external_auth_pb2.CheckRequest()
    http = request.attributes.request.http
    http.host = "mcp.example.com"
    http.header_map.headers.extend(
        [
            HeaderValue(key=":authority", raw_value=b"mcp.example.com"),
            HeaderValue(key="accept", raw_value=b"a"),
            HeaderValue(key="accept", raw_value=b"b"),
        ]
    )

    await servicer.Check(request, None)

    assert judge.seen.headers == (("accept", "a"), ("accept", "b"))


@pytest.mark.asyncio
async def test_a_denied_request_gets_403_and_no_changes(monkeypatch):
    def never(*_args):
        raise AssertionError("a denied request is not decided on")

    monkeypatch.setattr(ext_authz, "decide", never)

    response = await _check(monkeypatch, _Answering(Deny()), Allow())

    _assert_denied(response)


@pytest.mark.asyncio
async def test_allow_all_forwards_with_the_x_rail_headers(monkeypatch):
    response = await _check(monkeypatch, AllowAll(), Deny())

    _assert_forwarded_with_the_ticket(response)


def _break_building(monkeypatch):
    def broken(*_args, **_kwargs):
        raise ValueError(_SECRET)

    monkeypatch.setattr(ext_authz, "build_request", broken)


_FAILURES = {
    "building-the-request": (AllowAll(), _break_building),
    "the-judge-raising": (_Raising(), lambda _monkeypatch: None),
    "the-judge-needing-the-body": (_Answering(NeedsBody()), lambda _monkeypatch: None),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", list(_FAILURES))
async def test_a_judge_failure_is_refused_by_default(monkeypatch, caplog, failure):
    """Q3, not D13: a request that breaks the judging is never forwarded
    unjudged. The log names the failure, never a header value."""
    judge, arrange = _FAILURES[failure]
    arrange(monkeypatch)

    with caplog.at_level("DEBUG"):
        response = await _check(monkeypatch, judge, Deny(), {"authorization": _SECRET})

    _assert_denied(response)
    assert "refusing it" in caplog.text
    assert "agent-secret" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", list(_FAILURES))
async def test_a_judge_failure_is_forwarded_when_the_setting_allows(
    monkeypatch, caplog, failure
):
    """Forwarded as an allowed request: decided on, with the x-rail headers."""
    judge, arrange = _FAILURES[failure]
    arrange(monkeypatch)

    with caplog.at_level("DEBUG"):
        response = await _check(monkeypatch, judge, Allow(), {"authorization": _SECRET})

    _assert_forwarded_with_the_ticket(response)
    assert "forwarding it unjudged" in caplog.text
    assert "agent-secret" not in caplog.text
