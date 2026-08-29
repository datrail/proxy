"""The seam: a call arrives over HTTP, is forwarded, and the answer comes back.

The other tests in this repository exercise a function. This one drives the
real ASGI application the container serves, through a real MCP client, to an
upstream that records what it received — so it is the only place that can catch
the mount, the transport and the request path disagreeing with each other.
"""

from __future__ import annotations

import contextlib
import json
import pathlib

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from fastmcp_proxy import proxy as proxy_module
from fastmcp_proxy.proxy import McpMethodCompat
from tests.conftest import MCP_ACCEPT, _client_kwargs, wound_holder


@contextlib.asynccontextmanager
async def running_proxy(holder=None):
    """The proxy's own app, served in-process.

    `ASGITransport` does not run the lifespan, and FastMCP's session manager is
    started there — without entering it by hand every request fails on a manager
    that was never started. It is entered on the inner app because the manager's
    task group is bound to the task that opens it; the middleware's own handling
    of a lifespan scope is pinned separately, in
    `test_a_lifespan_scope_is_passed_through_untouched`.
    """
    app = proxy_module.build_app(holder)
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
    what FastMCP would have said does not arise."""
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
    """It reports that the process is up and its config parsed, and asks the
    upstream nothing. Under `RAIL_TICKET_MODE=none` there is no ticket to
    report, and `null` says that rather than omitting the key."""
    async with (
        running_proxy() as app,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as raw,
    ):
        response = await raw.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "ticket_mode": "none",
        "ticket": None,
    }
    assert upstream == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "PUT", "DELETE", "PATCH"])
async def test_every_other_verb_is_answered_here_rather_than_forwarded(
    config, upstream, method
):
    """Answered here rather than forwarded — `McpMethodCompat` says why the work
    an untrusted sandbox can make this endpoint do is worth bounding. The
    distinctive body is what proves the answer came from here rather than from
    downstream."""
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
    proxy_module.build_gateway(None)

    assert captured == [{"timeout": 7.0, "init_timeout": 7.0}]


# ─────────────────────────────────────────────────────────────────────
#  What goes out on the wire
#
#  Three states, not two. Pass-through is not the fail-closed path: a proxy
#  told to attach nothing says nothing about identity, while one that meant to
#  attach and could not says so in `x-rail-status`. A gateway has to tell an
#  agent running without a proxy from one whose ticket lapsed, and that
#  distinction is the only thing that carries it.
# ─────────────────────────────────────────────────────────────────────


#: Everything the transport itself puts on a forwarded request. The identity
#: headers are added per test, so a new name appearing on either path fails
#: rather than passing unnoticed.
_EXPECTED_OUTBOUND = {
    "host",
    "accept",
    "accept-encoding",
    "connection",
    "user-agent",
    "content-length",
    "content-type",
    "mcp-protocol-version",
    "mcp-session-id",
}


async def _forward_one_call(holder):
    """Make one tool call through the proxy and return what the upstream saw."""
    async with running_proxy(holder) as app, agent_client(app) as client:
        await client.call_tool("delivery_upstream", {"text": "hi"})


@pytest.mark.asyncio
async def test_a_held_ticket_is_attached_to_what_is_forwarded(config, upstream):
    """The feature: the sandbox never holds the credential identifying it, and
    the upstream sees an identity the agent could not have supplied."""
    await _forward_one_call(wound_holder(ticket="rc_ticket_opaque"))

    calls = [c for c in upstream if c["method"] == "tools/call"]
    assert calls, "nothing reached the upstream"
    assert all(c["x-rail"] == "rc_ticket_opaque" for c in calls)
    assert all(c["x-rail-status"] is None for c in calls)

    # The whole header set, not just the two this test is named for. A proxy
    # that started sending a fingerprint, or anything else derived from the
    # ticket, would otherwise be invisible on the one path that carries it.
    for call in calls:
        assert set(call["headers"]) <= _EXPECTED_OUTBOUND | {"x-rail"}, sorted(
            call["headers"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["not-found", "expired", "issuer-unreachable"], ids=str
)
async def test_no_valid_ticket_fails_closed_with_the_reason(config, upstream, reason):
    """The request still goes out, without an identity and saying why. Refusing
    to forward would make an issuer outage an agent outage; sending the reason
    in `x-rail` itself would turn absence into presence and make this component
    an author of ticket content."""
    await _forward_one_call(wound_holder(reason=reason))

    calls = [c for c in upstream if c["method"] == "tools/call"]
    assert calls, "nothing reached the upstream"
    assert all(c["x-rail"] is None for c in calls)
    assert all(c["x-rail-status"] == reason for c in calls)

    # The whole set on this path too. An outage is the state an operator can
    # least observe, so anything the holder started leaking here would be the
    # hardest to notice.
    for call in calls:
        assert set(call["headers"]) <= _EXPECTED_OUTBOUND | {"x-rail-status"}, sorted(
            call["headers"]
        )


@pytest.mark.asyncio
async def test_pass_through_attaches_neither_header(config, upstream):
    """`RAIL_TICKET_MODE=none`. Not the fail-closed path — no status header
    either, because nothing was attempted."""
    await _forward_one_call(None)

    calls = [c for c in upstream if c["method"] == "tools/call"]
    assert calls, "nothing reached the upstream"
    assert all(c["x-rail"] is None for c in calls)
    assert all(c["x-rail-status"] is None for c in calls)


@pytest.mark.asyncio
async def test_the_ticket_is_attached_to_the_handshake_too(config, upstream):
    """`initialize` and `tools/list` reach the same gateway as `tools/call`, so
    a proxy that stamped only the call would have its agent's tool discovery
    denied — and the failure would look like a missing upstream."""
    await _forward_one_call(wound_holder(ticket="rc_ticket_opaque"))

    assert upstream, "nothing reached the upstream"
    assert all(c["x-rail"] == "rc_ticket_opaque" for c in upstream)


@pytest.mark.asyncio
async def test_a_rotation_is_picked_up_without_a_restart(config, upstream):
    """httpx calls the auth flow per request, so a ticket that rotates mid-life
    reaches the next call. Read once at mount time, every call after a rotation
    would carry an identity Rail Center has already replaced."""
    from fastmcp_proxy.xrail_auth import Token

    holder = wound_holder(ticket="first")

    async with running_proxy(holder) as app, agent_client(app) as client:
        await client.call_tool("delivery_upstream", {"text": "one"})
        holder._ticket = Token("second", holder.clock() + 1800)
        await client.call_tool("delivery_upstream", {"text": "two"})

    seen = [c["x-rail"] for c in upstream if c["method"] == "tools/call"]
    assert seen == ["first", "second"]


@pytest.mark.asyncio
async def test_the_mode_health_reports_is_the_one_it_was_built_with(
    config, upstream, monkeypatch
):
    """`ticket_mode()` is resolved once, at build time. Moved into the handler
    it would be an environment lookup on the hot path and — since it raises on
    a value it does not know — a route that turns a 200 into a 500 long after
    startup, on the endpoint an operator reaches when calls are being denied."""
    monkeypatch.setenv("RAIL_TICKET_MODE", "observe")
    holder = wound_holder(ticket="rc_ticket_opaque")

    async with (
        running_proxy(holder) as app,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as raw,
    ):
        monkeypatch.setenv("RAIL_TICKET_MODE", "not-a-mode")
        response = await raw.get("/health")

    assert response.status_code == 200
    assert response.json()["ticket_mode"] == "observe"


@pytest.mark.asyncio
async def test_health_reports_what_is_held_without_reporting_the_ticket(
    config, upstream, monkeypatch
):
    """What an operator needs when calls are being denied downstream, and what
    an endpoint the sandbox can reach must not hand out."""
    from fastmcp_proxy.xrail_auth import token_fingerprint

    monkeypatch.setenv("RAIL_TICKET_MODE", "observe")
    holder = wound_holder(ticket="rc_ticket_opaque")

    async with (
        running_proxy(holder) as app,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as raw,
    ):
        response = await raw.get("/health")

    body = response.json()
    assert response.status_code == 200
    assert body["ticket_mode"] == "observe"
    assert body["ticket"]["ticket_held"] is True
    assert body["ticket"]["ticket_valid"] is True
    assert body["ticket"]["unavailable_reason"] is None
    assert body["ticket"]["expires_in_sec"] == 1800
    assert body["ticket"]["fingerprint"] == token_fingerprint("rc_ticket_opaque")
    assert "rc_ticket_opaque" not in response.text
    assert "source" not in body["ticket"]
    assert "rc.invalid" not in response.text


@pytest.mark.asyncio
async def test_the_ticket_does_not_follow_a_redirect_off_the_upstream(config):
    """A `307` from an upstream must not carry the ticket to the host it names.
    `upstream_client` says why that is possible at all."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(307, headers={"location": "https://evil.invalid/steal"})

    def factory(**kwargs):
        return proxy_module.upstream_client(
            transport=httpx.MockTransport(handler), **_client_kwargs(kwargs)
        )

    original = proxy_module.StreamableHttpTransport
    holder = wound_holder(ticket="rc_ticket_opaque")

    def patched(*args, **kwargs):
        kwargs["httpx_client_factory"] = factory
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(proxy_module, "StreamableHttpTransport", patched)
        async with running_proxy(holder) as app, agent_client(app) as client:
            with contextlib.suppress(Exception):
                await client.call_tool("delivery_upstream", {"text": "hi"})

    assert seen, "the upstream was never dialled"
    assert all(r.url.host == "upstream.invalid" for r in seen), [
        str(r.url) for r in seen
    ]


