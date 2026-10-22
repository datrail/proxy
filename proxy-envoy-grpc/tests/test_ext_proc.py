"""The ext_proc adapter, over a real stream on a unix socket."""

import asyncio
import tempfile
from pathlib import Path

import grpc
import pytest
from envoy.config.core.v3.base_pb2 import HeaderValue, HeaderValueOption
from envoy.extensions.filters.http.ext_proc.v3.processing_mode_pb2 import (
    ProcessingMode,
)
from envoy.service.auth.v3 import external_auth_pb2, external_auth_pb2_grpc
from envoy.service.ext_proc.v3 import (
    external_processor_pb2 as ep,
)
from envoy.service.ext_proc.v3 import (
    external_processor_pb2_grpc as ep_grpc,
)

from core_support import wound_holder, xrail_params
from proxy.core.body import BodyLimits
from proxy.core.judge import Allow, Deny, NeedsBody
from proxy.envoy_grpc import ext_proc, outcome, server
from proxy.envoy_grpc import settings as envoy_settings

_SECRET = "Bearer agent-secret"

_ORDINARY = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "user-agent": "agent/1",
    "x-trace": "abc",
}

_Mode = ProcessingMode.BodySendMode


@pytest.fixture
def socket_path():
    # A unix socket path is limited to about 100 bytes, which pytest's own
    # tmp_path can exceed.
    with tempfile.TemporaryDirectory(prefix="rail-") as directory:
        yield Path(directory) / "ext.sock"


def _protected(monkeypatch):
    monkeypatch.setenv(
        "RAIL_PROXY_PROTECTED_HOSTS",
        "mcp.example.com,mcp-port.example.com:8443,http://plain.example.com",
    )
    return envoy_settings.get_protected_hosts(plugin_on=False)


def _pseudo(target: str) -> dict[str, str]:
    return {":method": "POST", ":scheme": "http", ":authority": target, ":path": "/mcp"}


def _headers(
    headers: dict[str, str],
    *,
    end_of_stream=True,
    body_mode=None,
    without_waiting=False,
) -> ep.ProcessingRequest:
    message = ep.ProcessingRequest(
        request_headers=ep.HttpHeaders(end_of_stream=end_of_stream)
    )
    message.request_headers.headers.headers.extend(
        HeaderValue(key=k, raw_value=v.encode()) for k, v in headers.items()
    )
    if body_mode is not None:
        message.protocol_config.request_body_mode = body_mode
    if without_waiting:
        message.protocol_config.send_body_without_waiting_for_header_response = True
    return message


def _body(data: bytes, end_of_stream: bool) -> ep.ProcessingRequest:
    return ep.ProcessingRequest(
        request_body=ep.HttpBody(body=data, end_of_stream=end_of_stream)
    )


_TRAILERS = ep.ProcessingRequest(request_trailers=ep.HttpTrailers())
_RESPONSE_HEADERS = ep.ProcessingRequest(response_headers=ep.HttpHeaders())


async def _process(path: Path, messages: list[ep.ProcessingRequest]):
    async with grpc.aio.insecure_channel(f"unix:{path}") as channel:
        call = ep_grpc.ExternalProcessorStub(channel).Process(iter(messages))
        return [response async for response in call]


async def _start(monkeypatch, socket_path, **judging):
    """The server, holding a ticket."""
    return await _start_with(
        monkeypatch, socket_path, wound_holder(ticket="the-ticket"), True, **judging
    )


async def _start_with(monkeypatch, socket_path, holder, plugin, **judging):
    return await server._start_server(
        holder, plugin, _protected(monkeypatch), socket_path, **judging
    )


def _applied(agent: dict[str, str], response) -> dict[str, str]:
    """The agent's headers with the headers response applied, as Envoy
    applies it."""
    mutation = response.request_headers.response.header_mutation
    sent = {k: v for k, v in agent.items() if k not in set(mutation.remove_headers)}
    sent.update(
        {h.header.key: h.header.raw_value.decode() for h in mutation.set_headers}
    )
    return sent


def _part(response) -> str:
    return response.WhichOneof("response")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", xrail_params("envoy-grpc"))
async def test_every_row_of_the_contract(monkeypatch, socket_path, case):
    srv = await _start_with(monkeypatch, socket_path, case.holder(), case.plugin)
    try:
        agent = {**_pseudo("mcp.example.com"), **_ORDINARY, **case.agent_headers}
        [response] = await _process(socket_path, [_headers(agent)])
    finally:
        await srv.stop(None)

    sent = _applied(agent, response)
    for name, value in case.expected.items():
        assert sent[name] == value
    for name, value in case.forwarded.items():
        assert sent.get(name) == value
    assert not set(sent) & case.absent
    agents_own = {k: v for k, v in agent.items() if not k.startswith(("x-rail", ":"))}
    assert agents_own.items() <= sent.items()


