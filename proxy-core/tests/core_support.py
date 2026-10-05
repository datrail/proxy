"""Helpers for the proxy-core suite, and for the members built on proxy-core."""

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from proxy.core.xrail_auth import TicketHolder, Token

#: Every variable the proxy reads, cleared by each member's autouse
#: `no_rail_center`. Autouse rather than opt-in, and the whole set rather than the ticket half: a value left in the shell — `RAIL_HOST_ID`
#: alone makes `main()` exit 2 on a partly-configured control plane,
#: `RAIL_PROXY_PORT=junk` makes it exit 2 on the port — fails tests that never
#: mention either, somewhere that names none of this. `RAIL_PROXY_PORT` is one
#: the image itself sets.
_RAIL_ENVIRONMENT = (
    "RAIL_CENTER_URL",
    "RAIL_HOST_ID",
    "RAIL_SANDBOX_NAME",
    "RAIL_AUTH_MODE",
    "RAIL_AUTH_TOKEN",
    "RAIL_PLUGIN_ENABLED",
    # Retired, and cleared for exactly that reason: the proxy refuses to start
    # on a leftover one, so a value in the shell would exit 2 across the suite.
    "RAIL_TICKET_MODE",
    "RAIL_PROXY_REFRESH_SECONDS",
    "RAIL_PROXY_TICKET_TIMEOUT_SECONDS",
    "RAIL_PROXY_MAX_TICKET_LIFETIME_SECONDS",
    "RAIL_PROXY_ALLOW_INSECURE_CREDENTIAL",
    "RAIL_PROXY_BIND",
    "RAIL_PROXY_PORT",
    "RAIL_PROXY_LOG_LEVEL",
    "RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS",
    "RAIL_PROXY_CONFIG_FILE",
    "RAIL_PROXY_PROTECTED_HOSTS",
    "RAIL_PROXY_EXT_SOCKET",
    # Not RAIL_*, but read on the same path: `_exchange` builds its SSL context
    # with `trust_env=True` even behind a MockTransport, so a shell pointing
    # these at a bundle this machine does not have turns a fifth of the suite
    # red for a reason unrelated to the code.
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


class _NeverAsked:
    """A ticket source these tests never let anything call."""

    def describe(self) -> str:
        return "https://rc.invalid/v1/tickets (unauthenticated)"

    async def fetch(self):  # pragma: no cover - reaching this is the bug
        raise AssertionError("this holder was wound by hand; nothing should fetch")


def wound_holder(*, ticket: str | None = None, reason: str | None = None):
    """A real `TicketHolder` in a decided state, for tests about what goes out.

    A stub with `current` and `unavailable_reason` set independently can hold a
    pair the real state machine cannot produce — a ticket *and* a reason, or
    neither — so a test written against one proves less than it appears to.
    This winds the real object instead, and every read goes through the code
    under test.
    """
    now = 1_000_000.0
    holder = TicketHolder(_NeverAsked(), clock=lambda: now)
    if ticket is not None and reason is not None:
        raise AssertionError("a holder has a ticket or a reason, never both")
    if ticket is not None:
        holder._ticket = Token(ticket, now + 1800)
    elif reason == "expired":
        holder._ticket = Token("a-ticket-that-lapsed", now - 1)
    elif reason == "not-found":
        holder._no_ticket_reason = "not-found"
    elif reason not in (None, "issuer-unreachable"):
        raise AssertionError(f"no way to reach {reason!r} through the real object")
    return holder


#: Everything standalone's HTTP client itself puts on a forwarded tool call.
#: The identity headers are added per case, so a new name appearing on either
#: path fails rather than passing unnoticed. Standalone's own set: the Envoy
#: interface's is `ALLOWED_AGENT_HEADERS`, and reconciling the two is a change
#: of its own.
EXPECTED_OUTBOUND = frozenset(
    {
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
)

#: What a sandbox would send to pass itself off as identified, or to carry a
#: credential of its own upstream. None of it may cross.
FORGED = {
    "x-rail": "forged-by-the-sandbox",
    "x-rail-status": "forged",
    "authorization": "Bearer agent-secret",
}


@dataclass(frozen=True)
class XRailCase:
    """One row of the x-rail contract: a holder state, what the agent sent, and
    what the upstream must see as a result.

    Standalone's behaviour is the definition, so its tests run every row. An
    interface a row does not apply to names it in `not_for` with the reason,
    which `xrail_params` turns into a visible skip rather than a silent one.
    """

    name: str
    plugin: bool
    ticket: str | None = None
    reason: str | None = None
    agent_headers: Mapping[str, str] = field(default_factory=dict)
    #: Headers the upstream must see, with exactly these values.
    expected: Mapping[str, str] = field(default_factory=dict)
    #: Headers the upstream must not see at all.
    absent: frozenset[str] = frozenset()
    not_for: Mapping[str, str] = field(default_factory=dict)

    def holder(self) -> TicketHolder | None:
        """The holder this row is about: None where the plugin is off."""
        if not self.plugin:
            return None
        return wound_holder(ticket=self.ticket, reason=self.reason)


def _with_forged(case: XRailCase) -> XRailCase:
    """The same row with the agent forging every header it should not control.

    The expectations are the row's own, so a forged `x-rail` is overwritten by
    the real ticket or removed, never passed on, and `authorization` is absent.
    """
    return XRailCase(
        name=f"{case.name}, forged",
        plugin=case.plugin,
        ticket=case.ticket,
        reason=case.reason,
        agent_headers=FORGED,
        expected=case.expected,
        absent=case.absent | {"authorization"},
        not_for=case.not_for,
    )


_BASE_CASES = [
    # The feature: the sandbox never holds the credential identifying it, and
    # the upstream sees an identity the agent could not have supplied.
    XRailCase(
        name="ticket",
        plugin=True,
        ticket="rc_ticket_opaque",
        expected={"x-rail": "rc_ticket_opaque"},
        absent=frozenset({"x-rail-status"}),
    ),
    # Fail closed: the request still goes out, without an identity and saying
    # why. Refusing to forward would make an issuer outage an agent outage;
    # sending the reason in `x-rail` itself would turn absence into presence.
    *[
        XRailCase(
            name=reason,
            plugin=True,
            reason=reason,
            expected={"x-rail-status": reason},
            absent=frozenset({"x-rail"}),
        )
        for reason in ("not-found", "expired", "issuer-unreachable")
    ],
    # The plugin off. Not the fail-closed path: no status header either,
    # because nothing was attempted.
    XRailCase(
        name="pass-through",
        plugin=False,
        absent=frozenset({"x-rail", "x-rail-status"}),
    ),
]

#: Every row, plain and with forged agent headers.
XRAIL_CASES = [*_BASE_CASES, *(_with_forged(case) for case in _BASE_CASES)]


def xrail_params(interface: str) -> list:
    """`XRAIL_CASES` as pytest params for `interface`, ids by row name. A row
    that does not apply is skipped with the table's own reason."""
    return [
        pytest.param(
            case,
            id=case.name,
            marks=(
                [pytest.mark.skip(reason=case.not_for[interface])]
                if interface in case.not_for
                else []
            ),
        )
        for case in XRAIL_CASES
    ]
