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

try:
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.server import create_proxy
except ImportError:  # pragma: no cover - dependency missing, not a code path
    print(
        "error: fastmcp not installed. install via:\n    pip install -r requirements.txt",
        file=sys.stderr,
    )
    sys.exit(1)

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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

    Exits rather than serving a configuration nobody meant: the image bakes
    `RAIL_PROXY_CONFIG_FILE` to a path that holds no file, so a container
    started without a mounted config stops here instead of coming up with no
    upstream and answering every tool list with nothing.
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
    return servers


def build_gateway() -> FastMCP:
    """Mount every configured upstream under one endpoint.

    A mount's name becomes the prefix on every tool it re-exposes, so the
    agent sees `<name>_<tool>`. Renaming a server renames every tool the
    agents were prompted with.
    """
    gateway = FastMCP(name="datrail-proxy")

    for srv in load_servers():
        transport = StreamableHttpTransport(url=srv["url"])
        proxy = create_proxy(Client(transport), name=f"proxy-{srv['name']}")

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


#: What FastMCP answers a stream probe with: 400 on the pinned version, 404 kept
#: because the two have swapped between releases. A 404 answering a request that
#: *did* carry a session id means something else — the session is gone and the
#: client should open a new one — which is why `_is_stream_probe` excludes it.
_REWRITE_STATUSES = {400, 404}


class McpGetStatusCompat:
    """Answer `GET /mcp` with 405, which is what the transport requires.

    A server that does not offer the optional server-to-client SSE stream is
    supposed to refuse the GET with `405 Method Not Allowed`; a client then
    skips the stream and carries on. FastMCP answers 400 or 404 instead, and a
    client that follows the spec treats anything but 405 as fatal — so without
    this the session ends before the first tool call.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _is_stream_probe(scope: Scope) -> bool:
        """A GET at the MCP endpoint itself, offering no session id.

        Matched exactly rather than by prefix: `startswith("/mcp")` also catches
        `/mcp-admin` and `/mcpfoo`, and answering those `405 Allow: POST`
        asserts that posting to them would work — a claim about a route that
        does not exist.

        A request carrying a session id is excluded, because a 404 then means
        the session has ended; answering 405 leaves that client reusing a dead
        session instead of opening a new one.
        """
        if scope["type"] != "http" or scope["method"] != "GET":
            return False
        if scope["path"] not in ("/mcp", "/mcp/"):
            return False
        return not any(
            k.lower() == b"mcp-session-id" for k, _ in scope.get("headers", [])
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._is_stream_probe(scope):
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and message["status"] in _REWRITE_STATUSES
            ):
                message["status"] = 405
                message["headers"] = [*message.get("headers", []), (b"allow", b"POST")]
            await send(message)

        await self.app(scope, receive, send_wrapper)


def build_app() -> ASGIApp:
    """The ASGI application, wrapped in the compatibility shim."""
    return McpGetStatusCompat(build_gateway().http_app(transport="streamable-http"))


def configure_logging() -> None:
    """Apply RAIL_PROXY_LOG_LEVEL, falling back rather than refusing to start.

    An unusable level is worth a complaint, not an exit: the proxy's job does
    not depend on it, and dying over a log setting loses the traffic too.
    """
    level = os.environ.get("RAIL_PROXY_LOG_LEVEL", "INFO").upper()
    if level not in logging.getLevelNamesMapping():
        logging.basicConfig(level="INFO", format=_LOG_FORMAT)
        log.warning("RAIL_PROXY_LOG_LEVEL=%r is not a level; using INFO", level)
        return
    logging.basicConfig(level=level, format=_LOG_FORMAT)


async def main() -> int:
    import uvicorn

    configure_logging()
    try:
        app = build_app()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    bind = os.environ.get("RAIL_PROXY_BIND", "0.0.0.0")
    raw_port = os.environ.get("RAIL_PROXY_PORT", "8091")
    try:
        port = int(raw_port)
    except ValueError:
        log.error("RAIL_PROXY_PORT is not a number: %r", raw_port)
        return 2

    # uvicorn logs the bind once it has one. Announcing it here would name an
    # address the process may never get.
    await uvicorn.Server(uvicorn.Config(app, host=bind, port=port)).serve()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
