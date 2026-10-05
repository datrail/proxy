"""Helpers for the proxy-core suite, and for the members built on proxy-core."""

from __future__ import annotations

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
