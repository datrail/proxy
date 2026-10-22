"""The ext_authz adapter: the request it builds, the judge, and the answer."""

import asyncio
import gzip
import tempfile
import time
from pathlib import Path

import grpc
import pytest
from envoy.config.core.v3.base_pb2 import HeaderValue
from envoy.service.auth.v3 import external_auth_pb2, external_auth_pb2_grpc
from google.rpc import code_pb2

from core_support import wound_holder
from proxy.core.body import BodyLimits, BodyPolicy, Reason
from proxy.core.judge import Allow, AllowAll, Deny, NeedsBody
from proxy.envoy_grpc import ext_authz, outcome, server
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


def _servicer(monkeypatch, judge, on_judge_failure=None, limits=None, **judging):
    return ext_authz.ExtAuthz(
        outcome.Pipeline(
            wound_holder(ticket="the-ticket"),
            _protected(monkeypatch),
            outcome.Judging(
                judge=judge,
                on_failure=on_judge_failure or Deny(),
                body_limits=limits or BodyLimits(),
                **judging,
            ),
        )
    )


async def _check(monkeypatch, judge, on_judge_failure, headers=None):
    servicer = _servicer(monkeypatch, judge, on_judge_failure)
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
    servicer = ext_authz.ExtAuthz(
        outcome.Pipeline(None, _protected(monkeypatch), outcome.Judging(judge=judge))
    )
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
async def test_a_denied_request_gets_403_and_no_changes(monkeypatch, caplog):
    """Logged with the host only, never what the judge matched."""

    def never(*_args):
        raise AssertionError("a denied request is not decided on")

    monkeypatch.setattr(outcome, "decide", never)

    with caplog.at_level("INFO"):
        response = await _check(
            monkeypatch, _Answering(Deny()), Allow(), {"authorization": _SECRET}
        )

    _assert_denied(response)
    assert "refusing a request to mcp.example.com, judged on its headers" in (
        caplog.text
    )
    assert "agent-secret" not in caplog.text


@pytest.mark.asyncio
async def test_allow_all_forwards_with_the_x_rail_headers(monkeypatch):
    response = await _check(monkeypatch, AllowAll(), Deny())

    _assert_forwarded_with_the_ticket(response)


class _BodyJudge:
    """Needs every body, and denies one that says so."""

    def __init__(self, raises=False) -> None:
        self.raises = raises
        self.bodies: list[bytes] = []

    def judge_headers(self, request):
        return NeedsBody()

    def judge_body(self, request, body):
        if self.raises:
            raise ValueError(_SECRET)
        self.bodies.append(body.data)
        return Deny() if b"deny" in body.data else Allow()


def _break_building(monkeypatch):
    def broken(*_args, **_kwargs):
        raise ValueError(_SECRET)

    monkeypatch.setattr(ext_authz, "build_request", broken)


_FAILURES = {
    "building-the-request": (AllowAll(), _break_building),
    "the-judge-raising": (_Raising(), lambda _monkeypatch: None),
    "the-judge-raising-on-the-body": (
        _BodyJudge(raises=True),
        lambda _monkeypatch: None,
    ),
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


def _with_body(
    *,
    raw: bytes = b"",
    text: str = "",
    partial: str | None,
    length: int | None,
    headers: dict[str, str] | None = None,
) -> external_auth_pb2.CheckRequest:
    """A request as Envoy sends it with `with_request_body`: the body in
    `raw_body` under `pack_as_bytes`, in `body` without it."""
    headers = dict(headers or {})
    if partial is not None:
        headers["x-envoy-auth-partial-body"] = partial
    if length is not None:
        headers["content-length"] = str(length)
    request = _check_request(headers)
    request.attributes.request.http.raw_body = raw
    request.attributes.request.http.body = text
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "allowed"), [(b'{"ok":1}', True), (b'{"deny":1}', False)]
)
async def test_the_judge_reads_a_body_sent_as_bytes(monkeypatch, caplog, body, allowed):
    judge = _BodyJudge()
    request = _with_body(raw=body, partial="false", length=len(body))

    with caplog.at_level("INFO"):
        response = await _servicer(monkeypatch, judge).Check(request, None)

    assert judge.bodies == [body]
    if allowed:
        _assert_forwarded_with_the_ticket(response)
        assert "refusing" not in caplog.text
    else:
        _assert_denied(response)
        assert "judged on its body" in caplog.text
        assert body.decode() not in caplog.text


@pytest.mark.asyncio
async def test_a_request_without_a_body_is_judged_with_an_empty_one(monkeypatch):
    judge = _BodyJudge()

    response = await _servicer(monkeypatch, judge).Check(
        _with_body(partial=None, length=None), None
    )

    assert judge.bodies == [b""]
    _assert_forwarded_with_the_ticket(response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_", "reason"),
    [
        (_with_body(raw=b"abc", partial="true", length=10), "truncated"),
        # Envoy configured without `with_request_body`.
        (_with_body(partial=None, length=10), "not-sent"),
        # Without `pack_as_bytes`: perhaps changed by Envoy.
        (_with_body(text="abc", partial="false", length=3), "not-sent"),
        (_with_body(raw=b"12345", partial="false", length=5), "too-large"),
    ],
    ids=["truncated", "not-configured", "not-packed-as-bytes", "too-large"],
)
async def test_a_body_that_cannot_be_judged_is_refused(
    monkeypatch, caplog, request_, reason
):
    judge = _BodyJudge()
    servicer = _servicer(
        monkeypatch, judge, limits=BodyLimits(max_bytes=4, budget_bytes=4)
    )

    response = await servicer.Check(request_, None)

    assert judge.bodies == []
    _assert_denied(response)
    assert f"cannot judge a request's body ({reason}); refusing it" in caplog.text


