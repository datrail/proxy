"""The ext_authz adapter: the request it builds, the judge, and the answer."""

import pytest
from envoy.config.core.v3.base_pb2 import HeaderValue, HeaderValueOption
from envoy.service.auth.v3 import external_auth_pb2
from google.rpc import code_pb2

from core_support import wound_holder, xrail_params
from envoy_support import (
    ORDINARY_HEADERS,
    SECRET,
    BodyJudge,
    apply_check_response,
    assert_check_denied,
    get_pseudo_headers,
    send_check,
    set_protected_hosts,
)
from proxy.core.body import BodyLimits
from proxy.core.judge import Allow, AllowAll, Deny
from proxy.envoy_grpc import ext_authz, pipeline, server


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
        pipeline.Pipeline(
            wound_holder(ticket="the-ticket"),
            set_protected_hosts(monkeypatch),
            pipeline.Judging(
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
        raise ValueError(SECRET)


class _Answering:
    def __init__(self, verdict) -> None:
        self.verdict = verdict

    def judge_headers(self, request):
        return self.verdict


@pytest.mark.asyncio
@pytest.mark.parametrize("case", xrail_params("envoy-grpc"))
async def test_every_row_of_the_contract(monkeypatch, socket_path, case):
    """Over the socket, with every header overwritten, not appended."""
    srv = await server._start_server(
        case.holder(), case.plugin, set_protected_hosts(monkeypatch), socket_path
    )
    try:
        agent = {"host": "mcp.example.com", **ORDINARY_HEADERS, **case.agent_headers}
        response = await send_check(
            socket_path, {**get_pseudo_headers("mcp.example.com"), **agent}
        )
    finally:
        await srv.stop(None)

    assert response.status.code == code_pb2.OK
    assert {h.append_action for h in response.ok_response.headers} == {
        HeaderValueOption.OVERWRITE_IF_EXISTS_OR_ADD
    }
    sent = apply_check_response(agent, response)
    for name, value in case.expected.items():
        assert sent[name] == value
    for name, value in case.forwarded.items():
        assert sent.get(name) == value
    assert not set(sent) & case.absent
    agents_own = {k: v for k, v in agent.items() if not k.startswith("x-rail")}
    assert agents_own.items() <= sent.items()


@pytest.mark.asyncio
async def test_an_unprotected_request_is_answered_ok_with_no_changes(
    monkeypatch, socket_path
):
    srv = await server._start_server(
        wound_holder(ticket="the-ticket"),
        True,
        set_protected_hosts(monkeypatch),
        socket_path,
    )
    try:
        response = await send_check(
            socket_path, {**get_pseudo_headers("open.example.com"), "x-rail": "x"}
        )
    finally:
        await srv.stop(None)

    assert response.status.code == code_pb2.OK
    assert not response.ok_response.headers
    assert not response.ok_response.headers_to_remove


@pytest.mark.asyncio
async def test_a_failure_while_deciding_is_answered_ok_with_no_changes(
    monkeypatch, caplog
):
    """Envoy then forwards the request as if this server were down. The log has
    the traceback, but no header value."""

    def broken(*_args):
        # The message is a header value, as an exception's message can be.
        raise ValueError(SECRET)

    monkeypatch.setattr(pipeline, "decide", broken)

    with caplog.at_level("DEBUG"):
        response = await _check(
            monkeypatch, AllowAll(), Deny(), {"authorization": SECRET}
        )

    assert response.status.code == code_pb2.OK
    assert not response.ok_response.headers
    assert not response.ok_response.headers_to_remove
    assert "could not decide" in caplog.text
    assert "in broken" in caplog.text  # the traceback's frame
    assert "agent-secret" not in caplog.text


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
            "authorization": SECRET,
        },
    )

    request = judge.seen
    assert (request.method, request.scheme) == ("POST", "http")
    assert (request.authority, request.path) == ("mcp.example.com", "/mcp?session=1")
    assert dict(request.headers) == {"authorization": SECRET}