@pytest.mark.asyncio
async def test_the_upstream_client_is_the_one_the_proxy_builds():
    """The redirect refusal lives in `upstream_client`. A mount that builds its
    own client, or lets fastmcp build one, silently loses it."""
    client = proxy_module.upstream_client(follow_redirects=True, timeout=5.0)
    try:
        assert client.follow_redirects is False
        assert client.timeout.read == 5.0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_health_stays_200_while_failing_closed(config, upstream, monkeypatch):
    """200, and the state in the body. The route says why it is not 503."""
    monkeypatch.setenv("RAIL_TICKET_MODE", "enforce")
    holder = wound_holder(reason="issuer-unreachable")

    async with (
        running_proxy(holder) as app,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as raw,
    ):
        response = await raw.get("/health")

    assert response.status_code == 200
    assert response.json()["ticket"]["unavailable_reason"] == "issuer-unreachable"
    assert response.json()["ticket"]["ticket_held"] is False


@pytest.mark.asyncio
async def test_a_plaintext_upstream_is_reported_when_a_ticket_is_attached(
    write_config, monkeypatch, caplog
):
    """One warning, for the one upstream it applies to."""
    monkeypatch.setenv("RAIL_TICKET_MODE", "enforce")
    write_config(
        "mcp:\n  servers:\n"
        "    - name: plain\n      url: http://gateway.invalid:8080/mcp\n"
        "    - name: secure\n      url: https://gateway.invalid/mcp\n"
        "    - name: local\n      url: http://127.0.0.1:8080/mcp\n"
    )

    with caplog.at_level("WARNING"):
        proxy_module.build_gateway(wound_holder(ticket="t"))

    warned = [
        r.getMessage() for r in caplog.records if "plaintext http" in r.getMessage()
    ]
    assert len(warned) == 1
    assert "plain" in warned[0]