@pytest.mark.asyncio
async def test_a_body_not_packed_as_bytes_is_warned_about_once(monkeypatch, caplog):
    servicer = _servicer(monkeypatch, _BodyJudge())
    request = _with_body(text="abc", partial="false", length=3)

    await servicer.Check(request, None)
    await servicer.Check(request, None)

    assert caplog.text.count("set pack_as_bytes") == 1


@pytest.mark.asyncio
async def test_the_budget_is_given_back_once_a_body_is_judged(monkeypatch):
    servicer = _servicer(
        monkeypatch, _BodyJudge(), limits=BodyLimits(max_bytes=4, budget_bytes=4)
    )
    request = _with_body(raw=b"1234", partial="false", length=4)

    first = await servicer.Check(request, None)
    second = await servicer.Check(request, None)

    _assert_forwarded_with_the_ticket(first)
    _assert_forwarded_with_the_ticket(second)


@pytest.fixture
def socket_path():
    # A unix socket path is limited to about 100 bytes, which pytest's own
    # tmp_path can exceed.
    with tempfile.TemporaryDirectory(prefix="rail-") as directory:
        yield Path(directory) / "ext.sock"


@pytest.mark.asyncio
async def test_a_body_past_grpc_s_default_message_size_is_received(
    monkeypatch, socket_path
):
    """gRPC refuses a message over 4 MiB by default; the server takes one
    as large as the cap and its headers."""
    judge = _BodyJudge()
    body = b"x" * (5 * 1024 * 1024)
    srv = await server._start_server(
        wound_holder(ticket="the-ticket"),
        True,
        _protected(monkeypatch),
        socket_path,
        outcome.Judging(judge=judge),
    )
    try:
        async with grpc.aio.insecure_channel(f"unix:{socket_path}") as channel:
            response = await external_auth_pb2_grpc.AuthorizationStub(channel).Check(
                _with_body(raw=body, partial="false", length=len(body))
            )
    finally:
        await srv.stop(None)

    assert judge.bodies == [body]
    _assert_forwarded_with_the_ticket(response)


def _gzipped_request(data: bytes) -> external_auth_pb2.CheckRequest:
    body = gzip.compress(data)
    return _with_body(
        raw=body,
        partial="false",
        length=len(body),
        headers={"content-encoding": "gzip", "content-type": "application/json"},
    )


@pytest.mark.asyncio
async def test_the_judge_sees_the_body_decoded_and_parsed(monkeypatch):
    seen = []

    class Recording(_BodyJudge):
        def judge_body(self, request, body):
            seen.append(body)
            return Allow()

    await _servicer(monkeypatch, Recording()).Check(
        _gzipped_request(b'{"method": "tools/call"}'), None
    )

    [view] = seen
    assert view.json == {"method": "tools/call"}


@pytest.mark.asyncio
async def test_a_zip_bomb_is_refused_at_the_cap(monkeypatch, caplog):
    judge = _BodyJudge()
    servicer = _servicer(
        monkeypatch, judge, limits=BodyLimits(max_bytes=65536, budget_bytes=65536)
    )
    request = _gzipped_request(b"\x00" * 10_000_000)
    assert len(request.attributes.request.http.raw_body) < 65536

    response = await servicer.Check(request, None)

    assert judge.bodies == []
    _assert_denied(response)
    assert "(decoded-too-large); refusing it" in caplog.text


@pytest.mark.asyncio
async def test_a_reason_the_policy_allows_is_forwarded_unjudged(monkeypatch, caplog):
    judge = _BodyJudge()
    servicer = _servicer(monkeypatch, judge, body_policy=BodyPolicy([Reason.TRUNCATED]))

    response = await servicer.Check(
        _with_body(raw=b"start", partial="true", length=100), None
    )

    assert judge.bodies == []
    _assert_forwarded_with_the_ticket(response)
    assert "(truncated); forwarding it unjudged" in caplog.text


@pytest.mark.asyncio
async def test_a_slow_judgement_is_cut_short_without_stalling_other_calls(
    monkeypatch, caplog
):
    """Judged in a worker thread, so the event loop answers other calls
    meanwhile, and refused as `timed-out` once the time budget runs out."""
    finished: list[str] = []

    class Slow(_BodyJudge):
        def judge_body(self, request, body):
            time.sleep(0.5)
            return Allow()

    slow = _servicer(monkeypatch, Slow(), body_timeout=0.1)
    quick = _servicer(monkeypatch, AllowAll())

    async def call(name, servicer, request):
        response = await servicer.Check(request, None)
        finished.append(name)
        return response

    started = time.monotonic()
    slow_response, _ = await asyncio.gather(
        call("slow", slow, _with_body(raw=b"x", partial="false", length=1)),
        call("quick", quick, _check_request({})),
    )
    elapsed = time.monotonic() - started

    assert finished == ["quick", "slow"]
    assert elapsed < 0.4
    _assert_denied(slow_response)
    assert "(timed-out); refusing it" in caplog.text
