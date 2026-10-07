"""Tests for the gRPC server, over a real channel on a unix socket."""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import grpc
import pytest
from envoy.config.core.v3.base_pb2 import HeaderValueOption
from envoy.service.auth.v3 import external_auth_pb2, external_auth_pb2_grpc

from core_support import wound_holder, xrail_params
from proxy.core import lifecycle
from proxy.core.settings import ConfigError
from proxy.core.xrail_auth import TicketHolder, Token
from proxy.envoy_grpc import probe_health, server
from proxy.envoy_grpc import settings as envoy_settings

_ORDINARY = {
    "host": "mcp.example.com",
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "user-agent": "agent/1",
    "x-trace": "abc",
}


@pytest.fixture
def socket_path():
    # A unix socket path is limited to about 100 bytes, which pytest's own
    # tmp_path can exceed.
    with tempfile.TemporaryDirectory(prefix="rail-") as directory:
        yield Path(directory) / "ext.sock"


def _protected(monkeypatch, value="mcp.example.com"):
    monkeypatch.setenv("RAIL_PROXY_PROTECTED_HOSTS", value)
    return envoy_settings.get_protected_hosts(plugin_on=False)


async def _check(path: Path, target: str, headers: dict[str, str]):
    request = external_auth_pb2.CheckRequest()
    http = request.attributes.request.http
    http.host = target
    http.headers.update({":authority": target, ":path": "/mcp", **headers})
    async with grpc.aio.insecure_channel(f"unix:{path}") as channel:
        return await external_auth_pb2_grpc.AuthorizationStub(channel).Check(request)


def _applied(agent: dict[str, str], response) -> dict[str, str]:
    """The agent's headers with the response applied, as Envoy applies it."""
    ok = response.ok_response
    sent = {k: v for k, v in agent.items() if k not in set(ok.headers_to_remove)}
    sent.update({h.header.key: h.header.value for h in ok.headers})
    return sent


