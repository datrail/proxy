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

CONFIG_FILE = Path(
    os.environ.get(
        "RAIL_PROXY_CONFIG_FILE",
        Path(__file__).resolve().parent / "bridge.yaml",
    )
)
PORT = int(os.environ.get("RAIL_PROXY_PORT", "8091"))
BIND = os.environ.get("RAIL_PROXY_BIND", "0.0.0.0")

logging.basicConfig(
    level=os.environ.get("RAIL_PROXY_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("fastmcp_proxy")


def load_servers() -> list[dict[str, Any]]:
    """Read `mcp.servers` from the config file.

    Exits rather than serving a configuration nobody meant: the image bakes
    `RAIL_PROXY_CONFIG_FILE` to a path that holds no file, so a container
    started without a mounted config stops here instead of coming up with no
    upstream and answering every tool list with nothing.
    """
    if not CONFIG_FILE.exists():
        log.error("config not found: %s", CONFIG_FILE)
        sys.exit(2)
    data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    servers = [
        s
        for s in (data.get("mcp") or {}).get("servers") or []
        if isinstance(s, dict) and s.get("name") and s.get("url")
    ]
    if not servers:
        log.error("no MCP servers in %s", CONFIG_FILE)
        sys.exit(2)
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


#: Statuses FastMCP answers `GET /mcp` with, both reachable on the pinned
#: version: 400 when no session id is supplied, 404 when one is and no session
#: matches. Neither is what a client is entitled to expect.
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

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not (
            scope["type"] == "http"
            and scope["method"] == "GET"
            and scope["path"].startswith("/mcp")
        ):
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


async def main() -> int:
    import uvicorn

    app = build_app()
    log.info("listening on %s:%d", BIND, PORT)
    await uvicorn.Server(uvicorn.Config(app, host=BIND, port=PORT)).serve()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