@pytest.mark.asyncio
async def test_the_judge_sees_headers_sent_as_a_header_map(monkeypatch):
    """Envoy's `encode_raw_headers` sends `header_map` instead of `headers`."""
    judge = _Recording()
    servicer = ext_authz.ExtAuthz(
        pipeline.Pipeline(
            None, set_protected_hosts(monkeypatch), pipeline.Judging(judge=judge)
        )
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

    monkeypatch.setattr(pipeline, "decide", never)

    with caplog.at_level("INFO"):
        response = await _check(
            monkeypatch, _Answering(Deny()), Allow(), {"authorization": SECRET}
        )

    assert_check_denied(response)
    assert "refusing a request to mcp.example.com, judged on its headers" in (
        caplog.text
    )
    assert "agent-secret" not in caplog.text


def _break_building(monkeypatch):
    def broken(*_args, **_kwargs):
        raise ValueError(SECRET)

    monkeypatch.setattr(ext_authz, "build_request", broken)


_FAILURES = {
    "building-the-request": (AllowAll(), _break_building),
    "the-judge-raising": (_Raising(), lambda _monkeypatch: None),
    "the-judge-raising-on-the-body": (
        BodyJudge(raises=True),
        lambda _monkeypatch: None,
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", list(_FAILURES))
@pytest.mark.parametrize(
    ("on_failure", "assert_answered", "action"),
    [
        (Deny(), assert_check_denied, "refusing it"),
        (Allow(), _assert_forwarded_with_the_ticket, "forwarding it unjudged"),
    ],
    ids=["refused-by-default", "forwarded-when-the-setting-allows"],
)
async def test_a_judge_failure_follows_the_setting(
    monkeypatch, caplog, failure, on_failure, assert_answered, action
):
    """A request that breaks the judging is never forwarded unjudged unless
    the setting says so, and then as an allowed one: decided on, with the
    x-rail headers. The log names the failure, never a header value."""
    judge, arrange = _FAILURES[failure]
    arrange(monkeypatch)

    with caplog.at_level("DEBUG"):
        response = await _check(
            monkeypatch, judge, on_failure, {"authorization": SECRET}
        )

    assert_answered(response)
    assert action in caplog.text
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
    judge = BodyJudge()
    request = _with_body(raw=body, partial="false", length=len(body))

    with caplog.at_level("INFO"):
        response = await _servicer(monkeypatch, judge).Check(request, None)

    assert judge.bodies == [body]
    if allowed:
        _assert_forwarded_with_the_ticket(response)
        assert "refusing" not in caplog.text
    else:
        assert_check_denied(response)
        assert "judged on its body" in caplog.text
        assert body.decode() not in caplog.text


@pytest.mark.asyncio
async def test_a_request_without_a_body_is_judged_with_an_empty_one(monkeypatch):
    judge = BodyJudge()

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
    judge = BodyJudge()
    servicer = _servicer(
        monkeypatch, judge, limits=BodyLimits(max_bytes=4, budget_bytes=4)
    )

    response = await servicer.Check(request_, None)

    assert judge.bodies == []
    assert_check_denied(response)
    assert f"cannot judge a request's body ({reason}); refusing it" in caplog.text


@pytest.mark.asyncio
async def test_a_body_not_packed_as_bytes_is_warned_about_once(monkeypatch, caplog):
    servicer = _servicer(monkeypatch, BodyJudge())
    request = _with_body(text="abc", partial="false", length=3)

    await servicer.Check(request, None)
    await servicer.Check(request, None)

    assert caplog.text.count("set pack_as_bytes") == 1


@pytest.mark.asyncio
async def test_the_budget_is_given_back_once_a_body_is_judged(monkeypatch):
    servicer = _servicer(
        monkeypatch, BodyJudge(), limits=BodyLimits(max_bytes=4, budget_bytes=4)
    )
    request = _with_body(raw=b"1234", partial="false", length=4)

    first = await servicer.Check(request, None)
    second = await servicer.Check(request, None)

    _assert_forwarded_with_the_ticket(first)
    _assert_forwarded_with_the_ticket(second)
