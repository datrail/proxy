"""Tests for the per-request decision: which x-rail headers are set or removed,
and where a protected request is sent."""

import pytest

from core_support import xrail_params
from proxy.core.xrail_auth import OutboundHeaders, outbound_headers
from proxy.envoy_grpc import settings
from proxy.envoy_grpc.decision import Decision, decide

# What an ordinary MCP client sends, besides anything a test adds.
_ORDINARY = {
    "host": "mcp.example.com",
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "user-agent": "agent/1",
    "mcp-session-id": "s-1",
    "x-trace": "abc",
    "cookie": "session=1",
}


def _protected_hosts(monkeypatch, value):
    monkeypatch.setenv("RAIL_PROXY_PROTECTED_HOSTS", value)
    return settings.get_protected_hosts(plugin_on=False)


def _forwarded(agent: dict[str, str], decision: Decision) -> dict[str, str]:
    """What reaches the upstream: the agent's headers with the decision applied,
    as Envoy applies it. The route removes `x-rail-upstream`, and `:authority`
    becomes the request's host, so neither is a header the upstream sees."""
    sent = {k: v for k, v in agent.items() if k.lower() not in decision.remove}
    sent.update(decision.headers)
    sent.pop("x-rail-upstream", None)
    sent.pop(":authority", None)
    return sent


@pytest.mark.parametrize("case", xrail_params("envoy-grpc"))
def test_a_protected_request_matches_every_row_of_the_contract(monkeypatch, case):
    protected_hosts = _protected_hosts(monkeypatch, "mcp.example.com")
    agent = {**_ORDINARY, **case.agent_headers}

    decision = decide(
        "mcp.example.com", agent, protected_hosts, outbound_headers(case.holder())
    )
    sent = _forwarded(agent, decision)

    for name, value in case.expected.items():
        assert sent.get(name) == value
    for name, value in case.forwarded.items():
        assert sent.get(name) == value
    assert not set(sent) & case.absent
    agents_own = {k: v for k, v in agent.items() if not k.startswith("x-rail")}
    assert agents_own.items() <= sent.items()


@pytest.mark.parametrize(
    ("entry", "target", "upstream", "rewritten"),
    [
        ("mcp.example.com", "mcp.example.com", "tls", "mcp.example.com"),
        ("mcp.example.com", "mcp.example.com:80", "tls", "mcp.example.com"),
        ("mcp.example.com", "mcp.example.com:9443", "tls", "mcp.example.com:9443"),
        ("mcp.example.com:8443", "mcp.example.com", "tls", "mcp.example.com:8443"),
        ("mcp.example.com:8443", "mcp.example.com:80", "tls", "mcp.example.com:8443"),
        ("mcp.example.com:8443", "mcp.example.com:9999", "tls", "mcp.example.com:8443"),
        ("http://gw.internal", "gw.internal:8080", "plain", "gw.internal:8080"),
        ("http://gw.internal", "gw.internal", "plain", "gw.internal"),
        ("http://gw.internal:8080", "gw.internal", "plain", "gw.internal:8080"),
    ],
)
def test_where_a_protected_request_goes(
    monkeypatch, entry, target, upstream, rewritten
):
    """HTTPS unless the entry says `http://`; the entry's port, else the
    agent's, where `:80` counts as no port."""
    decision = decide(
        target, [], _protected_hosts(monkeypatch, entry), OutboundHeaders()
    )

    assert decision.headers["x-rail-upstream"] == upstream
    assert decision.headers[":authority"] == rewritten


@pytest.mark.parametrize(
    ("entry", "target", "rewritten"),
    [
        ("mcp.example.com", "MCP.Example.COM", "mcp.example.com"),
        ("mcp.example.com", "mcp.example.com.", "mcp.example.com"),
        ("mcp.example.com.", "mcp.example.com:9443", "mcp.example.com:9443"),
        ("[2001:db8::1]", "[2001:DB8::1]:8443", "[2001:db8::1]:8443"),
        ("10.0.0.7", "10.0.0.7:80", "10.0.0.7"),
    ],
)
def test_the_host_is_matched_as_the_entry_was_read(
    monkeypatch, entry, target, rewritten
):
    """Case, one trailing dot and IPv6 brackets don't change the host."""
    decision = decide(
        target, [], _protected_hosts(monkeypatch, entry), OutboundHeaders()
    )

    assert decision.headers[":authority"] == rewritten


@pytest.mark.parametrize(
    "target",
    [
        "open.example.com",
        "open.example.com:8080",
        "mcp.example.com.evil.example",
        "evil-mcp.example.com",
        "10.0.0.8",
        "[2001:db8::2]:8443",
    ],
)
def test_an_unprotected_request_is_left_alone(monkeypatch, target):
    """No ticket, no header removed, not upgraded to HTTPS, not rerouted."""
    protected_hosts = _protected_hosts(
        monkeypatch, "mcp.example.com,10.0.0.7,[2001:db8::1]"
    )
    agent = {**_ORDINARY, "authorization": "Bearer agent-secret"}
    outbound = OutboundHeaders({"x-rail": "the-ticket"})

    assert decide(target, agent, protected_hosts, outbound) == Decision()


def test_only_x_rail_headers_are_removed_whatever_their_case(monkeypatch):
    agent = ["Content-Type", "Authorization", ":path", "X-Rail", "X-Rail-Foo"]

    decision = decide(
        "mcp.example.com",
        agent,
        _protected_hosts(monkeypatch, "mcp.example.com"),
        OutboundHeaders(),
    )

    assert decision.remove == {"x-rail", "x-rail-foo"}


def test_a_header_merely_starting_with_x_rail_is_not_an_x_rail_header(monkeypatch):
    decision = decide(
        "mcp.example.com",
        ["x-railway-id", "x-rails"],
        _protected_hosts(monkeypatch, "mcp.example.com"),
        OutboundHeaders(),
    )

    assert decision.remove == frozenset()


def test_a_header_that_is_set_is_never_also_removed(monkeypatch):
    """The agent's own `x-rail` is replaced, not removed: removing it could
    also remove the ticket, depending on the order Envoy applies the two."""
    agent = ["x-rail", "x-rail-status", "x-rail-upstream", "x-rail-foo"]
    outbound = OutboundHeaders({"x-rail": "the-ticket"})

    decision = decide(
        "mcp.example.com",
        agent,
        _protected_hosts(monkeypatch, "mcp.example.com"),
        outbound,
    )

    assert decision.headers["x-rail"] == "the-ticket"
    assert decision.remove == {"x-rail-status", "x-rail-foo"}


@pytest.mark.parametrize("target", ["mcp.example.com:abc", "[2001:db8::1"])
def test_a_target_that_cannot_be_parsed_raises(monkeypatch, target):
    with pytest.raises(ValueError):
        decide(
            target,
            [],
            _protected_hosts(monkeypatch, "mcp.example.com"),
            OutboundHeaders(),
        )
