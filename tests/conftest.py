"""Shared fixtures: a recording upstream, and the config the proxy reads."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import httpx
import pytest

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
def write_config(tmp_path, monkeypatch):
    """Write a config file and point the proxy at it through the environment.

    Only the environment — no attribute is patched. `config_file()` resolves
    RAIL_PROXY_CONFIG_FILE per call, so patching a module attribute instead
    would leave the variable that the image actually sets untested.
    """

    def write(body: str) -> pathlib.Path:
        path = tmp_path / "bridge.yaml"
        path.write_text(body, encoding="utf-8")
        monkeypatch.setenv("RAIL_PROXY_CONFIG_FILE", str(path))
        return path

    return write


ONE_UPSTREAM = (
    "mcp:\n  servers:\n    - name: delivery\n      url: http://upstream.invalid/mcp\n"
)


@pytest.fixture
def config(write_config):
    """One upstream, named `delivery`."""
    return write_config(ONE_UPSTREAM)
