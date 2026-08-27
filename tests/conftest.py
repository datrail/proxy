"""Shared fixtures: a recording upstream, and the proxy's app driven over ASGI."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

MCP_ACCEPT = "application/json, text/event-stream"


def _upstream_handler(seen: list[dict[str, Any]]):
    """An MCP server good enough to complete a handshake, recording every hit.

    It answers plain JSON rather than SSE-framed events. That is not a
    shortcut: the transport accepts both, and the JSON form is what lets an
    off-the-shelf stub stand in for this upstream outside the test suite.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        seen.append(
            {
                "method": body.get("method"),
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
                        "name": "echo",
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
    """The subset of the factory's kwargs an AsyncClient accepts alongside a
    transport. FastMCP passes headers, auth and follow_redirects."""
    return {
        k: v for k, v in kwargs.items() if k in ("headers", "auth", "follow_redirects")
    }


@pytest.fixture
def upstream(monkeypatch):
    """Point every upstream the proxy builds at a recorder, and return its log.

    Patched at the module symbol rather than threaded through `build_gateway`
    as a parameter: a seam that exists only for tests is a seam that can be
    wrong in production without any test noticing.
    """
    from fastmcp_proxy import proxy as proxy_module

    seen: list[dict[str, Any]] = []
    handler = _upstream_handler(seen)

    def factory(**kwargs):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **_client_kwargs(kwargs)
        )

    original = proxy_module.StreamableHttpTransport

    def patched(*args, **kwargs):
        kwargs.setdefault("httpx_client_factory", factory)
        return original(*args, **kwargs)

    monkeypatch.setattr(proxy_module, "StreamableHttpTransport", patched)
    return seen


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A config file naming one upstream, as the proxy will read it."""
    path = tmp_path / "bridge.yaml"
    path.write_text(
        "mcp:\n  servers:\n    - name: delivery\n      url: http://upstream.invalid/mcp\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RAIL_PROXY_CONFIG_FILE", str(path))
    from fastmcp_proxy import proxy as proxy_module

    monkeypatch.setattr(proxy_module, "CONFIG_FILE", path)
    return path
