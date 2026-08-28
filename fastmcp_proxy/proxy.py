"""Receive MCP calls from an agent and forward them to a configured upstream.

The proxy is a sidecar: one process serves one agent, mounting the upstream
named in its config file and re-exposing that upstream's tools under a
namespace. Attaching the `x-rail` ticket is the point of the component and is
not implemented here — this module is the path a request travels.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_CONFIG_FILE = Path(__file__).resolve().parent / "bridge.yaml"
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
log = logging.getLogger("fastmcp_proxy")


class ConfigError(Exception):
    """The configuration cannot be served. Raised rather than exited: this
    module is importable, and a library that calls `sys.exit` takes its caller
    down past every `except Exception` between them. `main()` is where this
    becomes the container's exit code."""


def config_file() -> Path:
    """Where the upstream list is read from, resolved per call.

    Read here rather than bound at import, so the environment a process is given
    is the environment it uses. An empty value — what an unset compose
    interpolation yields — falls back rather than becoming `Path("")`, which is
    the current directory and passes an existence check.
    """
    raw = os.environ.get("RAIL_PROXY_CONFIG_FILE", "").strip()
    return Path(raw) if raw else DEFAULT_CONFIG_FILE


def load_servers() -> list[dict[str, Any]]:
    """Read `mcp.servers` from the config file.

    Raises rather than serving a configuration nobody meant: the image bakes
    `RAIL_PROXY_CONFIG_FILE` to a path that holds no file, so a container
    started without a mounted config stops here instead of coming up with no
    upstream and answering every tool list with nothing. `main` turns this into
    exit 2.
    """
    path = config_file()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} is not valid UTF-8") from exc

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    # Each level is checked rather than assumed: a hand-edited file goes wrong
    # in more shapes than an empty one, and `.get` on a list is a traceback
    # where a sentence would do.
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a mapping, not {type(data).__name__}")
    mcp = data.get("mcp") or {}
    if not isinstance(mcp, dict):
        raise ConfigError(f"{path}: `mcp` must be a mapping, not {type(mcp).__name__}")
    entries = mcp.get("servers") or []
    if not isinstance(entries, list):
        raise ConfigError(
            f"{path}: `mcp.servers` must be a list, not {type(entries).__name__}"
        )

    servers = [
        e for e in entries if isinstance(e, dict) and e.get("name") and e.get("url")
    ]
    if not servers:
        raise ConfigError(f"{path} names no upstream with both a name and a url")

    # The transport rejects a url it cannot use by raising ValueError from the
    # mount loop, which is past every handler and lands as a traceback and exit
    # 1. `gateway:8080/mcp` — the packaged example minus its scheme — is the
    # likeliest hand-edit of this file, so it is checked where the rest of the
    # file's mistakes are reported.
    for entry in servers:
        url = str(entry["url"])
        if not url.startswith(("http://", "https://")):
            raise ConfigError(
                f"{path}: upstream {entry['name']!r} has no http:// or https:// "
                f"scheme: {url!r}"
            )
    return servers


