"""The seam: a call arrives over HTTP, is forwarded, and the answer comes back.

The other tests in this repository exercise a function. This one drives the
real ASGI application the container serves, through a real MCP client, to an
upstream that records what it received — so it is the only place that can catch
the mount, the transport and the request path disagreeing with each other.
"""

from __future__ import annotations

import contextlib
import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from fastmcp_proxy import proxy as proxy_module
from fastmcp_proxy.proxy import McpMethodCompat
from tests.conftest import MCP_ACCEPT, _client_kwargs


@contextlib.asynccontextmanager
async def running_proxy():
    """The proxy's own app, served in-process.

    `ASGITransport` does not run the lifespan, and FastMCP's session manager is
    started there — without entering it by hand every request fails on a manager
    that was never started. It is entered on the inner app because the manager's
    task group is bound to the task that opens it; the middleware's own handling
    of a lifespan scope is pinned separately, in
    `test_a_lifespan_scope_is_passed_through_untouched`.
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
    """The agent sees what the upstream offers,
    under the mount's name. The prefix is a contract, not a formatting detail —
    it is the string an agent is prompted with — and the suffix is the stub's
    tool, named after the host it was dialled at."""
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
    # `delivery_upstream`, which no upstream would recognise.
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
    arrived: list[dict[str, str]] = []

    async with running_proxy() as app:

        async def recording(scope, receive, send):
            if scope["type"] == "http":
                arrived.append({k.decode(): v.decode() for k, v in scope["headers"]})
            await app(scope, receive, send)

        def factory(**kwargs):
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=recording),
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

    # A positive control. Every assertion above is an absence, and an absence
    # holds just as well if the forged headers never left the test's own
    # client — the boundary and the harness that exercises it would go green
    # together. This says they reached the proxy and stopped there.
    assert arrived, "no request reached the proxy"
    for name, value in forged.items():
        assert {hit.get(name) for hit in arrived} == {value}, name


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
        with_session = await raw.get(
            "/mcp", headers={"Accept": MCP_ACCEPT, "mcp-session-id": "anything"}
        )

    assert no_session.status_code == 405
    assert no_session.headers["allow"] == "POST"
    # `/mcp/` is the url a client is given. Forwarded, it redirects before any
    # status worth rewriting exists; answered here, it behaves like `/mcp`.
    assert trailing_slash.status_code == 405
    # A session id changes nothing: the server is stateless, so there is no
    # session for one to name.
    assert with_session.status_code == 405


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
    assert response.headers["allow"] == "POST"
    assert response.headers["content-type"] == "application/json"
    # The response is hand-rolled ASGI, so its framing is this module's to get
    # right: a content-length that disagrees with the body is accepted by an
    # in-process transport and raises RuntimeError under a real server.
    assert json.loads(McpMethodCompat._RESPONSE)["error"]["code"] == -32600
    if method != "HEAD":
        # Against the body actually received, not against the constant the code
        # sends: comparing the header to `len(_RESPONSE)` pins the two to each
        # other and passes while the body drifts from both. uvicorn raises
        # RuntimeError on the mismatch, so only a real server would have caught
        # it — and an in-process transport does not.
        assert int(response.headers["content-length"]) == len(response.content)
        assert "accepts POST" in response.text


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

    # Not `!= 405`: that holds for a proxy answering 500 to everything. A
    # malformed POST is the transport's to reject, and it rejects with 400.
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_the_server_issues_no_session(config, upstream):
    """Stateless is what keeps an unauthenticated POST from retaining a
    transport nothing reclaims, so the absence of a session id on a real
    exchange is the property worth pinning, not an incidental."""
    async with (
        running_proxy() as app,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as raw,
    ):
        response = await raw.post(
            "/mcp",
            headers={"Accept": MCP_ACCEPT},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        )

    assert response.status_code == 200
    assert "mcp-session-id" not in response.headers


@pytest.mark.asyncio
async def test_a_lifespan_scope_is_passed_through_untouched():
    """The one scope a server always sends that is not a request.

    Handled wrongly, FastMCP's session manager never starts and every MCP call
    fails while `/health` still answers 200 — a server that looks up and serves
    nothing. The main harness cannot see this: it enters the lifespan on the
    inner app, so the scope never travels through the wrapper.
    """
    seen: list[str] = []

    async def inner(scope, _receive, _send):
        seen.append(scope["type"])

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(_message):
        raise AssertionError("the middleware answered a lifespan scope itself")

    await McpMethodCompat(inner)({"type": "lifespan"}, receive, send)

    assert seen == ["lifespan"]


@pytest.mark.asyncio
async def test_the_upstream_timeout_reaches_the_client(config, upstream, monkeypatch):
    """The accessor is pinned elsewhere; this pins that its value reaches the
    client. Dropping either argument, or hardcoding a number, is invisible to
    every test that only reads the accessor — and an absent timeout is the one
    failure mode the setting exists to prevent.

    Asserted at the module's own symbol rather than by walking FastMCP's
    provider tree, which is private.
    """
    monkeypatch.setenv("RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS", "7")
    captured: list[dict] = []
    original = proxy_module.Client

    def recording_client(transport, **kwargs):
        captured.append(kwargs)
        return original(transport, **kwargs)

    monkeypatch.setattr(proxy_module, "Client", recording_client)
    proxy_module.build_gateway()

    assert captured == [{"timeout": 7.0, "init_timeout": 7.0}]
