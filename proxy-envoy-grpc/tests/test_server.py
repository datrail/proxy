"""Tests for the gRPC server, over a real channel on a unix socket."""

import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import grpc
import pytest

from core_support import wound_holder
from envoy_support import (
    BodyJudge,
    apply_check_response,
    get_pseudo_headers,
    send_check,
    set_protected_hosts,
)
from proxy.core import lifecycle
from proxy.core.settings import ConfigError
from proxy.core.xrail_auth import TicketHolder, Token
from proxy.envoy_grpc import pipeline, probe_health, server


async def _check(path: Path, target: str, headers: dict[str, str]):
    return await send_check(path, {**get_pseudo_headers(target), **headers})


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
async def test_requests_are_answered_before_the_first_fetch(monkeypatch, socket_path):
    """The socket is bound at once, and until the first fetch lands a protected
    request gets `x-rail-status: issuer-unreachable`."""
    source = _GatedSource()
    holder = TicketHolder(source)

    async with lifecycle.running(holder, wait_for_first_fetch=False):
        srv = await server._start_server(
            holder, True, set_protected_hosts(monkeypatch), socket_path
        )
        try:
            before = await _check(socket_path, "mcp.example.com", {})
            source.gate.set()
            await asyncio.wait_for(_wait_for_ticket(holder), 5)
            after = await _check(socket_path, "mcp.example.com", {})
        finally:
            await srv.stop(None)

    assert apply_check_response({}, before)["x-rail-status"] == "issuer-unreachable"
    assert "x-rail" not in apply_check_response({}, before)
    assert apply_check_response({}, after)["x-rail"] == "the-ticket"


async def _wait_for_ticket(holder: TicketHolder) -> None:
    while holder.snapshot()[0] is None:
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_stale_socket_file_is_replaced(monkeypatch, socket_path):
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(socket_path))
    stale.close()
    assert socket_path.is_socket()

    srv = await server._start_server(
        None, False, set_protected_hosts(monkeypatch), socket_path
    )
    try:
        response = await _check(socket_path, "open.example.com", {})
    finally:
        await srv.stop(None)

    assert response.status.code == 0


@pytest.mark.asyncio
async def test_the_socket_is_for_its_user_and_group_only(monkeypatch, socket_path):
    srv = await server._start_server(
        None, False, set_protected_hosts(monkeypatch), socket_path
    )
    try:
        mode = socket_path.stat().st_mode & 0o777
    finally:
        await srv.stop(None)

    assert mode == 0o660


@pytest.mark.asyncio
async def test_a_file_that_is_not_a_socket_is_left_alone(monkeypatch, socket_path):
    socket_path.write_text("not ours")

    with pytest.raises(ConfigError, match="not a socket"):
        await server._start_server(
            None, False, set_protected_hosts(monkeypatch), socket_path
        )

    assert socket_path.read_text() == "not ours"


@pytest.mark.asyncio
async def test_a_socket_in_a_missing_directory_is_a_configuration_error(
    monkeypatch, socket_path
):
    missing = socket_path.parent / "missing" / "ext.sock"

    with pytest.raises(ConfigError, match=f"cannot bind {missing}"):
        await server._start_server(
            None, False, set_protected_hosts(monkeypatch), missing
        )


@contextlib.contextmanager
def _mode(path: Path, mode: int):
    """`path` with `mode` for the block, restored so the directory can be
    cleaned up."""
    before = path.stat().st_mode & 0o777
    path.chmod(mode)
    try:
        yield
    finally:
        path.chmod(before)


_AS_ROOT = pytest.mark.skipif(
    os.geteuid() == 0, reason="root is not stopped by permissions"
)


@_AS_ROOT
@pytest.mark.asyncio
async def test_a_directory_it_cannot_search_is_a_configuration_error(
    monkeypatch, socket_path
):
    locked = socket_path.parent / "locked"
    locked.mkdir()
    path = locked / "ext.sock"

    with (
        _mode(locked, 0o000),
        pytest.raises(ConfigError, match=f"cannot use {path}: Permission denied"),
    ):
        await server._start_server(None, False, set_protected_hosts(monkeypatch), path)