def upstream_timeout() -> float:
    """Seconds to wait on an upstream, for one request and for the handshake.

    Without it a silent upstream costs a tool call the transport's 300-second
    read default, twice over, and an upstream that answers malformedly hangs the
    call indefinitely — the exchange completes and the wait moves to a protocol
    layer with no deadline of its own. An agent has no way to tell either from a
    slow tool.
    """
    raw = os.environ.get("RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return 30.0
    try:
        value = float(raw)
    except ValueError:
        log.warning("RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS=%r is not a number", raw)
        return 30.0
    return value if value > 0 else 30.0


def build_gateway() -> FastMCP:
    """Mount every configured upstream under one endpoint.

    A mount's name becomes the prefix on every tool it re-exposes, so the
    agent sees `<name>_<tool>`. Renaming a server renames every tool the
    agents were prompted with.
    """
    gateway = FastMCP(name="datrail-proxy")

    for srv in load_servers():
        timeout = upstream_timeout()
        transport = StreamableHttpTransport(url=srv["url"])
        proxy = create_proxy(
            Client(transport, timeout=timeout, init_timeout=timeout),
            name=f"proxy-{srv['name']}",
            # Default is "warn", which turns an unreachable upstream into an
            # empty tool list — so an agent is told the tool does not exist
            # rather than that the thing behind it is down, and does not retry.
            provider_error_strategy="raise",
        )

        # `create_proxy` turns on incoming-header forwarding, which is wrong for
        # this component in the one way that matters: the sandbox could set
        # `x-rail` itself and have it arrive upstream unchanged, so the identity
        # the proxy exists to assert would be supplied by the caller it exists
        # to identify. `authorization` rides the same path, re-included by
        # fastmcp rather than stripped. The proxy is the boundary; nothing the
        # agent sends crosses it.
        transport.forward_incoming_headers = False

        gateway.mount(proxy, namespace=srv["name"])
        log.info("mounted '%s' -> %s", srv["name"], srv["url"])

    @gateway.custom_route("/health", methods=["GET"])
    async def health(_request):  # pragma: no cover - exercised over HTTP
        """Liveness only: the process is up and its config parsed.

        It reports nothing about whether a ticket is held, because no ticket
        is fetched here. Anything waiting on this to decide an agent may start
        is waiting on the wrong signal.
        """
        return JSONResponse({"status": "ok"})

    return gateway


class McpMethodCompat:
    """Answer 405 to methods the MCP endpoint does not serve.

    A server that does not offer the optional server-to-client SSE stream must
    refuse `GET /mcp` with `405 Method Not Allowed`; a client then skips the
    stream and carries on. FastMCP answers 400 or 404 instead, and a client
    following the transport spec treats anything but 405 as fatal — so without
    this the session ends before the first tool call. Answering here rather than
    rewriting a forwarded status also covers `/mcp/`, which redirects with a 307
    before any status worth rewriting exists.

    Answering also avoids allocating a session per request for these methods.
    That is a saving, **not a protection**: POST must pass through, because it
    is how a session is created, and an unauthenticated garbage POST allocates a
    transport that is never reclaimed just the same. Bounding that is FastMCP's
    to do — its session manager takes an idle timeout that `http_app()` does not
    expose — and reaching into the manager to set it would couple this to
    internals that change between releases. It is recorded as a limitation
    rather than papered over, and it is one more thing that authenticating the
    agent-to-proxy hop would close.
    """

    _RESPONSE = (
        b'{"jsonrpc":"2.0","error":{"code":-32600,'
        b'"message":"This endpoint accepts POST, and DELETE for a session it '
        b'issued."},"id":null}'
    )

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _answer_here(scope: Scope) -> bool:
        """Anything at the MCP endpoint that opens no session and cannot use one.

        POST is how a session is created, so it always passes through. Every
        other method — GET for the stream, and HEAD, OPTIONS, PUT, DELETE,
        PATCH — is answered here, because forwarding one makes FastMCP allocate
        a transport before deciding it is a 405, and nothing reclaims a
        transport that never handshakes. Gating on GET alone left every other
        verb leaking: 5,000 HEADs took the container from 61 to 267 MiB.

        A request carrying a session id passes through whatever its method: a
        404 then means the session has ended, and DELETE with one is how a
        client terminates its own session.

        The path is matched exactly rather than by prefix: `startswith("/mcp")`
        also catches `/mcp-admin` and `/mcpfoo`, and answering those
        `405 Allow: …` asserts something about a route that does not exist.
        """
        if scope["type"] != "http" or scope["method"] == "POST":
            return False
        if scope["path"] not in ("/mcp", "/mcp/"):
            return False
        return not any(
            k.lower() == b"mcp-session-id" for k, _ in scope.get("headers", [])
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._answer_here(scope):
            await self.app(scope, receive, send)
            return

        await send(
            {
                "type": "http.response.start",
                "status": 405,
                "headers": [
                    (b"allow", b"POST, DELETE"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(self._RESPONSE)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": self._RESPONSE})


def build_app() -> ASGIApp:
    """The ASGI application, wrapped in the compatibility shim."""
    return McpMethodCompat(build_gateway().http_app(transport="streamable-http"))


#: uvicorn resolves a level by dict lookup and raises KeyError on a miss, so its
#: vocabulary is the narrower of the two and the one to validate against.
#: `logging` also accepts WARN, FATAL and NOTSET, which would pass a check
#: against `logging` alone and then kill the server after the mount lines had
#: already been logged — a configuration typo presenting as a startup failure.
_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "TRACE"}
_LEVEL_ALIASES = {"WARN": "WARNING", "FATAL": "CRITICAL"}


def log_level() -> str:
    """The configured level, or INFO. Pure: it neither warns nor configures.

    Empty is unset, as it is for the config path and the port.
    """
    level = os.environ.get("RAIL_PROXY_LOG_LEVEL", "").strip().upper() or "INFO"
    level = _LEVEL_ALIASES.get(level, level)
    return level if level in _LEVELS else "INFO"


def configure_logging() -> None:
    """Apply RAIL_PROXY_LOG_LEVEL, falling back rather than refusing to start.

    An unusable level is worth a complaint, not an exit: the proxy's job does
    not depend on it, and dying over a log setting loses the traffic too.
    """
    resolved = log_level()
    # TRACE is uvicorn's alone; `logging` has no such level, so the root logger
    # takes the most verbose one it knows.
    logging.basicConfig(
        level="DEBUG" if resolved == "TRACE" else resolved, format=_LOG_FORMAT
    )
    raw = os.environ.get("RAIL_PROXY_LOG_LEVEL", "").strip()
    if raw and raw.upper() not in _LEVELS and raw.upper() not in _LEVEL_ALIASES:
        log.warning("RAIL_PROXY_LOG_LEVEL=%r is not a level; using INFO", raw)


async def main() -> int:
    import uvicorn

    configure_logging()
    try:
        app = build_app()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    bind = os.environ.get("RAIL_PROXY_BIND", "0.0.0.0")
    raw_port = os.environ.get("RAIL_PROXY_PORT", "").strip() or "8091"
    try:
        port = int(raw_port)
    except ValueError:
        log.error("RAIL_PROXY_PORT is not a number: %r", raw_port)
        return 2
    # Reported rather than left to bind(), which raises OverflowError past every
    # handler. 0 is excluded deliberately: it binds an ephemeral port, so the
    # process comes up somewhere nothing is configured to look.
    if not 1 <= port <= 65535:
        log.error("RAIL_PROXY_PORT is out of range: %d", port)
        return 2

    # uvicorn logs the bind once it has one. Announcing it here would name an
    # address the process may never get.
    # uvicorn installs its own loggers, so `basicConfig` alone leaves the access
    # log and the startup lines at INFO whatever the variable said.
    await uvicorn.Server(
        uvicorn.Config(
            app,
            host=bind,
            port=port,
            log_level=log_level().lower(),
            # Without this uvicorn stops immediately and an in-flight tool call
            # is dropped with no response, leaving the agent waiting on an
            # answer that will never come. A routine restart should not wedge
            # the thing this proxy exists to serve.
            timeout_graceful_shutdown=int(upstream_timeout()),
        )
    ).serve()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
