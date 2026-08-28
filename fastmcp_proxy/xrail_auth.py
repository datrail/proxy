"""Obtain this proxy's x-rail ticket from Rail Center.

The ticket is what identifies the agent this proxy serves to everything
downstream. This module fetches one and says whether it is still good; putting
it on an outbound request is not implemented here.

The ticket is **opaque**: what is inside it is the gateway's contract, and
nothing here looks. Expiry therefore comes from `expires_at` alone, and an entry
without one is rejected rather than stored as never expiring — with nothing else
to read, "no expiry" would mean "valid for ever".

The wire contract is pinned in `spec/ticket-fetch.schema.json`, and
`tests/fixtures/tickets.json` is the instance the suite validates against it.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

log = logging.getLogger("fastmcp_proxy.xrail")

#: Largest ticket response this proxy will read. A named fetch answers with at
#: most one ticket, so a body past this is not a big answer but a wrong one, and
#: reading it to the end would let the issuer choose this process's memory use.
MAX_RESPONSE_BYTES = 256 * 1024

Clock = Callable[[], float]


class NoTicketAvailable(Exception):
    """Rail Center answered, and the answer is that no ticket exists.

    Distinct from a fetch *failure* on purpose, because the two mean opposite
    things: a failure says nothing about whether a ticket exists, while an empty
    list is the complete answer for that host and sandbox. Conflating them makes
    "the issuer is down" indistinguishable from "you have no identity".

    ``reason`` is a short label for the answer, for a caller deciding what to
    do about it.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(detail or reason)


def is_loopback(host: str | None) -> bool:
    """True only for hosts whose traffic never leaves the machine.

    `host.docker.internal` deliberately does not count: it resolves to the
    host's bridge address, so the request crosses a virtual network other
    containers sit on.
    """
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def token_fingerprint(value: str) -> str:
    """A short, stable, non-reversible label for a ticket — safe to log.

    The ticket must never be logged, but an operator still needs to see *that*
    it rotated, so a digest prefix stands in for it.
    """
    return hashlib.sha256(value.encode()).hexdigest()[:12]


class Token:
    """A ticket and the moment it stops being valid."""

    __slots__ = ("expires_at", "value")

    def __init__(self, value: str, expires_at: float) -> None:
        self.value = value
        self.expires_at = expires_at

    def is_valid(self, now: float) -> bool:
        return now < self.expires_at

    def remaining(self, now: float) -> float:
        return self.expires_at - now

    def __repr__(self) -> str:  # never render the value itself
        return f"Token(fingerprint={token_fingerprint(self.value)}, expires_at={self.expires_at})"


def _clip(value: Any, limit: int = 120) -> str:
    """Render an issuer-controlled value for a message without letting it set
    the size *or the cost* of that message.

    Truncating after `repr()` bounds only the output: the whole value is still
    materialised first, synchronously, on the loop that serves every mounted
    upstream. A large enough value therefore stalls all proxied traffic, not
    only the fetch that hit it. So containers are summarised without being
    rendered, strings are sliced before `repr`, and a huge integer is described
    by its size rather than converted.

    Every value reaching here is either a `json.loads` result or a header
    string, which is what makes those cases the whole set — nothing arrives
    whose `repr` can raise.
    """
    if isinstance(value, str):
        return repr(value[:limit] + "…") if len(value) > limit else repr(value)
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return f"<{type(value).__name__} of {len(value)} items>"
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value.bit_length() > 256
    ):
        # int → str is superlinear, and CPython refuses past
        # sys.set_int_max_str_digits anyway.
        return f"<int of {value.bit_length()} bits>"
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _describe_keys(mapping: Mapping[str, Any], sample: int = 5) -> str:
    """Name a few of a mapping's keys without echoing all of them.

    The key count is the issuer's to choose and is bounded only by the response
    cap, and the description goes into an exception that is logged: rendering
    all of them turns a 20k-key response into a log line hundreds of kilobytes
    long. Only the first few are rendered at all, each clipped.
    """
    shown = [_clip(key, 40) for _, key in zip(range(sample), mapping)]
    more = len(mapping) - len(shown)
    return f"{len(mapping)} keys: {', '.join(shown)}" + (
        f", +{more} more" if more > 0 else ""
    )


