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

    assert tools == ["delivery_upstream"]


@pytest.mark.asyncio
async def test_a_tool_call_is_forwarded_and_its_answer_returned(config, upstream):
    """The whole path, in one assertion each way: the call reaches the upstream,
    and the upstream's answer reaches the agent."""
    async with running_proxy() as app, agent_client(app) as client:
        result = await client.call_tool("delivery_upstream", {"text": "hello"})

    assert result.content[0].text == "reached-the-upstream"

    calls = [hit for hit in upstream if hit["method"] == "tools/call"]
    assert len(calls) == 1
    # The namespace is added on the way in and stripped on the way out. Asserting
    # only that a call happened would pass against a proxy forwarding
    # `delivery_echo`, which no upstream would recognise.
    assert calls[0]["tool"] == "upstream"
    assert calls[0]["arguments"] == {"text": "hello"}


@pytest.mark.asyncio
async def test_calls_go_to_the_configured_address(config, upstream):
    """The stub answers any host and path, so without this nothing pins the url
    from the config to the request that is actually made."""
    async with running_proxy() as app, agent_client(app) as client:
        await client.call_tool("delivery_upstream", {"text": "hello"})

    assert {hit["url"] for hit in upstream} == {"http://upstream.invalid/mcp"}


@pytest.mark.asyncio
async def test_the_namespace_is_the_configured_name(write_config, upstream):
    """`delivery` is a value in a file, not a constant in the proxy. A second
    name proves the prefix follows the config rather than a literal."""
    write_config(
        "mcp:\n  servers:\n    - name: billing\n      url: http://upstream.invalid/mcp\n"
    )
    async with running_proxy() as app, agent_client(app) as client:
        tools = [t.name for t in await client.list_tools()]

    assert tools == ["billing_upstream"]


@pytest.mark.asyncio
async def test_every_configured_upstream_is_mounted(write_config, upstream):
    """One entry is the deployed shape, so nothing else exercises the loop."""
    write_config(
        "mcp:\n  servers:\n"
        "    - name: delivery\n      url: http://one.invalid/mcp\n"
        "    - name: billing\n      url: http://two.invalid/mcp\n"
    )
    async with running_proxy() as app, agent_client(app) as client:
        tools = sorted(t.name for t in await client.list_tools())

    # The suffix is the host each mount dialled, so this pins mount to url:
    # a hardcoded address, or two upstreams swapped, gives different names.
    assert tools == ["billing_two", "delivery_one"]


@pytest.mark.asyncio
async def test_the_agents_own_headers_do_not_reach_the_upstream(config, upstream):
    """The proxy is the identity boundary, and this is the case that makes it
    one. `create_proxy` turns on incoming-header forwarding, so without the
    switch being turned back off an agent sets `x-rail` itself and the upstream
    receives it unchanged — an identity supplied by the caller it identifies.
    `authorization` is forwarded by the same path."""
    forged = {
        "x-rail": "forged-by-the-sandbox",
        "x-rail-status": "forged",
        "authorization": "Bearer agent-secret",
    }
    async with running_proxy() as app:

        def factory(**kwargs):
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://proxy.test",
                **_client_kwargs(kwargs),
            )

        client = Client(
            StreamableHttpTransport(
                url="http://proxy.test/mcp/",
                headers=forged,
                httpx_client_factory=factory,
            )
        )
        async with client:
            await client.call_tool("delivery_upstream", {"text": "hello"})

    for name in forged:
        assert {hit["headers"].get(name) for hit in upstream} == {None}, name


@pytest.mark.asyncio
async def test_no_identity_header_is_attached(config, upstream):
    """Nothing here attaches `x-rail`, and nothing here explains its absence.

    That is this component's pass-through behaviour, and it is worth asserting
    rather than assuming: an empty header is one of the two states a gateway
    downstream has to tell apart, and a proxy that started sending something
    from a default would be indistinguishable from one that was configured to.
    """
    async with running_proxy() as app, agent_client(app) as client:
        await client.call_tool("delivery_upstream", {"text": "hello"})

    # Every header on the wire, not a two-name allowlist: a proxy that started
    # attaching something else would otherwise pass this unchanged.
    expected = {
        "host",
        "accept",
        "accept-encoding",
        "connection",
        "user-agent",
        "content-length",
        "content-type",
        "mcp-protocol-version",
        "mcp-session-id",
        "cache-control",
    }
    for hit in upstream:
        assert set(hit["headers"]) <= expected, sorted(set(hit["headers"]) - expected)