@pytest.mark.asyncio
async def test_a_protected_request_is_routed_again(monkeypatch, socket_path):
    """Headers overwritten, not appended, and the route picked again from
    `x-rail-upstream`."""
    srv = await _start(monkeypatch, socket_path)
    try:
        [response] = await _process(
            socket_path, [_headers(_pseudo("mcp-port.example.com"))]
        )
    finally:
        await srv.stop(None)

    common = response.request_headers.response
    assert common.clear_route_cache
    assert {h.append_action for h in common.header_mutation.set_headers} == {
        HeaderValueOption.OVERWRITE_IF_EXISTS_OR_ADD
    }
    sent = _applied({}, response)
    assert sent[":authority"] == "mcp-port.example.com:8443"
    assert sent["x-rail-upstream"] == "tls"


@pytest.mark.asyncio
async def test_an_unprotected_request_is_continued_unchanged(monkeypatch, socket_path):
    srv = await _start(monkeypatch, socket_path)
    try:
        [response] = await _process(
            socket_path, [_headers({**_pseudo("open.example.com"), "x-rail": "x"})]
        )
    finally:
        await srv.stop(None)

    assert response == ep.ProcessingResponse(request_headers=ep.HeadersResponse())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [_Mode.BUFFERED, _Mode.BUFFERED_PARTIAL, _Mode.STREAMED],
    ids=["buffered", "buffered-partial", "streamed"],
)
async def test_a_body_is_continued_untouched(monkeypatch, socket_path, mode):
    """Every part answered, in order, so the stream never stalls."""
    messages = [
        _headers(_pseudo("mcp.example.com"), end_of_stream=False, body_mode=mode),
        _body(b'{"jsonrpc":', False),
        _body(b'"2.0"}', False),
        _TRAILERS,
        _RESPONSE_HEADERS,
    ]
    srv = await _start(monkeypatch, socket_path)
    try:
        responses = await _process(socket_path, messages)
    finally:
        await srv.stop(None)

    assert [_part(r) for r in responses] == [
        "request_headers",
        "request_body",
        "request_body",
        "request_trailers",
        "response_headers",
    ]
    assert all(r == ep.ProcessingResponse(**{_part(r): {}}) for r in responses[1:])


@pytest.mark.asyncio
async def test_a_full_duplex_body_is_sent_back_as_it_came(monkeypatch, socket_path):
    """In this mode Envoy forwards only the body it's sent back."""
    messages = [
        _headers(
            _pseudo("mcp.example.com"),
            end_of_stream=False,
            body_mode=_Mode.FULL_DUPLEX_STREAMED,
        ),
        _body(b"first", False),
        _body(b"last", True),
    ]
    srv = await _start(monkeypatch, socket_path)
    try:
        responses = await _process(socket_path, messages)
    finally:
        await srv.stop(None)

    streamed = [
        r.request_body.response.body_mutation.streamed_response for r in responses[1:]
    ]
    assert [(s.body, s.end_of_stream) for s in streamed] == [
        (b"first", False),
        (b"last", True),
    ]


async def _check(path: Path, headers: dict[str, str]):
    request = external_auth_pb2.CheckRequest()
    http = request.attributes.request.http
    http.host = headers[":authority"]
    http.headers.update(headers)
    async with grpc.aio.insecure_channel(f"unix:{path}") as channel:
        return await external_auth_pb2_grpc.AuthorizationStub(channel).Check(request)


def _applied_check(agent: dict[str, str], response) -> dict[str, str]:
    ok = response.ok_response
    sent = {k: v for k, v in agent.items() if k not in set(ok.headers_to_remove)}
    sent.update({h.header.key: h.header.value for h in ok.headers})
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "mcp.example.com",
        "mcp.example.com:9000",
        "mcp.example.com:80",
        "mcp-port.example.com:9000",
        "plain.example.com",
        "open.example.com:9000",
    ],
)
@pytest.mark.parametrize("first", ["ext_authz", "ext_proc"])
async def test_two_calls_for_one_request_change_nothing_more(
    monkeypatch, socket_path, target, first
):
    """Both filters in one chain: the second call sees the request as the first
    changed it, and leaves it as it is."""

    async def through_ext_authz(agent):
        return _applied_check(agent, await _check(socket_path, agent))

    async def through_ext_proc(agent):
        [response] = await _process(socket_path, [_headers(agent)])
        return _applied(agent, response)

    calls = [through_ext_authz, through_ext_proc]
    if first == "ext_proc":
        calls.reverse()
    agent = {**_pseudo(target), **_ORDINARY, "x-rail": "forged"}
    srv = await _start(monkeypatch, socket_path)
    try:
        once = await calls[0](agent)
        twice = await calls[1](once)
    finally:
        await srv.stop(None)

    assert twice == once