def parse_expires_at(value: Any) -> float:
    """Read an `expires_at` stamp — absolute, UTC, ISO 8601 — or reject it.

    Rail Center emits `"2026-07-28T10:15:00Z"`: whole seconds, always UTC.
    Anything unparseable is rejected rather than ignored, because ignoring it
    would store the ticket with no expiry at all, which this module treats as
    valid for ever — and nothing in the logs would say so.

    A date with no time of day is refused rather than read as midnight. RFC
    3339 requires the time, and an instant a whole day earlier than the one the
    issuer meant is not a rounding error.

    A stamp in the *past* is not malformed. An expired ticket is a legitimate
    answer, and the caller turns it into the fail-closed path.

    A naive stamp is off-contract and read as UTC rather than refused: the
    field is always UTC where it carries a zone, and the alternative reading —
    the proxy's own — shifts the expiry by the whole offset, which is hours of
    treating a dead ticket as live.
    """
    if not isinstance(value, str):
        raise ValueError(f"expires_at is not an ISO 8601 string: {_clip(value)}")
    if "T" not in value and "t" not in value:
        raise ValueError(f"expires_at names no time of day: {_clip(value)}")
    try:
        # Two rewrites, both for the 3.10 floor, where `fromisoformat` reads
        # only what `isoformat` writes. It learns the trailing-Z spelling in
        # 3.11, and Z is what Rail Center emits — without the first, 3.10 parses
        # no ticket at all. It accepts 3- or 6-digit fractional seconds only,
        # while RFC 3339 allows any number of them, so the second pads or
        # truncates to microseconds, which is all a ticket expiry needs.
        stamp = value[:-1] + "+00:00" if value[-1:] in ("Z", "z") else value
        stamp = _FRACTION.sub(lambda m: f".{m.group(1)[:6]:0<6}", stamp, count=1)
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        raise ValueError(f"expires_at is not parseable: {_clip(value)}") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


#: The fractional-seconds group of an ISO 8601 stamp.
_FRACTION = re.compile(r"\.(\d+)")

#: Lifetime beyond which a *warning* is logged. Not a limit: x-rail v1 puts no
#: maximum on a ticket's life and its gateway re-checks liveness per request, so
#: a long-lived ticket is something the design tolerates. Refusing one here would
#: lock out a compliant issuer, so this only makes it visible.
IMPLAUSIBLE_TICKET_LIFETIME_SEC = 30 * 24 * 3600


def check_lifetime(seconds: float, max_lifetime: float | None) -> None:
    """Reject a lifetime past the operator's bound, and warn on one long enough
    to be effectively non-expiring. The two thresholds are unrelated: the bound
    is whatever was configured, and the warning is this module's own."""
    if max_lifetime is not None and seconds > max_lifetime:
        raise ValueError(
            f"expires_at is {seconds:.0f}s away, beyond the configured maximum "
            f"of {max_lifetime:.0f}s"
        )
    if seconds > IMPLAUSIBLE_TICKET_LIFETIME_SEC:
        log.warning(
            "ticket expires in %.0fs (%.1f days) — close enough to non-expiring "
            "to be worth checking; set a maximum lifetime to reject it",
            seconds,
            seconds / 86400,
        )