@pytest.mark.asyncio
async def test_get_on_the_mcp_path_is_refused_as_the_transport_requires(
    config, upstream
):
    """A client that follows the transport spec treats anything but 405 on
    `GET /mcp` as fatal. The probe is answered here rather than forwarded, so
    what FastMCP would have said does not arise — and neither does the session
    it would have allocated for a request that never completes a handshake."""
    async with (
        running_proxy() as app,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as raw,
    ):
        no_session = await raw.get("/mcp", headers={"Accept": MCP_ACCEPT})
        trailing_slash = await raw.get("/mcp/", headers={"Accept": MCP_ACCEPT})
        unknown_session = await raw.get(
            "/mcp", headers={"Accept": MCP_ACCEPT, "mcp-session-id": "no-such"}
        )

    assert no_session.status_code == 405
    assert no_session.headers["allow"] == "POST, DELETE"
    # `/mcp/` is the url a client is given. Forwarded, it redirects before any
    # status worth rewriting exists; answered here, it behaves like `/mcp`.
    assert trailing_slash.status_code == 405
    # A session id that no session matches means the session ended, not that the
    # stream is unavailable. Answering 405 would leave the client reusing it.
    assert unknown_session.status_code == 404


@pytest.mark.asyncio
async def test_the_rewrite_does_not_reach_past_the_mcp_endpoint(config, upstream):
    """`startswith("/mcp")` would also catch these, and `Allow: POST` on a route
    that does not exist asserts that posting to it would work."""
    async with (
        running_proxy() as app,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as raw,
    ):
        statuses = {
            path: (await raw.get(path, headers={"Accept": MCP_ACCEPT})).status_code
            for path in ("/mcpfoo", "/mcp-admin", "/nope")
        }
        posted = await raw.post("/mcp", headers={"Accept": MCP_ACCEPT}, json={})

    assert statuses == {"/mcpfoo": 404, "/mcp-admin": 404, "/nope": 404}
    assert posted.status_code != 405


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


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "PUT", "DELETE", "PATCH"])
async def test_every_sessionless_method_is_answered_without_opening_a_session(
    config, upstream, method
):
    """Forwarding one of these makes FastMCP allocate a transport before
    deciding it is a 405, and nothing reclaims a transport that never
    handshakes — so an unauthenticated caller could exhaust the process with a
    verb the shim did not cover. The distinctive body is what proves the answer
    came from here rather than from downstream."""
    async with (
        running_proxy() as app,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as raw,
    ):
        response = await raw.request(method, "/mcp", headers={"Accept": MCP_ACCEPT})

    assert response.status_code == 405
    assert response.headers["allow"] == "POST, DELETE"
    if method != "HEAD":
        assert "does not offer a GET event stream" in response.text


@pytest.mark.asyncio
async def test_post_is_never_answered_here(config, upstream):
    """POST is how a session is created. Answering it would end the protocol
    before it began."""
    async with (
        running_proxy() as app,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as raw,
    ):
        response = await raw.post("/mcp", headers={"Accept": MCP_ACCEPT}, json={})

    assert response.status_code != 405


@pytest.mark.asyncio
async def test_a_session_id_passes_through_whatever_the_method(config, upstream):
    """DELETE with a session id is how a client terminates its own session, and
    a 404 for any method means the session has ended rather than that the method
    is unavailable."""
    async with (
        running_proxy() as app,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as raw,
    ):
        response = await raw.request(
            "DELETE",
            "/mcp",
            headers={"Accept": MCP_ACCEPT, "mcp-session-id": "no-such"},
        )

    assert response.status_code != 405