def _fail_once_at(monkeypatch, name: str, part: str):
    """Make `ext_proc.<name>` raise the first time it answers `part`."""
    real = getattr(ext_proc, name)
    failed = []

    def flaky(message, *args):
        if message.WhichOneof("request") == part and not failed:
            failed.append(part)
            # The message is a header value, as an exception's message can be.
            raise ValueError(_SECRET)
        return real(message, *args)

    monkeypatch.setattr(ext_proc, name, flaky)


def _fail_headers(monkeypatch):
    def broken(*_args):
        raise ValueError(_SECRET)

    monkeypatch.setattr(ext_proc, "_answer", broken)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "part",
    ["request_headers", "request_body", "request_trailers", "response_headers"],
)
async def test_a_failure_at_any_part_is_continued_and_the_stream_goes_on(
    monkeypatch, socket_path, caplog, part
):
    """D13 for the whole stream: the part is answered unchanged, with its own
    response type, and the rest of the stream is answered as usual. The log
    names the part, never a header value."""
    if part == "request_headers":
        _fail_headers(monkeypatch)
    else:
        _fail_once_at(monkeypatch, "_continue", part)
    messages = [
        _headers(
            {**_pseudo("mcp.example.com"), "authorization": _SECRET},
            end_of_stream=False,
            body_mode=_Mode.STREAMED,
        ),
        _body(b"data", True),
        _TRAILERS,
        _RESPONSE_HEADERS,
    ]
    srv = await _start(monkeypatch, socket_path)
    try:
        with caplog.at_level("DEBUG"):
            responses = await _process(socket_path, messages)
    finally:
        await srv.stop(None)

    assert [_part(r) for r in responses] == [
        "request_headers",
        "request_body",
        "request_trailers",
        "response_headers",
    ]
    failed = next(r for r in responses if _part(r) == part)
    assert failed == ep.ProcessingResponse(**{part: {}})
    assert f"could not answer Envoy's {part}" in caplog.text
    assert "agent-secret" not in caplog.text


def _break_building(monkeypatch):
    def broken(*_args):
        raise ValueError(_SECRET)

    monkeypatch.setattr(ext_proc, "_build_request", broken)


@pytest.mark.asyncio
async def test_a_judge_failure_is_refused_by_default(monkeypatch, socket_path, caplog):
    """Q3, not D13: refused at once, and the stream ends there."""
    _break_building(monkeypatch)
    messages = [
        _headers(
            {**_pseudo("mcp.example.com"), "authorization": _SECRET},
            end_of_stream=False,
            body_mode=_Mode.BUFFERED,
        ),
        _body(b"data", True),
    ]
    srv = await _start(
        monkeypatch, socket_path, judging=outcome.Judging(on_failure=Deny())
    )
    try:
        with caplog.at_level("DEBUG"):
            responses = await _process(socket_path, messages)
    finally:
        await srv.stop(None)

    [response] = responses
    immediate = response.immediate_response
    assert immediate.status.code == 403
    assert immediate.body == b"request refused by policy\n"
    assert {
        h.header.key: h.header.raw_value for h in immediate.headers.set_headers
    } == {"content-type": b"text/plain"}
    assert "refusing it" in caplog.text
    assert "agent-secret" not in caplog.text


@pytest.mark.asyncio
async def test_a_judge_failure_is_forwarded_when_the_setting_allows(
    monkeypatch, socket_path
):
    _break_building(monkeypatch)
    srv = await _start(
        monkeypatch, socket_path, judging=outcome.Judging(on_failure=Allow())
    )
    try:
        [response] = await _process(socket_path, [_headers(_pseudo("mcp.example.com"))])
    finally:
        await srv.stop(None)

    assert _applied({}, response)["x-rail"] == "the-ticket"


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
        self.bodies.append(body)
        return Deny() if b"deny" in body else Allow()