class _GatedSource:
    """A source whose fetch answers only once the test opens the gate."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()

    def describe(self) -> str:
        return "https://rc.invalid/v1/tickets (unauthenticated)"

    async def fetch(self) -> Token:
        await self.gate.wait()
        return Token("the-ticket", time.time() + 3600)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", xrail_params("envoy-grpc"))
async def test_every_row_of_the_contract(monkeypatch, socket_path, case):
    srv = await server._start_server(
        case.holder(), case.plugin, _protected(monkeypatch), socket_path
    )
    try:
        agent = {**_ORDINARY, **case.agent_headers}
        response = await _check(socket_path, "mcp.example.com", agent)
    finally:
        await srv.stop(None)

    assert response.status.code == 0
    sent = _applied(agent, response)
    for name, value in case.expected.items():
        assert sent[name] == value
    for name, value in case.forwarded.items():
        assert sent.get(name) == value
    assert not set(sent) & case.absent
    agents_own = {k: v for k, v in agent.items() if not k.startswith("x-rail")}
    assert agents_own.items() <= sent.items()


@pytest.mark.asyncio
async def test_headers_are_overwritten_not_appended(monkeypatch, socket_path):
    srv = await server._start_server(
        wound_holder(ticket="the-ticket"), True, _protected(monkeypatch), socket_path
    )
    try:
        response = await _check(socket_path, "mcp.example.com", {})
    finally:
        await srv.stop(None)

    actions = {h.append_action for h in response.ok_response.headers}
    assert actions == {HeaderValueOption.OVERWRITE_IF_EXISTS_OR_ADD}


@pytest.mark.asyncio
async def test_an_unprotected_request_is_answered_ok_with_no_changes(
    monkeypatch, socket_path
):
    srv = await server._start_server(
        wound_holder(ticket="the-ticket"), True, _protected(monkeypatch), socket_path
    )
    try:
        response = await _check(socket_path, "open.example.com", {"x-rail": "x"})
    finally:
        await srv.stop(None)

    assert response.status.code == 0
    assert not response.ok_response.headers
    assert not response.ok_response.headers_to_remove


@pytest.mark.asyncio
async def test_a_failure_while_deciding_is_answered_ok_with_no_changes(
    monkeypatch, socket_path, caplog
):
    """Envoy then forwards the request as if this server were down. The log has
    the traceback, but no header value."""

    secret = "Bearer agent-secret"

    def broken(*_args):
        # The message is a header value, as an exception's message can be.
        raise ValueError(secret)

    monkeypatch.setattr(server, "decide", broken)
    srv = await server._start_server(
        wound_holder(ticket="the-ticket"), True, _protected(monkeypatch), socket_path
    )
    try:
        with caplog.at_level("DEBUG"):
            response = await _check(
                socket_path, "mcp.example.com", {"authorization": secret}
            )
    finally:
        await srv.stop(None)

    assert response.status.code == 0
    assert not response.ok_response.headers
    assert not response.ok_response.headers_to_remove
    assert "could not decide" in caplog.text
    assert "in broken" in caplog.text  # the traceback's frame
    assert "agent-secret" not in caplog.text


@pytest.mark.asyncio
async def test_requests_are_answered_before_the_first_fetch(monkeypatch, socket_path):
    """The socket is bound at once, and until the first fetch lands a protected
    request gets `x-rail-status: issuer-unreachable`."""
    source = _GatedSource()
    holder = TicketHolder(source)

    async with lifecycle.running(holder, wait_for_first_fetch=False):
        srv = await server._start_server(
            holder, True, _protected(monkeypatch), socket_path
        )
        try:
            before = await _check(socket_path, "mcp.example.com", {})
            source.gate.set()
            await asyncio.wait_for(_ticket_held(holder), 5)
            after = await _check(socket_path, "mcp.example.com", {})
        finally:
            await srv.stop(None)

    assert _applied({}, before)["x-rail-status"] == "issuer-unreachable"
    assert "x-rail" not in _applied({}, before)
    assert _applied({}, after)["x-rail"] == "the-ticket"


async def _ticket_held(holder: TicketHolder) -> None:
    while holder.snapshot()[0] is None:
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_stale_socket_file_is_replaced(monkeypatch, socket_path):
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(socket_path))
    stale.close()
    assert socket_path.is_socket()

    srv = await server._start_server(None, False, _protected(monkeypatch), socket_path)
    try:
        response = await _check(socket_path, "open.example.com", {})
    finally:
        await srv.stop(None)

    assert response.status.code == 0


@pytest.mark.asyncio
async def test_the_socket_is_for_its_user_and_group_only(monkeypatch, socket_path):
    srv = await server._start_server(None, False, _protected(monkeypatch), socket_path)
    try:
        mode = socket_path.stat().st_mode & 0o777
    finally:
        await srv.stop(None)

    assert mode == 0o660


@pytest.mark.asyncio
async def test_a_file_that_is_not_a_socket_is_left_alone(monkeypatch, socket_path):
    socket_path.write_text("not ours")

    with pytest.raises(ConfigError, match="not a socket"):
        await server._start_server(None, False, _protected(monkeypatch), socket_path)

    assert socket_path.read_text() == "not ours"


@pytest.mark.parametrize(
    "env",
    [
        {"RAIL_PROXY_PROTECTED_HOSTS": "ftp://mcp.example.com"},
        {"RAIL_PLUGIN_ENABLED": "true"},
        {"RAIL_PLUGIN_ENABLED": "maybe"},
    ],
    ids=["bad-entry", "plugin-on-without-rail-center", "bad-flag"],
)
def test_a_configuration_error_exits_2(monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert server.main() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("holder", "ticket"),
    [
        (None, None),
        (TicketHolder(_GatedSource()), None),
        (wound_holder(ticket="the-ticket"), "held"),
    ],
    ids=["plugin-off", "before-first-fetch", "ticket-held"],
)
async def test_the_status_method(monkeypatch, socket_path, holder, ticket):
    """Answered whether or not a ticket is held, and never with the ticket."""
    enabled = holder is not None
    srv = await server._start_server(
        holder, enabled, _protected(monkeypatch), socket_path
    )
    try:
        async with grpc.aio.insecure_channel(f"unix:{socket_path}") as channel:
            raw = await channel.unary_unary(server.STATUS_METHOD)(b"")
        monkeypatch.setenv("RAIL_PROXY_EXT_SOCKET", str(socket_path))
        probe = await asyncio.to_thread(probe_health.main)
    finally:
        await srv.stop(None)

    payload = json.loads(raw)
    assert payload["status"] == "ok"
    assert payload["plugin_enabled"] is enabled
    assert (payload["ticket"] is None) is (holder is None)
    assert "the-ticket" not in raw.decode()
    assert probe == 0


def test_the_probe_exits_1_with_no_server(monkeypatch, socket_path, capsys):
    monkeypatch.setenv("RAIL_PROXY_EXT_SOCKET", str(socket_path))

    assert probe_health.main() == 1
    assert "no answer" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_the_ticket_is_never_logged(monkeypatch, socket_path, caplog):
    srv = await server._start_server(
        wound_holder(ticket="the-ticket"), True, _protected(monkeypatch), socket_path
    )
    try:
        with caplog.at_level("DEBUG"):
            await _check(socket_path, "mcp.example.com", {"x-rail": "forged"})
            await _check(socket_path, "open.example.com", {})
    finally:
        await srv.stop(None)

    assert "injected x-rail" in caplog.text
    assert "the-ticket" not in caplog.text


def test_sigterm_stops_the_process_cleanly(socket_path):
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("RAIL_")},
        "RAIL_PROXY_EXT_SOCKET": str(socket_path),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "proxy.envoy_grpc"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not socket_path.is_socket():
            assert process.poll() is None, process.stdout.read()
            assert time.monotonic() < deadline, "the socket never appeared"
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        code = process.wait(timeout=10)
    finally:
        process.kill()

    output = process.stdout.read()
    assert code == 0, output
    assert "Traceback" not in output
    assert "proxy.envoy_grpc.server: stopping" in output
    assert "__main__" not in output


def test_the_process_binds_before_the_first_fetch(socket_path):
    """With a Rail Center that never answers and a 30s fetch timeout, the
    socket is up and answering within seconds."""
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("RAIL_")},
        "RAIL_PROXY_EXT_SOCKET": str(socket_path),
        "RAIL_PLUGIN_ENABLED": "true",
        "RAIL_CENTER_URL": f"http://127.0.0.1:{silent.getsockname()[1]}",
        "RAIL_HOST_ID": "h",
        "RAIL_SANDBOX_NAME": "s",
        "RAIL_PROXY_TICKET_TIMEOUT_SECONDS": "30",
        "RAIL_PROXY_PROTECTED_HOSTS": "mcp.example.com",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "proxy.envoy_grpc"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not socket_path.is_socket():
            assert process.poll() is None, process.stdout.read()
            assert time.monotonic() < deadline, "the socket waited for the fetch"
            time.sleep(0.05)
        probe = subprocess.run(
            [sys.executable, "-m", "proxy.envoy_grpc.probe_health"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)
    finally:
        process.kill()
        silent.close()

    assert probe.returncode == 0, probe.stderr
    ticket = json.loads(probe.stdout)["ticket"]
    assert ticket["unavailable_reason"] == "issuer-unreachable"


@pytest.mark.asyncio
async def test_only_protected_requests_are_logged(monkeypatch, socket_path, caplog):
    """A missing ticket is warned about for requests that would have carried
    it, not for the agent's other traffic."""
    srv = await server._start_server(
        wound_holder(reason="not-found"), True, _protected(monkeypatch), socket_path
    )
    try:
        with caplog.at_level("DEBUG"):
            await _check(socket_path, "open.example.com", {})
            unprotected = caplog.text
            await _check(socket_path, "mcp.example.com", {})
    finally:
        await srv.stop(None)

    assert "no valid ticket" not in unprotected
    assert "no valid ticket" in caplog.text
