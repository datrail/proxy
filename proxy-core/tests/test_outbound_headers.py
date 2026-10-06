"""What goes out with a request: the x-rail headers, decided once for every
interface."""

import pytest

from core_support import wound_holder, xrail_params
from proxy.core.xrail_auth import (
    XRAIL_HEADER,
    XRAIL_STATUS_HEADER,
    XRAIL_UPSTREAM_HEADER,
    TicketHeaders,
    XRailInjector,
    agent_header_may_cross,
    is_xrail_header,
    outbound_headers,
    token_fingerprint,
)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"ticket": "the-ticket"}, {"x-rail": "the-ticket"}),
        ({"reason": "not-found"}, {"x-rail-status": "not-found"}),
        ({"reason": "expired"}, {"x-rail-status": "expired"}),
        ({"reason": "issuer-unreachable"}, {"x-rail-status": "issuer-unreachable"}),
    ],
    ids=["ticket", "not-found", "expired", "issuer-unreachable"],
)
def test_a_holder_gives_exactly_one_of_the_two_headers(state, expected):
    assert dict(outbound_headers(wound_holder(**state)).headers) == expected


def test_no_holder_gives_neither_header():
    """Pass-through says nothing about identity, so not even a status."""
    assert dict(outbound_headers(None).headers) == {}


def test_the_names_are_the_injectors():
    """One set of names, whichever interface writes them."""
    assert XRailInjector.HEADER == XRAIL_HEADER == "x-rail"
    assert XRailInjector.STATUS_HEADER == XRAIL_STATUS_HEADER == "x-rail-status"
    assert XRAIL_UPSTREAM_HEADER == "x-rail-upstream"


def test_no_holder_is_neither_attached_nor_logged(caplog):
    headers = TicketHeaders(None)

    with caplog.at_level("DEBUG"):
        assert dict(headers.for_request("http://up.invalid/mcp").headers) == {}

    assert not caplog.records


def test_an_attached_ticket_is_logged_by_its_fingerprint_only(caplog):
    headers = TicketHeaders(wound_holder(ticket="the-ticket"))

    with caplog.at_level("DEBUG"):
        headers.for_request("http://up.invalid/mcp")

    assert token_fingerprint("the-ticket") in caplog.text
    assert "the-ticket" not in caplog.text


def test_a_missing_ticket_is_warned_about_once_per_outage(caplog):
    headers = TicketHeaders(wound_holder(reason="expired"))

    with caplog.at_level("WARNING"):
        for _ in range(5):
            headers.for_request("http://up.invalid/mcp")
        headers.holder = wound_holder(ticket="recovered")
        headers.for_request("http://up.invalid/mcp")
        headers.holder = wound_holder(reason="expired")
        headers.for_request("http://up.invalid/mcp")

    warnings = [r for r in caplog.records if "no valid ticket" in r.getMessage()]
    assert len(warnings) == 2


@pytest.mark.parametrize("case", xrail_params("core"))
def test_the_decision_matches_every_row_of_the_contract(case):
    """The decision alone, before any interface applies it. The agent's own
    headers are the interface's to remove, so a forged row decides exactly what
    its plain row does."""
    decided = dict(outbound_headers(case.holder()).headers)

    assert decided == dict(case.expected)
    assert not set(decided) & case.absent


@pytest.mark.parametrize(
    "name", ["x-rail", "x-rail-status", "x-rail-upstream", "x-rail-foo", "X-Rail"]
)
def test_the_x_rail_namespace_is_x_rail_and_every_x_rail_header(name):
    assert is_xrail_header(name)
    assert not agent_header_may_cross(name)


@pytest.mark.parametrize("name", ["x-railway", "x-rails", "rail", "x-trace"])
def test_a_name_that_only_starts_like_x_rail_is_not_in_it(name):
    assert not is_xrail_header(name)


@pytest.mark.parametrize("name", ["x_rail", "x_rail_status", "X_Trace", "a_b"])
def test_a_name_with_an_underscore_never_crosses(name):
    """Many servers read `_` as `-`, so `x_rail` would reach one as `x-rail`."""
    assert not agent_header_may_cross(name)


@pytest.mark.parametrize(
    "name", ["authorization", "cookie", "user-agent", "x-trace", "X-Trace"]
)
def test_the_agents_other_headers_may_cross(name):
    assert agent_header_may_cross(name)