class _Live:
    """A stream driven a part at a time, as Envoy drives it, its responses
    collected as they come."""

    def __init__(self, channel) -> None:
        self.call = ep_grpc.ExternalProcessorStub(channel).Process()
        self.responses: list[ep.ProcessingResponse] = []
        self._reading = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        async for response in self.call:
            self.responses.append(response)

    async def send(self, message, *, then: int) -> list[str]:
        """Send `message`, and the parts of the `then` responses it
        releases."""
        before = len(self.responses)
        await self.call.write(message)
        async with asyncio.timeout(5):
            while len(self.responses) < before + then:
                await asyncio.sleep(0.01)
        # Long enough for a response that shouldn't come to arrive.
        await asyncio.sleep(0.05)
        return [_part(r) for r in self.responses[before:]]

    async def finish(self) -> None:
        await self.call.done_writing()
        await self._reading


def _judging(judge, max_bytes=1024, budget_bytes=1024, on_failure=None):
    return outcome.Judging(
        judge=judge,
        on_failure=on_failure or Deny(),
        body_limits=BodyLimits(max_bytes=max_bytes, budget_bytes=budget_bytes),
    )


def _body_headers(mode, **kwargs):
    return _headers(
        {**_pseudo("mcp.example.com"), "authorization": _SECRET},
        end_of_stream=False,
        body_mode=mode,
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", [_Mode.BUFFERED, _Mode.STREAMED], ids=["buffered", "streamed"]
)
async def test_headers_are_answered_at_once_and_the_body_held(
    monkeypatch, socket_path, mode
):
    """Where Envoy holds the body until it's answered, the headers go first;
    the body's parts are answered once the judge has the whole body."""
    judge = _BodyJudge()
    srv = await _start(monkeypatch, socket_path, judging=_judging(judge))
    try:
        async with grpc.aio.insecure_channel(f"unix:{socket_path}") as channel:
            live = _Live(channel)
            assert await live.send(_body_headers(mode), then=1) == ["request_headers"]
            assert await live.send(_body(b"one,", False), then=0) == []
            assert await live.send(_body(b"two", True), then=2) == [
                "request_body",
                "request_body",
            ]
            await live.finish()
    finally:
        await srv.stop(None)

    assert judge.bodies == [b"one,two"]
    assert _applied({}, live.responses[0])["x-rail"] == "the-ticket"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "without_waiting"),
    [(_Mode.STREAMED, True), (_Mode.FULL_DUPLEX_STREAMED, False)],
    ids=["streamed-without-waiting", "full-duplex"],
)
async def test_the_headers_are_held_with_the_body(
    monkeypatch, socket_path, mode, without_waiting
):
    """Where Envoy sends the body before the headers are answered, nothing is
    answered, so nothing goes upstream, before the verdict."""
    judge = _BodyJudge()
    srv = await _start(monkeypatch, socket_path, judging=_judging(judge))
    try:
        async with grpc.aio.insecure_channel(f"unix:{socket_path}") as channel:
            live = _Live(channel)
            headers = _body_headers(mode, without_waiting=without_waiting)
            assert await live.send(headers, then=0) == []
            assert await live.send(_body(b"one,", False), then=0) == []
            assert await live.send(_body(b"two", True), then=3) == [
                "request_headers",
                "request_body",
                "request_body",
            ]
            await live.finish()
    finally:
        await srv.stop(None)

    assert judge.bodies == [b"one,two"]
    assert _applied({}, live.responses[0])["x-rail"] == "the-ticket"
    if mode == _Mode.FULL_DUPLEX_STREAMED:
        streamed = [
            r.request_body.response.body_mutation.streamed_response.body
            for r in live.responses[1:]
        ]
        assert streamed == [b"one,", b"two"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "without_waiting"),
    [
        (_Mode.BUFFERED, False),
        (_Mode.STREAMED, False),
        (_Mode.STREAMED, True),
        (_Mode.FULL_DUPLEX_STREAMED, False),
    ],
    ids=["buffered", "streamed", "streamed-without-waiting", "full-duplex"],
)
async def test_a_body_the_judge_denies_is_refused(
    monkeypatch, socket_path, mode, without_waiting
):
    messages = [
        _body_headers(mode, without_waiting=without_waiting),
        _body(b"please deny", True),
    ]
    srv = await _start(monkeypatch, socket_path, judging=_judging(_BodyJudge()))
    try:
        responses = await _process(socket_path, messages)
    finally:
        await srv.stop(None)

    assert responses[-1].immediate_response.status.code == 403
    assert all(not r.HasField("immediate_response") for r in responses[:-1])


