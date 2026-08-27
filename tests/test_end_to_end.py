"""The seam: a call arrives over HTTP, is forwarded, and the answer comes back.

Every other test in this repository exercises a function. This one drives the
real ASGI application the container serves, through a real MCP client, to an
upstream that records what it received — so it is the only place that can catch
the mount, the transport and the request path disagreeing with each other.
"""

from __future__ import annotations

import contextlib

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from fastmcp_proxy import proxy as proxy_module
from tests.conftest import MCP_ACCEPT, _client_kwargs


@contextlib.asynccontextmanager
async def running_proxy():
    """The proxy's own app, served in-process.

    `ASGITransport` does not run the lifespan, and FastMCP's session manager is
    started there — without entering it by hand every request fails on a
    manager that was never started.
    """
    app = proxy_module.build_app()
    inner = app.app
    async with inner.router.lifespan_context(inner):
        yield app


def agent_client(app) -> Client:
    """An MCP client speaking to the proxy the way a sandboxed agent does."""

    def factory(**kwargs):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
            **_client_kwargs(kwargs),
        )

    return Client(
        StreamableHttpTransport(
            url="http://proxy.test/mcp/", httpx_client_factory=factory
        )
    )


@pytest.mark.asyncio
async def test_the_upstreams_tools_reach_the_agent_namespaced(config, upstream):
    """Feature 1 and 4's first half: the agent sees what the upstream offers,
    under the mount's name. `delivery_echo` is the string an agent is prompted
    with, so the prefix is a contract and not a formatting detail."""
    async with running_proxy() as app, agent_client(app) as client:
        tools = [t.name for t in await client.list_tools()]

    assert tools == ["delivery_echo"]


@pytest.mark.asyncio
async def test_a_tool_call_is_forwarded_and_its_answer_returned(config, upstream):
    """The whole path, in one assertion each way: the call reaches the upstream,
    and the upstream's answer reaches the agent."""
    async with running_proxy() as app, agent_client(app) as client:
        result = await client.call_tool("delivery_echo", {"text": "hello"})

    assert result.content[0].text == "reached-the-upstream"
    assert "tools/call" in [hit["method"] for hit in upstream]


@pytest.mark.asyncio
async def test_no_identity_header_is_attached(config, upstream):
    """Nothing here attaches `x-rail`, and nothing here explains its absence.

    That is this component's pass-through behaviour, and it is worth asserting
    rather than assuming: an empty header is one of the two states a gateway
    downstream has to tell apart, and a proxy that started sending something
    from a default would be indistinguishable from one that was configured to.
    """
    async with running_proxy() as app, agent_client(app) as client:
        await client.call_tool("delivery_echo", {"text": "hello"})

    assert {hit["x-rail"] for hit in upstream} == {None}
    assert {hit["x-rail-status"] for hit in upstream} == {None}


@pytest.mark.asyncio
async def test_get_on_the_mcp_path_is_refused_as_the_transport_requires(
    config, upstream
):
    """A client that follows the transport spec treats anything but 405 on
    `GET /mcp` as fatal, and FastMCP answers 400 without a session id and 404
    with an unknown one. Both are rewritten; if a future version answers a
    third status this test is what notices."""
    async with (
        running_proxy() as app,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as raw,
    ):
        no_session = await raw.get("/mcp", headers={"Accept": MCP_ACCEPT})
        unknown_session = await raw.get(
            "/mcp", headers={"Accept": MCP_ACCEPT, "mcp-session-id": "no-such"}
        )

    assert no_session.status_code == 405
    assert unknown_session.status_code == 405
    assert no_session.headers["allow"] == "POST"


@pytest.mark.asyncio
async def test_health_answers_without_an_upstream_being_reachable(config, upstream):
    """Liveness, not readiness: it reports that the process is up and its
    config parsed, and asks the upstream nothing."""
    async with (
        running_proxy() as app,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as raw,
    ):
        response = await raw.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert upstream == []
