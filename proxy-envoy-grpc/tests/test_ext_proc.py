"""The ext_proc adapter, over a real stream on a unix socket."""

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
from proxy.core.judge import Allow, AllowAll, Deny
from proxy.envoy_grpc import ext_proc, server
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
    headers: dict[str, str], *, end_of_stream=True, body_mode=None
) -> ep.ProcessingRequest:
    message = ep.ProcessingRequest(
        request_headers=ep.HttpHeaders(end_of_stream=end_of_stream)
    )
    message.request_headers.headers.headers.extend(
        HeaderValue(key=k, raw_value=v.encode()) for k, v in headers.items()
    )
    if body_mode is not None:
        message.protocol_config.request_body_mode = body_mode
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
        monkeypatch, socket_path, judge=AllowAll(), on_judge_failure=Deny()
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
        monkeypatch, socket_path, judge=AllowAll(), on_judge_failure=Allow()
    )
    try:
        [response] = await _process(socket_path, [_headers(_pseudo("mcp.example.com"))])
    finally:
        await srv.stop(None)

    assert _applied({}, response)["x-rail"] == "the-ticket"