class TicketSource:
    """Reads this proxy's own ticket from `GET /v1/tickets`.

    The route is keyed on the host, because an `agent_id` is Rail Center's to
    assign and a caller cannot start with one; `(host_id, sandbox_name)` is what
    this proxy knows about itself.

    **`sandbox_name` is required rather than optional, and this class is where
    that is decided.** An unnamed fetch answers for the whole host, and there is
    then no way to tell which entry belongs to *this* proxy — a host whose
    single registered ticket is another sandbox's would silently run all traffic
    under that agent's identity. The same reason makes an entry that names no
    sandbox unusable rather than merely unlabelled: it is what an issuer that
    ignored the narrowing returns.
    """

    def __init__(
        self,
        base_url: str,
        *,
        host_id: str,
        sandbox_name: str,
        auth_token: str | None = None,
        timeout_seconds: float = 10.0,
        max_lifetime_seconds: float | None = None,
        allow_insecure_credential: bool = False,
        clock: Clock = time.time,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not host_id or not sandbox_name:
            raise ValueError(
                "both host_id and sandbox_name are required: an unnamed fetch "
                "cannot prove which entry is this proxy's own ticket"
            )
        base, safe_base = self._parse_base(base_url)
        query = urlencode({"host_id": host_id, "sandbox_name": sandbox_name})
        self.url = f"{base}/v1/tickets?{query}"
        self.safe_url = f"{safe_base}/v1/tickets?{query}"
        self.host_id = host_id
        self.sandbox_name = sandbox_name
        self.auth_token = auth_token
        self.timeout_seconds = timeout_seconds
        self.max_lifetime_seconds = max_lifetime_seconds
        self.clock = clock
        self._transport = transport
        if auth_token and self.url_credential:
            # httpx builds Basic auth from userinfo and it overwrites the
            # Authorization header this class sets, so the bearer would never
            # leave the process while every log line said it had.
            raise ValueError(
                "RAIL_CENTER_URL carries a credential and RAIL_AUTH_TOKEN is "
                "set; the URL's would win silently. Configure one."
            )
        if (auth_token or self.url_credential) and not allow_insecure_credential:
            self._reject_plaintext_credential()
        elif self.in_the_clear:
            # Either no credential to refuse over, or one the operator chose to
            # send anyway. The ticket comes back on the wire in both cases, and
            # it is the more valuable secret: it is what everything downstream
            # trusts. A warning rather than a refusal, because an
            # unauthenticated plaintext control plane is the shape a local
            # stack takes.
            log.warning(
                "fetching this proxy's ticket over plaintext http from %s — "
                "anyone on that path reads %s",
                self.safe_url,
                "the credential and the ticket it returns"
                if (auth_token or self.url_credential)
                else "the ticket it returns",
            )

    def _parse_base(self, base_url: str) -> tuple[str, str]:
        """The scheme, authority and path of the configured base, in the form
        that goes on the wire and the form that is safe to render.

        Also where `url_credential` and `in_the_clear` are decided, because both
        are properties of the base and this is the only place it is parsed.

        A base carrying its own query or fragment is refused rather than
        concatenated onto. `https://rc.example.com#` appended to
        `/v1/tickets?host_id=…` puts the whole route inside the fragment, which
        is not sent: the request reaches `GET /` with no parameters at all —
        the unnamed, host-wide fetch this class exists to make impossible. A
        base with a query swallows `host_id` into a neighbouring value for the
        same reason.

        The scheme and host are checked here rather than left to httpx, because
        `rail-center:8000` — the packaged example minus its scheme — parses as
        scheme `rail-center` with no host, and would otherwise surface as a
        fetch failure at startup instead of the configuration error it is.
        """
        try:
            parts = urlsplit(base_url)
        except ValueError:
            # urlsplit refuses a netloc that fails NFKC normalisation, and it
            # puts the netloc in the message — which is the userinfo this whole
            # method exists to keep out of a log line.
            raise ValueError("RAIL_CENTER_URL is not a URL") from None
        # Rendered without its userinfo. The raw string is what an operator
        # typed, and what they typed may be a password. A value missing its
        # `//` has no authority at all, so `user:pass@host` lands in the path —
        # and the forgotten scheme is the likeliest hand-edit there is, which
        # makes that the case this must handle rather than the exotic one.
        shown = urlunsplit(
            (
                parts.scheme,
                parts.netloc.rpartition("@")[2],
                parts.path.rpartition("@")[2],
                "",
                "",
            )
        )
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(
                f"RAIL_CENTER_URL must be an http(s) URL with a host: {shown!r}"
            )
        if parts.query or parts.fragment:
            raise ValueError(
                "RAIL_CENTER_URL must carry no query or fragment; it is a base "
                f"the ticket route is appended to: {shown!r}"
            )
        self.url_credential = bool(parts.username or parts.password)
        self.in_the_clear = parts.scheme != "https" and not is_loopback(parts.hostname)
        path = parts.path.rstrip("/")
        # Two forms: the one that goes on the wire, and the one that is safe to
        # render. They differ only in the userinfo a password can hide in.
        return (
            urlunsplit((parts.scheme, parts.netloc, path, "", "")),
            urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], path, "", "")),
        )

    def _reject_plaintext_credential(self) -> None:
        """Refuse to put a credential on the wire in the clear.

        An attacker on the path captures both the credential used to fetch and
        the ticket it returns — enough to impersonate this agent to everything
        that trusts x-rail. Loopback is exempt, because that traffic never
        leaves the machine.

        Userinfo in `RAIL_CENTER_URL` counts: httpx turns it into a real
        `Authorization: Basic` header, so a base URL alone can put a secret on a
        plaintext connection with no `RAIL_AUTH_TOKEN` set anywhere.
        """
        if not self.in_the_clear:
            return
        parts = urlsplit(self.safe_url)
        named = (
            "RAIL_AUTH_TOKEN"
            if self.auth_token
            else "the credential in RAIL_CENTER_URL"
        )
        raise ValueError(
            f"refusing to send {named} to {parts.scheme}://{parts.hostname} "
            "in the clear — use https, a loopback address, or set "
            "RAIL_PROXY_ALLOW_INSECURE_CREDENTIAL=true to override"
        )

    def describe(self) -> str:
        """A label for logs that carries neither the token nor a URL password."""
        if self.auth_token:
            state = "bearer"
        elif self.url_credential:
            state = "url-credential"
        else:
            state = "unauthenticated"
        return f"{self.safe_url} ({state})"

    async def _request(self) -> tuple[httpx.Response, float]:
        """One exchange, under one deadline.

        `httpx.Timeout` is per-operation and its read budget re-arms on every chunk, so an issuer
        dribbling a byte at a time is bounded only by the cap — 256k reads,
        during which the listener is not yet bound.
        """
        return await asyncio.wait_for(self._exchange(), self.timeout_seconds)

    async def _exchange(self) -> tuple[httpx.Response, float]:
        """One GET, plus the clock sampled *before* sending.

        An expiry measured against a post-response timestamp would credit the
        ticket with the round trip it already spent in flight.

        The body is streamed and capped rather than buffered whole. A response
        is issuer-controlled, and `client.get` would read one of any size into
        memory — and then parse it synchronously on the loop that serves every
        mounted upstream — before any of the guards below could describe it.

        Uncompressed, and refused if it is not. `Accept-Encoding` is a request
        hint the issuer may ignore, and `aiter_bytes` yields *decoded* bytes —
        so under gzip a 66 KB body expands past the cap having already been
        materialised, and the issuer chooses this process's memory use through
        the ratio rather than the length. The `Content-Encoding` check arrives
        with the headers, before a byte of body is read, which is what makes
        the cap a bound on allocation rather than on the number it prints. A
        ticket response is small JSON; compressing it buys nothing to weigh
        against that.

        `trust_env=False`, so `HTTP_PROXY` and its neighbours cannot redirect
        this. httpx applies them to loopback addresses too, and a proxy
        variable set in a base image or a pod spec would otherwise turn the one
        destination the plaintext guard exempts — traffic that never leaves the
        machine — into a hop across the network carrying both the credential and
        the ticket.

        The same flag governs where httpx finds its roots, so the context is
        built separately with the environment left on. The two are not
        symmetric, which is the whole reason to separate them: `SSL_CERT_FILE`
        is set on purpose, by an operator putting an internal CA in front of
        their own Rail Center, and refusing to read it would refuse the private
        https this guard exists to push them towards. `HTTP_PROXY` is set for
        unrelated reasons, by base images and cluster defaults, and applies to
        loopback — it redirects deployments nobody meant to redirect.

        What comes back is a detached response carrying the capped body and no
        headers, so nothing the issuer sent in a header can reach a rendered
        message: `raise_for_status` otherwise writes a 3xx `Location` into it
        verbatim, and `reason_phrase` is the server's string.
        """
        headers = {"Accept-Encoding": "identity"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        sent_at = self.clock()
        async with (
            httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self._transport,
                trust_env=False,
                verify=httpx.create_ssl_context(trust_env=True),
            ) as client,
            client.stream("GET", self.url, headers=headers) as streamed,
        ):
            encoding = streamed.headers.get("content-encoding", "identity")
            if encoding.strip().lower() not in ("identity", ""):
                raise ValueError(
                    f"the response is {_clip(encoding, 40)}-encoded; "
                    "a ticket fetch reads only identity"
                )
            body = bytearray()
            async for chunk in streamed.aiter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError(
                        "the response is longer than the "
                        f"{MAX_RESPONSE_BYTES} bytes a ticket fetch reads; "
                        "abandoned unread"
                    )
            detached = httpx.Response(
                streamed.status_code,
                content=bytes(body),
                request=streamed.request,
            )
        return detached, sent_at

    async def fetch(self) -> Token:
        """The current ticket, or a reason there is none.

        `NoTicketAvailable` where Rail Center answered and the answer is that
        none exists — see its docstring for what separates that from a failure.
        `ValueError` where the answer did not match the contract. A failure is
        whatever httpx, `json` or `asyncio` raise on the way: an issuer can be
        unreachable in as many ways as they have exceptions, which is why the
        caller catches broadly rather than by name.
        """
        response, sent_at = await self._request()
        # Every non-2xx is a fetch failure and none is an authoritative empty
        # answer. The empty list is the only "no ticket exists" the contract
        # defines, and reading a status as one instead would clear a ticket the
        # issuer never said to clear.
        response.raise_for_status()

        envelope = response.json()
        if not isinstance(envelope, Mapping):
            raise ValueError("the response was not a JSON object")

        # The echo the contract requires, checked rather than discarded. It is
        # the one field that says which host the answer describes, so a
        # misrouted or cached response is caught before its ticket is read.
        echoed = envelope.get("host_id")
        if echoed != self.host_id:
            raise ValueError(
                f"the response answers for host {_clip(echoed)}, not the "
                f"{self.host_id!r} it was asked about"
            )

        tickets = envelope.get("tickets")
        if not isinstance(tickets, list):
            raise ValueError(
                f"the response carried no tickets list ({_describe_keys(envelope)})"
            )
        if not tickets:
            raise NoTicketAvailable(
                "not-found",
                f"Rail Center holds no current ticket for host_id={self.host_id!r}, "
                f"sandbox_name={self.sandbox_name!r}",
            )
        if len(tickets) > 1:
            # `maxItems: 1` in the schema. More means the issuer did not honour
            # the narrowing, and choosing one would be guessing at an identity.
            raise ValueError(
                f"{len(tickets)} tickets for a named fetch of "
                f"sandbox_name={self.sandbox_name!r} — the narrowing was not honoured"
            )

        entry = tickets[0]
        if not isinstance(entry, Mapping):
            raise ValueError(f"the ticket entry is not an object: {_clip(entry)}")

        # The ownership check the count alone cannot give; the class docstring
        # says why it is not optional.
        entry_sandbox = entry.get("sandbox_name")
        if entry_sandbox != self.sandbox_name:
            raise ValueError(
                f"the ticket names sandbox {_clip(entry_sandbox)}, not the "
                f"{self.sandbox_name!r} this proxy serves — refusing another "
                "agent's identity"
            )

        value = entry.get("token")
        if not isinstance(value, str) or not value:
            raise ValueError(f"the ticket carried no token ({_describe_keys(entry)})")

        if entry.get("expires_at") is None:
            raise ValueError("the ticket carried no expires_at")
        expires_at = parse_expires_at(entry["expires_at"])
        check_lifetime(expires_at - sent_at, self.max_lifetime_seconds)
        return Token(value, expires_at)