@_AS_ROOT
@pytest.mark.asyncio
async def test_a_stale_socket_it_cannot_remove_is_a_configuration_error(
    monkeypatch, socket_path
):
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(socket_path))
    stale.close()

    with (
        _mode(socket_path.parent, 0o500),
        pytest.raises(
            ConfigError, match=f"cannot use {socket_path}: Permission denied"
        ),
    ):
        await server._start_server(
            None, False, set_protected_hosts(monkeypatch), socket_path
        )

    assert socket_path.is_socket()


@pytest.mark.parametrize(
    ("env", "error"),
    [
        pytest.param(
            {"RAIL_PROXY_PROTECTED_HOSTS": "ftp://mcp.example.com"},
            "has a scheme other than http:// or https://",
            id="bad-entry",
        ),
        pytest.param(
            {
                "RAIL_PLUGIN_ENABLED": "true",
                "RAIL_PROXY_PROTECTED_HOSTS": "mcp.example.com",
            },
            "a Rail Center to fetch one from is required",
            id="plugin-on-without-rail-center",
        ),
        pytest.param(
            {"RAIL_PLUGIN_ENABLED": "maybe"}, "is not true or false", id="bad-flag"
        ),
        pytest.param(
            {"RAIL_PROXY_ON_JUDGE_FAILURE": "refuse"},
            "is not deny or allow",
            id="bad-judge-failure",
        ),
        pytest.param(
            {"RAIL_PROXY_MAX_BODY_BYTES": "8MiB"},
            "is not a positive number of bytes",
            id="bad-body-cap",
        ),
        pytest.param(
            {"RAIL_PROXY_ALLOW_UNREADABLE_BODY": "too-big"},
            "too-big is not a reason",
            id="bad-body-reason",
        ),
        pytest.param(
            {"RAIL_PROXY_BODY_TIMEOUT_SECONDS": "150ms"},
            "is not a positive number of seconds",
            id="bad-body-timeout",
        ),
    ],
)
def test_a_configuration_error_exits_2(monkeypatch, caplog, env, error):
    """Each case is refused for its own error, not for one that comes first."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert server.main() == 2
    assert error in caplog.text


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
        holder, enabled, set_protected_hosts(monkeypatch), socket_path
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
        wound_holder(ticket="the-ticket"),
        True,
        set_protected_hosts(monkeypatch),
        socket_path,
    )
    try:
        with caplog.at_level("DEBUG"):
            await _check(socket_path, "mcp.example.com", {"x-rail": "forged"})
            await _check(socket_path, "open.example.com", {})
    finally:
        await srv.stop(None)

    assert "injected x-rail" in caplog.text
    assert "the-ticket" not in caplog.text


def test_the_process_binds_before_the_first_fetch_and_stops_on_sigterm(socket_path):
    """With a Rail Center that never answers and a 30s fetch timeout, the
    socket is up and answering within seconds, and SIGTERM still stops the
    process cleanly while the fetch is pending."""
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
        code = process.wait(timeout=10)
    finally:
        process.kill()
        silent.close()

    assert probe.returncode == 0, probe.stderr
    ticket = json.loads(probe.stdout)["ticket"]
    assert ticket["unavailable_reason"] == "issuer-unreachable"
    output = process.stdout.read()
    assert code == 0, output
    assert "Traceback" not in output
    assert "proxy.envoy_grpc.server: stopping" in output
    assert "__main__" not in output


@pytest.mark.asyncio
async def test_only_protected_requests_are_logged(monkeypatch, socket_path, caplog):
    """A missing ticket is warned about for requests that would have carried
    it, not for the agent's other traffic."""
    srv = await server._start_server(
        wound_holder(reason="not-found"),
        True,
        set_protected_hosts(monkeypatch),
        socket_path,
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


@pytest.mark.asyncio
async def test_a_body_past_grpc_s_default_message_size_is_received(
    monkeypatch, socket_path
):
    """gRPC refuses a message over 4 MiB by default; the server takes one
    as large as the cap and its headers."""
    judge = BodyJudge()
    body = b"x" * (5 * 1024 * 1024)
    srv = await server._start_server(
        wound_holder(ticket="the-ticket"),
        True,
        set_protected_hosts(monkeypatch),
        socket_path,
        pipeline.Judging(judge=judge),
    )
    try:
        response = await send_check(
            socket_path, get_pseudo_headers("mcp.example.com"), body
        )
    finally:
        await srv.stop(None)

    assert judge.bodies == [body]
    assert apply_check_response({}, response)["x-rail"] == "the-ticket"