@pytest.mark.asyncio
async def test_trailers_end_the_body(monkeypatch, socket_path):
    judge = _BodyJudge()
    messages = [
        _body_headers(_Mode.STREAMED),
        _body(b"all of it", False),
        _TRAILERS,
    ]
    srv = await _start(monkeypatch, socket_path, judging=_judging(judge))
    try:
        responses = await _process(socket_path, messages)
    finally:
        await srv.stop(None)

    assert judge.bodies == [b"all of it"]
    assert [_part(r) for r in responses] == [
        "request_headers",
        "request_body",
        "request_trailers",
    ]


@pytest.mark.asyncio
async def test_a_request_without_a_body_is_judged_with_an_empty_one(
    monkeypatch, socket_path
):
    judge = _BodyJudge()
    srv = await _start(monkeypatch, socket_path, judging=_judging(judge))
    try:
        [response] = await _process(socket_path, [_headers(_pseudo("mcp.example.com"))])
    finally:
        await srv.stop(None)

    assert judge.bodies == [b""]
    assert _applied({}, response)["x-rail"] == "the-ticket"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("messages", "reason"),
    [
        ([_body_headers(_Mode.NONE)], "not-sent"),
        (
            [_body_headers(_Mode.BUFFERED_PARTIAL), _body(b"start", False)],
            "truncated",
        ),
        ([_body_headers(_Mode.STREAMED), _body(b"x" * 9, False)], "too-large"),
    ],
    ids=["not-sent", "truncated", "too-large"],
)
async def test_a_body_that_cannot_be_judged_is_refused(
    monkeypatch, socket_path, caplog, messages, reason
):
    judge = _BodyJudge()
    srv = await _start(
        monkeypatch, socket_path, judging=_judging(judge, max_bytes=8, budget_bytes=8)
    )
    try:
        responses = await _process(socket_path, messages)
    finally:
        await srv.stop(None)

    assert judge.bodies == []
    assert responses[-1].immediate_response.status.code == 403
    assert f"cannot judge a request's body ({reason}); refusing it" in caplog.text


@pytest.mark.asyncio
async def test_bodies_past_the_budget_are_refused_and_the_budget_comes_back(
    monkeypatch, socket_path, caplog
):
    """Two requests holding bodies at once share the budget. Each gives its
    share back when its stream ends, however it ends."""
    judge = _BodyJudge()
    srv = await _start(
        monkeypatch, socket_path, judging=_judging(judge, max_bytes=8, budget_bytes=8)
    )
    try:
        async with grpc.aio.insecure_channel(f"unix:{socket_path}") as channel:
            first, second = _Live(channel), _Live(channel)
            await first.send(_body_headers(_Mode.STREAMED), then=1)
            await first.send(_body(b"123456", False), then=0)
            await second.send(_body_headers(_Mode.STREAMED), then=1)
            assert await second.send(_body(b"123456", False), then=1) == [
                "immediate_response"
            ]
            # The first request goes away without finishing its body.
            first.call.cancel()

            async with asyncio.timeout(5):
                while True:
                    third = _Live(channel)
                    await third.send(_body_headers(_Mode.STREAMED), then=1)
                    released = await third.send(_body(b"12345678", True), then=1)
                    if released == ["request_body"]:
                        break
            await third.finish()
    finally:
        await srv.stop(None)

    assert "cannot judge a request's body (over-budget)" in caplog.text
    assert judge.bodies[-1] == b"12345678"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("on_failure", "parts"),
    [
        (Deny(), ["immediate_response"]),
        (Allow(), ["request_headers", "request_body", "request_body"]),
    ],
    ids=["refused-by-default", "forwarded-when-the-setting-allows"],
)
async def test_a_failure_while_reading_the_body_follows_the_setting(
    monkeypatch, socket_path, caplog, on_failure, parts
):
    """Before the verdict, so Q3: what was held is refused, or released."""
    real = ext_proc._Stream._read
    calls = []

    def flaky(self, message, part):
        calls.append(part)
        if len(calls) == 2:
            raise ValueError(_SECRET)
        return real(self, message, part)

    monkeypatch.setattr(ext_proc._Stream, "_read", flaky)
    messages = [
        _body_headers(_Mode.FULL_DUPLEX_STREAMED),
        _body(b"one,", False),
        _body(b"two", True),
    ]
    srv = await _start(
        monkeypatch,
        socket_path,
        judging=_judging(_BodyJudge(), on_failure=on_failure),
    )
    try:
        with caplog.at_level("DEBUG"):
            responses = await _process(socket_path, messages)
    finally:
        await srv.stop(None)

    assert [_part(r) for r in responses] == parts
    assert "could not judge a request" in caplog.text
    assert "agent-secret" not in caplog.text
