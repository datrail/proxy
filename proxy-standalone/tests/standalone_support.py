"""Helpers for the proxy-standalone suite: a recording MCP upstream."""

import json
from typing import Any

import httpx

MCP_ACCEPT = "application/json, text/event-stream"


def _upstream_handler(seen: list[dict[str, Any]]):
    """An MCP server good enough to complete a handshake, recording every hit.

    It answers plain JSON rather than SSE-framed events. That is not a
    shortcut: the transport accepts both, and the JSON form is what lets an
    off-the-shelf stub stand in for this upstream outside the test suite.

    **Its tool is named after the host that was dialled.** One handler serves
    every mount, so a stub answering identically everywhere would let a proxy
    hardcode a url, mount every server against the first one, or swap two
    upstreams, and no assertion about tool names could tell. Deriving the name
    from `request.url.host` means a tool can only appear if the address its
    config named was the address actually called.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        params = body.get("params") or {}
        seen.append(
            {
                "method": body.get("method"),
                "url": str(request.url),
                "tool": params.get("name"),
                "arguments": params.get("arguments"),
                "headers": dict(request.headers),
                "x-rail": request.headers.get("x-rail"),
                "x-rail-status": request.headers.get("x-rail-status"),
            }
        )
        session = {"mcp-session-id": "test-upstream-session"}
        method = body.get("method")

        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test-upstream", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": request.url.host.split(".")[0],
                        "description": "Echo the text back.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    }
                ]
            }
        elif method == "tools/call":
            result = {
                "content": [{"type": "text", "text": "reached-the-upstream"}],
                "isError": False,
            }
        elif method and method.startswith("notifications/"):
            return httpx.Response(202, headers=session)
        else:
            result = {}

        return httpx.Response(
            200,
            headers=session,
            json={"jsonrpc": "2.0", "id": body.get("id"), "result": result},
        )

    return handler


def _client_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """The factory's kwargs an AsyncClient accepts alongside a transport.

    fastmcp 3.4.6 passes headers, auth, follow_redirects and timeout. Dropping
    timeout would discard the very bound `upstream_timeout()` exists to set, so
    the test client would not carry what the real one does.
    """
    return {
        k: v
        for k, v in kwargs.items()
        if k in ("headers", "auth", "follow_redirects", "timeout")
    }