@pytest.mark.asyncio
async def test_pass_through_does_not_warn_about_a_plaintext_upstream(
    config, caplog, monkeypatch
):
    """With nothing to attach there is nothing on the wire to read, and a
    warning naming a risk that does not apply teaches an operator to skip it."""
    monkeypatch.setenv("RAIL_TICKET_MODE", "none")

    with caplog.at_level("WARNING"):
        proxy_module.build_gateway(None)

    assert not [r for r in caplog.records if "plaintext http" in r.getMessage()]


@pytest.mark.asyncio
async def test_the_upstream_client_reads_no_ambient_proxy_setting(monkeypatch):
    """No mounts, so nothing an environment variable names can be routed
    through — `upstream_client` says why that matters here."""
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")

    client = proxy_module.upstream_client(follow_redirects=True, timeout=5.0)
    try:
        assert client._mounts == {}
        assert client.follow_redirects is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_the_upstream_client_still_finds_an_internal_ca(monkeypatch, tmp_path):
    """One flag governs proxies and CA roots both, so shutting out the first
    must not lose the second."""
    import ssl

    import certifi

    # One root, not the whole default store: a context built with the
    # environment ignored would hold certifi's ~120 instead, so the count is
    # what tells the two apart.
    first = (
        pathlib.Path(certifi.where()).read_text().split("-----END CERTIFICATE-----")[0]
    )
    bundle = tmp_path / "bundle.pem"
    bundle.write_text(first + "-----END CERTIFICATE-----\n")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))

    client = proxy_module.upstream_client()
    try:
        context = client._transport._pool._ssl_context
        assert isinstance(context, ssl.SSLContext)
        assert len(context.get_ca_certs()) == 1
    finally:
        await client.aclose()


def test_a_credential_on_an_upstream_url_is_refused_while_a_ticket_is_attached(
    write_config, monkeypatch
):
    """Refused rather than dropped — `build_gateway` says why it cannot be
    sent."""
    monkeypatch.setenv("RAIL_TICKET_MODE", "enforce")
    write_config(
        "mcp:\n  servers:\n"
        "    - name: paid\n      url: https://svc:s3cret@gateway.invalid/mcp\n"
    )

    with pytest.raises(proxy_module.ConfigError, match="carries a credential") as info:
        proxy_module.build_gateway(wound_holder(ticket="t"))

    assert "s3cret" not in str(info.value)
    assert "paid" in str(info.value)


def test_a_username_only_upstream_url_is_a_credential_too(write_config, monkeypatch):
    """httpx derives Basic auth from `username or password`, so
    `https://token@host/` is as much a credential as `https://u:p@host/`.
    Reading only the password lets the one-part form past."""
    monkeypatch.setenv("RAIL_TICKET_MODE", "enforce")
    write_config(
        "mcp:\n  servers:\n"
        "    - name: paid\n      url: https://s3cret-as-a-username@gateway.invalid/mcp\n"
    )

    with pytest.raises(proxy_module.ConfigError, match="carries a credential"):
        proxy_module.build_gateway(wound_holder(ticket="t"))


def test_a_credential_on_an_upstream_url_is_fine_with_nothing_to_attach(
    write_config, monkeypatch
):
    """With no injector httpx derives Basic auth from the userinfo, which is
    what the config asked for."""
    monkeypatch.setenv("RAIL_TICKET_MODE", "none")
    write_config(
        "mcp:\n  servers:\n"
        "    - name: paid\n      url: https://svc:s3cret@gateway.invalid/mcp\n"
    )

    proxy_module.build_gateway(None)
