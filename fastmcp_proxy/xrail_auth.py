"""Obtain this proxy's x-rail ticket from Rail Center, and put it on the wire.

The ticket is what identifies the agent this proxy serves to everything
downstream. `TicketSource` fetches one, `TicketHolder` keeps a valid one to
hand, and `XRailInjector` puts it on every outbound request.

The ticket is **opaque**: what is inside it is the gateway's contract, and
nothing here looks. Expiry therefore comes from `expires_at` alone, and an entry
without one is rejected rather than stored as never expiring — with nothing else
to read, "no expiry" would mean "valid for ever".

The wire contract is pinned in `spec/ticket-fetch.schema.json`, and
`tests/fixtures/tickets.json` is the instance the suite validates against it.

`redact_credentials` lives here too, because what it recognises is a credential
inside a URL — the same thing `_parse_base` and `describe` are careful about.
`proxy.RedactingFilter` is what installs it on every handler.
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


#: `scheme://` then everything up to the last `@` of an authority — greedy, so
#: a password containing its own `@` is taken whole rather than left with its
#: tail in the line.
#:
#: **Why this is matched on text rather than on a url object:** the messages
#: that carry a credential are written by libraries that never hand one over.
#: httpx logs `HTTP Request: %s %s …` at INFO, once per request, with the whole
#: url among the arguments — so the leak is at request rate, and redacting only
#: where this module formats a url would miss all of it.
#:
#: The scheme run is length-bounded, and the function below returns early on
#: text with no `://` in it. `[\w+.-]*://` is quadratic on a string that never
#: satisfies it — every start position tries every length — and this runs on
#: every record, on the loop that serves every mount. A record can carry text an
#: untrusted sandbox chose: the MCP transport logs a rejected `Content-Type`
#: verbatim, and its logger propagates to root. With the run bounded, both
#: quantifiers are, and the match is linear in the length of the line.
_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][\w+.-]{0,30}://)[^/\s]*@")


def redact_credentials(text: str) -> str:
    """Remove userinfo from any url in a log message.

    `user:pass@host` is how httpx is told to send Basic auth to an upstream, so
    a credential there is a working configuration rather than a mistake.
    """
    # An early out rather than a guard: with the scheme run bounded the match
    # is already linear, and most records have no url in them at all.
    if "://" not in text:
        return text
    return _USERINFO.sub(r"\g<scheme>***@", text)


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

    Every value reaching here is a `json.loads` result, a header string or an
    already-rendered message, so nothing arrives whose `repr` can raise.
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


#: What may appear in an HTTP header value: visible ASCII, space and tab.
#: RFC 9110 §5.5, minus the obsolete line folding.
_HEADER_SAFE = re.compile(r"[\x21-\x7e]([\x20-\x7e\t]*[\x21-\x7e])?")

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
        if auth_token and not _HEADER_SAFE.fullmatch(auth_token):
            # Refused at configuration rather than at the request, for the
            # reason the ticket is refused at `fetch`: h11 rejects a header
            # value carrying a newline, a NUL or a non-ASCII character when it
            # serialises the request, and the error it raises renders the whole
            # value — here, the credential. Refresh runs for the life of the
            # process, so left to h11 that message is re-printed at every
            # attempt rather than once, and `redact_credentials` cannot match a
            # bare token: there is no `scheme://…@` in it. The value is not
            # echoed, for the same reason the ticket's is not.
            raise ValueError(
                "RAIL_AUTH_TOKEN is not a valid header value "
                f"({len(auth_token)} characters)"
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
        concatenated onto. `https://rc.example.com#tail` appended to
        `/v1/tickets?host_id=…` puts the whole route inside the fragment, which
        is not sent: the request reaches `GET /` with no parameters at all —
        the unnamed, host-wide fetch this class exists to make impossible. A
        base with a query swallows `host_id` into a neighbouring value for the
        same reason. An *empty* query or fragment is not refused; rebuilding
        from the parts drops it, so the route survives.

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

        `httpx.Timeout` is per-operation and its read budget re-arms on every
        chunk, so an issuer dribbling a byte at a time is bounded only by the
        cap — 256k reads. That applies to every fetch this makes: the one at
        startup and every refresh for the life of the process after it.
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
        # Every non-2xx is a fetch failure. The empty list is the only
        # authoritative "no ticket exists" — `NoTicketAvailable` says why the
        # two must not be conflated.
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
        if not _HEADER_SAFE.fullmatch(value):
            # Refused here rather than at injection. A ticket carrying a
            # newline, a NUL or a surrogate is rejected by h11 when the request
            # is serialised — but the error it raises renders the whole value,
            # and the module's one rule is that the ticket is never logged. A
            # surrogate does not even survive `token_fingerprint`, so storing
            # one takes down the refresh loop and `/health` with it. The value
            # is not echoed: there is nothing to say about it that its length
            # does not say.
            raise ValueError(
                f"the ticket is not a valid header value ({len(value)} characters)"
            )

        if entry.get("expires_at") is None:
            raise ValueError("the ticket carried no expires_at")
        expires_at = parse_expires_at(entry["expires_at"])
        check_lifetime(expires_at - sent_at, self.max_lifetime_seconds)
        return Token(value, expires_at)


class TicketHolder:
    """Keeps a valid ticket to hand, and says why there is none when there is not.

    Refresh is best-effort. A failed fetch keeps the ticket already held *while
    it is still valid*, because an issuer being briefly unreachable says nothing
    about whether this proxy's identity is still good. Once nothing valid is
    left, `current` is None and the injector fails closed.

    The exception is an authoritative empty answer, which clears the held
    ticket outright — see `NoTicketAvailable`.
    """

    #: Never poll the issuer faster than this, however short a ticket's life.
    MIN_REFRESH_INTERVAL_SEC = 5.0
    #: Refresh once this fraction of the remaining lifetime has elapsed.
    REFRESH_RATIO = 0.5

    # Constants rather than settings, deliberately. A ratio at 0.99 injects
    # tickets about to expire, and a floor at 0.1 lets every proxy in a fleet
    # hammer Rail Center — neither is a preference an operator should be able to
    # express, and both fail in a way nothing local would notice.

    def __init__(
        self,
        source: TicketSource,
        *,
        refresh_seconds: float = 3600.0,
        clock: Clock = time.time,
    ) -> None:
        self.source = source
        self.refresh_seconds = refresh_seconds
        self.clock = clock
        self._ticket: Token | None = None
        self._last_ok_at: float = 0.0
        #: How long this proxy has had no usable identity, counted in attempts
        #: — not how many times Rail Center failed to answer. A fetch that
        #: succeeds and returns an already-expired ticket counts; a failure
        #: while a valid ticket is still held does not. It drives the retry
        #: delay alone, so the ramp restarts when a ticket lapses rather than
        #: carrying over failures accumulated while one was still good, which
        #: would put the first retry after losing an identity at the ceiling.
        self._failures = 0
        #: Set only by an authoritative "no ticket", never by a fetch failure.
        self._no_ticket_reason: str | None = None
        self._task: asyncio.Task[None] | None = None

    def snapshot(self) -> tuple[str | None, str | None]:
        """The ticket to inject and the reason there is none — exactly one of
        which is set.

        Both come from a single read of the ticket and a single read of the
        clock. Asking `current` and then `unavailable_reason` samples the clock
        twice, and this module deliberately uses wall time rather than a
        monotonic one, because an expiry is an absolute instant: an NTP step
        backwards between the two reads answers "no ticket" to the first and
        "no reason" to the second, and the injector then writes a header whose
        value is None.
        """
        ticket = self._ticket
        now = self.clock()
        if ticket is not None and ticket.is_valid(now):
            return ticket.value, None
        if self._no_ticket_reason:
            return None, self._no_ticket_reason
        if ticket is not None:
            return None, "expired"
        return None, "issuer-unreachable"

    @property
    def current(self) -> str | None:
        """The ticket to inject, or None when nothing valid is held."""
        return self.snapshot()[0]

    @property
    def unavailable_reason(self) -> str | None:
        """Why `current` is None — the value `x-rail-status` carries.

        Three states an operator must be able to tell apart, because they call
        for different responses: `not-found` is Rail Center saying this agent
        has no ticket, `expired` is one that lapsed with no replacement, and
        `issuer-unreachable` is having no current answer — either none has ever
        landed, or the last one has been superseded by a failure to reach the
        issuer at all.
        """
        return self.snapshot()[1]

    @property
    def status(self) -> dict[str, Any]:
        """What `/health` reports. Never the ticket itself."""
        now = self.clock()
        ticket = self._ticket
        return {
            "ticket_held": ticket is not None,
            "ticket_valid": bool(ticket and ticket.is_valid(now)),
            "unavailable_reason": self.unavailable_reason,
            "fingerprint": token_fingerprint(ticket.value) if ticket else None,
            "expires_in_sec": round(ticket.remaining(now)) if ticket else None,
            # No `source`. This is served on `/health`, by the same listener the
            # sandbox reaches for `/mcp`, and `describe()` renders the whole
            # fetch route — the Rail Center address, both query parameters that
            # key it, and whether it needs a credential at all. An operator has
            # it already: `start` logs it once, where the sandbox cannot read.
            "last_refresh_age_sec": (
                round(now - self._last_ok_at) if self._last_ok_at else None
            ),
            "next_refresh_in_sec": round(self.next_refresh_delay()),
        }

    def next_refresh_delay(self) -> float:
        """Seconds to wait before refreshing again.

        Keyed on "nothing usable is held" rather than on the ticket being
        absent: an expired one is never cleared, so a proxy whose issuer died
        would otherwise reach the branch below with a negative remaining life,
        pin to the floor and hammer Rail Center every few seconds — which is
        the case the backoff exists for.
        """
        ticket = self._ticket
        if ticket is None or not ticket.is_valid(self.clock()):
            # Injection is failing closed, so retry promptly — but back off, or
            # a persistently failing issuer is asked forever at the floor.
            backoff = self.MIN_REFRESH_INTERVAL_SEC * (2 ** min(self._failures, 6))
            # Floored as well as capped. `refresh_seconds` is an operator's
            # value and may be under the floor, and this branch is entered by
            # exactly the condition the floor exists for — nothing usable held,
            # every proxy in the fleet asking at once, Rail Center denied its
            # own recovery.
            return max(
                self.MIN_REFRESH_INTERVAL_SEC, min(self.refresh_seconds, backoff)
            )

        # Half the remaining life, so a ticket shorter-lived than the configured
        # interval is replaced before it lapses rather than after.
        return max(
            self.MIN_REFRESH_INTERVAL_SEC,
            min(
                self.refresh_seconds,
                ticket.remaining(self.clock()) * self.REFRESH_RATIO,
            ),
        )

    async def refresh_once(self) -> bool:
        """Fetch a ticket. Returns False rather than raising on failure."""
        try:
            ticket = await self.source.fetch()
        except NoTicketAvailable as exc:
            previous = self._ticket
            self._ticket = None
            self._no_ticket_reason = exc.reason
            self._failures += 1
            log.warning(
                "Rail Center holds no ticket (%s) — %s; x-rail-status will "
                "report %r (retry in %.0fs)",
                _clip(redact_credentials(str(exc)), 300),
                "cleared the one held" if previous is not None else "none was held",
                exc.reason,
                self.next_refresh_delay(),
            )
            return False
        except Exception as exc:
            # The issuer failed to answer, so its last authoritative answer is
            # no longer what is being reported: `not-found` means Rail Center
            # said this agent has no ticket, and it has now said nothing at all.
            self._no_ticket_reason = None
            held = self._ticket
            if held is not None and held.is_valid(self.clock()):
                outcome = "keeping the ticket held until it expires"
            else:
                self._failures += 1
                if held is not None:
                    outcome = (
                        "the ticket held has expired — injection is failing closed"
                    )
                elif self._last_ok_at:
                    outcome = "nothing is held — injection is failing closed"
                else:
                    outcome = (
                        "no ticket has ever been fetched — injection is failing closed"
                    )
            # Redacted before it is cut: `_USERINFO` needs the closing `@`, so
            # a cut landing inside a password leaves a stump nothing matches.
            detail = redact_credentials(str(exc))[:300]
            log.warning(
                "ticket refresh failed (%s) — %s (retry in %.0fs)",
                f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__,
                outcome,
                self.next_refresh_delay(),
            )
            return False

        self._no_ticket_reason = None
        previous = self._ticket

        now = self.clock()
        if not ticket.is_valid(now) and previous is not None and previous.is_valid(now):
            # An answer worse than no answer. A failed fetch keeps a ticket that
            # is still valid, so a *successful* one returning an already-lapsed
            # ticket must not be the thing that discards it — that fails closed
            # for whatever life the held one had left, over an issuer bug or a
            # clock skew this proxy could have ridden out.
            #
            # Neither counter moves. `_failures` counts attempts made with no
            # usable identity, and one is held; `_last_ok_at` is what `/health`
            # renders as the age of the last good refresh, and nothing good came
            # back — stamping it here reports a fresh identity in the one state
            # an operator most needs to catch.
            log.warning(
                "Rail Center returned an already-expired ticket "
                "(fingerprint=%s); keeping the one held, which has %.0fs left",
                token_fingerprint(ticket.value),
                previous.remaining(now),
            )
            return True

        self._ticket = ticket
        fingerprint = token_fingerprint(ticket.value)
        if ticket.is_valid(now):
            self._last_ok_at = now
            self._failures = 0
        else:
            # A fetch that succeeds and yields nothing usable still counts
            # against the ramp — `_failures` says why. `_last_ok_at` is left
            # alone for the reason the branch above gives: it is the age
            # `/health` renders for the last *good* refresh, and nothing good
            # came back.
            self._failures += 1
        if not ticket.is_valid(now):
            # A ticket already past its expiry is a well-formed answer, so it
            # arrives here rather than as an error. Announcing it as acquired
            # would report a dead identity as a healthy one — and `current` is
            # about to return None for it, so injection fails closed while the
            # log said the fetch succeeded.
            log.warning(
                "Rail Center returned an already-expired ticket "
                "(fingerprint=%s, expired %.0fs ago) — injection is failing closed",
                fingerprint,
                -ticket.remaining(now),
            )
        elif previous is None:
            log.info(
                "ticket acquired (fingerprint=%s, expires in %.0fs)",
                fingerprint,
                ticket.remaining(now),
            )
        elif previous.value != ticket.value:
            log.info(
                "ticket rotated (fingerprint=%s, expires in %.0fs)",
                fingerprint,
                ticket.remaining(now),
            )
        else:
            log.debug("ticket refreshed, unchanged")
        return True

    async def start(self) -> None:
        """Fetch once, then keep it fresh in the background.

        The first fetch is awaited, so a wrong sandbox name or a rejected
        credential is visible at startup rather than at the first call that
        needed an identity. It is never fatal: the proxy comes up either way and
        fails closed until a ticket lands.
        """
        log.info("fetching this proxy's ticket from %s", self.source.describe())
        await self.refresh_once()
        self._task = asyncio.create_task(self._refresh_loop())

    async def aclose(self) -> None:
        """Stop refreshing. Idempotent, and safe before `start`.

        Nothing the refresh loop raises escapes here. `main` calls this from a
        `finally`, so such an exception would replace the shutdown it was
        tidying up after with a traceback — including, on an already-finished
        task, whatever that task died of. A cancellation aimed at the *caller*
        does escape; the comment on that branch says why.
        """
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # Only the loop's own cancellation is swallowed. One delivered to
            # *this* task is somebody stopping the caller, and dropping it here
            # would let a cancelled shutdown carry on as though it were clean.
            #
            # `Task.cancelling()` arrived in 3.11 and this project's floor is
            # 3.10, where there is no way to tell the two apart — so on the
            # floor the loop's cancellation is what this is assumed to be,
            # which is the case that actually happens.
            current = asyncio.current_task()
            if current is not None and getattr(current, "cancelling", int)():
                raise
        except Exception as exc:
            log.error("the ticket refresh loop had already failed: %r", exc)

    async def _refresh_loop(self) -> None:
        """Refresh until cancelled.

        The body is guarded because nothing supervises this task. An exception
        escaping it ends refreshing for the life of the process — silently,
        since a task nobody awaits swallows what killed it — and the proxy then
        fails closed for ever with nothing in the log saying why.
        """
        while True:
            try:
                await asyncio.sleep(self.next_refresh_delay())
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Logged with a traceback and retried at the floor: this is a
                # defect in the proxy rather than a condition at the issuer, and
                # the one thing that must not happen is stopping.
                log.exception("the ticket refresh loop raised; retrying")
                await asyncio.sleep(self.MIN_REFRESH_INTERVAL_SEC)


class XRailInjector(httpx.Auth):
    """Puts the held ticket on every outbound request.

    httpx calls `auth_flow` per request, so a rotation is picked up without
    restarting anything.

    With no valid ticket the request still goes out — `x-rail` omitted, and the
    reason in `x-rail-status`. That is the fail-closed path: an expired or
    absent ticket is never sent, and enforcement belongs to the gateway, where
    an absent `x-rail` denies. The status header is what lets it tell a managed
    agent whose ticket lapsed from one running with no proxy at all.

    The reason never goes in `x-rail` itself. That would turn absence into
    presence and make this component an author of ticket content.
    """

    #: A protocol constant, not a setting: the gateway looks for exactly this.
    #: A configurable name fails silently — injection logs success, the gateway
    #: sees no `x-rail`, every call is refused, and nothing names the cause.
    HEADER = "x-rail"
    #: Where an omitted `x-rail` is explained. Advisory and caller-supplied, so
    #: the gateway must never let it contribute to an allow.
    STATUS_HEADER = "x-rail-status"

    def __init__(self, holder: TicketHolder) -> None:
        self.holder = holder
        #: The last reason warned about. One agent tool call opens a fresh
        #: upstream session — initialize, notifications/initialized, tools/list,
        #: tools/call — so a per-request warning lets a sandbox in a loop choose
        #: this process's log volume during exactly the outage an operator needs
        #: to read about. Warned on the transition, DEBUG for the rest.
        self._warned: str | None = None

    def auth_flow(self, request: httpx.Request):
        ticket, reason = self.holder.snapshot()
        if ticket is not None:
            request.headers[self.HEADER] = ticket
            self._warned = None
            log.debug(
                "outbound %s — injected %s (fingerprint=%s)",
                request.url,
                self.HEADER,
                token_fingerprint(ticket),
            )
        else:
            request.headers[self.STATUS_HEADER] = reason
            at = log.warning if reason != self._warned else log.debug
            self._warned = reason
            at(
                "outbound %s — no valid ticket: %s omitted, %s=%s",
                request.url,
                self.HEADER,
                self.STATUS_HEADER,
                reason,
            )
        yield request  # the httpx.Auth contract
