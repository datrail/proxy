"""Obtaining this proxy's own ticket from Rail Center.

The wire contract is `spec/ticket-fetch.schema.json`; `tests/fixtures/tickets.json`
is the instance, and the first test here is what keeps the two in step.
"""

from __future__ import annotations

import base64
import json
import pathlib
from typing import Any

import httpx
import jsonschema
import pytest

from fastmcp_proxy.xrail_auth import (
    MAX_RESPONSE_BYTES,
    NoTicketAvailable,
    TicketHolder,
    TicketSource,
    Token,
    XRailInjector,
    check_lifetime,
    is_loopback,
    parse_expires_at,
    token_fingerprint,
)

SPEC = pathlib.Path(__file__).resolve().parents[1] / "spec" / "ticket-fetch.schema.json"
FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "tickets.json"

#: An injected clock, so `2026-07-28T10:15:00Z` in the fixture is a fixed
#: instant rather than a value that rots. `BEFORE_FIXTURE_EXPIRY` is what every
#: source reads by default, which makes a fixture ticket live; `FAR_FUTURE` is
#: for the tests that need it dead.
FAR_FUTURE = 4_102_444_800.0  # 2100-01-01T00:00:00Z
BEFORE_FIXTURE_EXPIRY = (
    1_785_233_000.0  # 2026-07-28T10:03:20Z, inside the fixture's life
)


def _payload(**overrides: Any) -> dict[str, Any]:
    """The fixture, with one ticket entry adjusted."""
    body = json.loads(FIXTURE.read_text())
    body["tickets"][0].update(overrides)
    return body


def _source(handler, **kwargs: Any) -> TicketSource:
    return TicketSource(
        "https://rail-center.invalid",
        host_id=kwargs.pop("host_id", "e2e-host"),
        sandbox_name=kwargs.pop("sandbox_name", "e2e-sandbox"),
        clock=lambda: BEFORE_FIXTURE_EXPIRY,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _answers(body: Any, status: int = 200):
    return lambda request: httpx.Response(status, json=body)


def _validate(instance: Any) -> None:
    """Validate with `format` asserted, not merely annotated.

    Draft 2020-12 treats `format` as an annotation, and `jsonschema.validate`
    honours that: without a checker `expires_at` may be any string at all, so
    the one field whose shape decides whether a ticket is live would be the one
    the published contract said nothing about.
    """
    schema = json.loads(SPEC.read_text())
    jsonschema.validate(instance, schema, format_checker=jsonschema.FormatChecker())


def test_the_fixture_matches_the_published_schema():
    """The schema is the published contract and the fixture is what the tests
    around it are built on. Validating one against the other is what stops the
    two drifting into separate ideas of the same response."""
    _validate(json.loads(FIXTURE.read_text()))


@pytest.mark.parametrize(
    ("field", "value"),
    [("expires_at", "tomorrow-ish"), ("token", "")],
    ids=["prose-expiry", "empty-token"],
)
def test_the_schema_rejects_what_the_parser_rejects(field, value):
    """If these validate, the schema is published as though the issuer may send
    them while the proxy refuses them anyway — two documents disagreeing about
    the same response."""
    with pytest.raises(jsonschema.ValidationError):
        _validate(_payload(**{field: value}))


@pytest.mark.asyncio
async def test_the_single_entry_is_read_and_its_expiry_parsed():
    ticket = await _source(_answers(_payload())).fetch()

    assert ticket.value == "e2e-opaque-ticket-value"
    assert ticket.is_valid(BEFORE_FIXTURE_EXPIRY)
    assert not ticket.is_valid(FAR_FUTURE)


@pytest.mark.asyncio
async def test_an_empty_list_is_authoritative_rather_than_an_error():
    """Authoritative rather than an error — `NoTicketAvailable` says why."""
    with pytest.raises(NoTicketAvailable) as info:
        await _source(_answers({"host_id": "e2e-host", "tickets": []})).fetch()

    assert info.value.reason == "not-found"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 409, 500, 503], ids=str)
async def test_no_status_is_an_authoritative_empty_answer(status):
    """The empty list is the only "no ticket exists" the contract defines.
    Reading a status as one instead would clear a ticket on a 503 — the issuer
    is down, which is exactly when the held ticket is worth keeping."""
    with pytest.raises(httpx.HTTPStatusError):
        await _source(_answers({}, status=status)).fetch()


@pytest.mark.asyncio
async def test_a_failure_message_carries_no_response_header():
    """`raise_for_status` renders a 3xx `Location` verbatim into the message
    that is then logged. It is the one path where text the issuer chose would
    reach a log record whole."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "https://" + "a" * 4000 + ".invalid"}
        )

    with pytest.raises(httpx.HTTPStatusError) as info:
        await _source(handler).fetch()

    assert "aaaa" not in str(info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", [None, "", "someone-else"], ids=["null", "empty", "other"]
)
async def test_a_ticket_not_naming_this_sandbox_is_refused(value):
    """An entry naming another sandbox is another agent's ticket. One naming
    none is what an issuer that ignored the narrowing returns — the host's
    single row, which may be anybody's — so it is refused for the same reason
    rather than accepted as contradicting nothing."""
    body = _payload()
    if value is None:
        del body["tickets"][0]["sandbox_name"]
    else:
        body["tickets"][0]["sandbox_name"] = value

    with pytest.raises(ValueError, match="refusing another agent's identity"):
        await _source(_answers(body)).fetch()


@pytest.mark.asyncio
async def test_more_than_one_ticket_is_a_contract_breach():
    """`(host_id, sandbox_name)` is unique, so a named fetch answers with at
    most one. Choosing between two would be guessing at an identity."""
    body = _payload()
    body["tickets"].append(dict(body["tickets"][0]))

    with pytest.raises(ValueError, match="narrowing was not honoured"):
        await _source(_answers(body)).fetch()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expires_at", "expected"),
    [
        (12345, "not an ISO 8601 string"),
        (None, "no expires_at"),
        ("soon", "no time of day"),
        ("", "no time of day"),
        ("2026-07-28Tmidday", "not parseable"),
    ],
    ids=["number", "null", "prose", "empty", "unparseable-time"],
)
async def test_an_unreadable_expiry_is_refused_rather_than_ignored(
    expires_at, expected
):
    """Ignoring it would store the ticket with no expiry, which this module
    treats as valid for ever — and nothing in the logs would say so."""
    with pytest.raises(ValueError, match=expected):
        await _source(_answers(_payload(expires_at=expires_at))).fetch()


@pytest.mark.asyncio
async def test_an_expiry_in_the_past_is_a_ticket_rather_than_an_error():
    """An expired ticket is a legitimate answer; the caller turns it into the
    fail-closed path."""
    ticket = await _source(
        _answers(_payload(expires_at="2020-01-01T00:00:00Z"))
    ).fetch()

    assert not ticket.is_valid(BEFORE_FIXTURE_EXPIRY)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ([1, 2, 3], "not a JSON object"),
        ({"tickets": []}, "answers for host"),
        ({"host_id": "another-host", "tickets": []}, "answers for host"),
        ({"host_id": "e2e-host"}, "no tickets list"),
        ({"host_id": "e2e-host", "tickets": "nope"}, "no tickets list"),
        ({"host_id": "e2e-host", "tickets": ["not-a-mapping"]}, "not an object"),
        (
            {
                "host_id": "e2e-host",
                "tickets": [
                    {
                        "sandbox_name": "e2e-sandbox",
                        "expires_at": "2026-07-28T10:15:00Z",
                    }
                ],
            },
            "no token",
        ),
        (
            {
                "host_id": "e2e-host",
                "tickets": [
                    {
                        "token": 7,
                        "sandbox_name": "e2e-sandbox",
                        "expires_at": "2026-07-28T10:15:00Z",
                    }
                ],
            },
            "no token",
        ),
        (
            {
                "host_id": "e2e-host",
                "tickets": [
                    {
                        "token": "",
                        "sandbox_name": "e2e-sandbox",
                        "expires_at": "2026-07-28T10:15:00Z",
                    }
                ],
            },
            "no token",
        ),
    ],
    ids=[
        "not-an-object",
        "no-host-echo",
        "another-host",
        "no-list",
        "list-is-a-string",
        "entry-not-a-mapping",
        "no-token",
        "token-not-a-string",
        "token-is-empty",
    ],
)
async def test_a_response_off_the_contract_is_refused(body, expected):
    with pytest.raises(ValueError, match=expected):
        await _source(_answers(body)).fetch()


@pytest.mark.asyncio
async def test_the_bearer_token_is_sent_and_never_described():
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=_payload())

    source = _source(handler, auth_token="rc_svc_secret")
    await source.fetch()

    assert seen == ["Bearer rc_svc_secret"]
    assert source.describe() == (
        "https://rail-center.invalid/v1/tickets"
        "?host_id=e2e-host&sandbox_name=e2e-sandbox (bearer)"
    )


def test_loopback_and_an_explicit_override_are_both_allowed():
    """Loopback traffic never leaves the machine, and the override is for a
    deployment whose operator has decided otherwise."""
    TicketSource("http://127.0.0.1:8080", host_id="h", sandbox_name="s", auth_token="t")
    TicketSource(
        "http://rail-center.invalid",
        host_id="h",
        sandbox_name="s",
        auth_token="t",
        allow_insecure_credential=True,
    )


@pytest.mark.parametrize(
    ("host_id", "sandbox_name"),
    [("h", ""), ("", "s"), ("", "")],
    ids=["no-sandbox", "no-host", "neither"],
)
def test_both_halves_of_the_identity_are_required(host_id, sandbox_name):
    """An unnamed fetch answers for the whole host, and any entry it returns —
    even a lone one — may be another agent's."""
    with pytest.raises(ValueError, match="both host_id and sandbox_name"):
        TicketSource(
            "https://rail-center.invalid", host_id=host_id, sandbox_name=sandbox_name
        )


@pytest.mark.asyncio
async def test_a_body_past_the_cap_is_abandoned_rather_than_read():
    """The response is the issuer's to size. Reading one of any length into
    memory — and then parsing it on the loop that serves every mount — lets a
    compromised or on-path issuer choose this process's memory use."""
    pulled = 0

    async def endless():
        nonlocal pulled
        yield b'{"host_id": "e2e-host", "tickets": ['
        # Bounded well past the cap rather than genuinely endless: with the cap
        # removed, an unbounded body makes the suite die of memory instead of
        # failing, and a SIGKILL is not a diagnosis.
        while pulled < 4 * MAX_RESPONSE_BYTES // 8192:
            pulled += 1
            yield b" " * 8192

    with pytest.raises(ValueError, match="abandoned unread"):
        await _source(lambda _r: httpx.Response(200, content=endless())).fetch()

    # Bounded by the cap rather than by the body ending: an endless response
    # has no end to buffer to. The literal is the point — a bound recomputed
    # from the constant holds however large the constant grows, and the guard
    # exists to deny the issuer a say in this process's memory.
    assert MAX_RESPONSE_BYTES <= 1024 * 1024
    assert pulled <= 34


@pytest.mark.asyncio
async def test_the_fetch_carries_a_timeout_even_when_none_is_configured():
    """An unreachable Rail Center is awaited before the port is bound, so a
    fetch with no deadline is a container that never comes up and never says
    why."""
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200, json=_payload())

    source = _source(handler)
    assert source.timeout_seconds == 10.0
    await source.fetch()
    assert seen[0]["read"] == 10.0


@pytest.mark.asyncio
async def test_a_configured_maximum_lifetime_is_applied_to_the_ticket():
    """The bound an operator sets is the whole point of the setting; parsing it
    correctly and never consulting it is the same as not having it."""
    far = _payload(expires_at="2027-07-28T10:15:00Z")

    with pytest.raises(ValueError, match="beyond the configured maximum"):
        await _source(_answers(far), max_lifetime_seconds=3600).fetch()

    ticket = await _source(_answers(far), max_lifetime_seconds=10**9).fetch()
    assert ticket.value == "e2e-opaque-ticket-value"


@pytest.mark.asyncio
async def test_the_lifetime_is_measured_from_before_the_request_was_sent():
    """A round trip the ticket already spent in flight is not life it still
    has. Measured after the response, a slow issuer credits it back."""
    now = 1000.0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal now
        now = 3400.0  # the round trip
        return httpx.Response(200, json=_payload(expires_at="1970-01-01T01:00:00Z"))

    def source(**kwargs):
        return TicketSource(
            "https://rail-center.invalid",
            host_id="e2e-host",
            sandbox_name="e2e-sandbox",
            clock=lambda: now,
            transport=httpx.MockTransport(handler),
            **kwargs,
        )

    # Expiry 3600. Measured before sending, the ticket has 2600s of life and is
    # refused by a 2000s bound; measured after, it has 200s and slips under it.
    with pytest.raises(ValueError, match="beyond the configured maximum"):
        await source(max_lifetime_seconds=2000).fetch()

    now = 1000.0
    ticket = await source(max_lifetime_seconds=3000).fetch()
    assert ticket.expires_at == 3600.0


def test_a_naive_expiry_is_read_as_utc_rather_than_local_time(monkeypatch, request):
    """The contract says the field is always UTC. Reading it in the proxy's own
    zone shifts the expiry by the whole offset — hours of treating a dead
    ticket as live, in whichever direction the deployment happens to sit.

    Under a forced non-UTC zone, because CI runs in UTC: there, local time and
    the intended reading agree, and the assertion holds however the stamp was
    interpreted."""
    import time as _time

    # `tzset` reads TZ into the C library, and monkeypatch restores the variable
    # but not the library. Teardown is LIFO, so the finaliser has to be
    # registered *before* the setenv it undoes — registered after, it runs
    # first, re-reads the TZ that is still set, and leaves the process in
    # Kolkata for every test that follows.
    request.addfinalizer(_time.tzset)
    monkeypatch.setenv("TZ", "Asia/Kolkata")  # +05:30, and never on DST
    _time.tzset()

    assert parse_expires_at("2026-07-28T10:15:00") == 1_785_233_700.0
    assert parse_expires_at("2026-07-28T10:15:00Z") == 1_785_233_700.0
    assert parse_expires_at("2026-07-28T11:15:00+01:00") == 1_785_233_700.0


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("localhost", True),
        ("LOCALHOST", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("[::1]", True),
        ("host.docker.internal", False),
        ("rail-center", False),
        ("10.0.0.5", False),
        ("localhost.evil.invalid", False),
        ("", False),
        (None, False),
    ],
    ids=str,
)
def test_only_traffic_that_stays_on_the_machine_counts_as_loopback(host, loopback):
    """Both directions matter. Too narrow and a documented local deployment
    exits 2 on a configuration the docstring calls exempt; too wide and the
    plaintext guard waves through a request that crosses a network."""
    assert is_loopback(host) is loopback


def test_a_credential_in_the_url_is_a_credential():
    """httpx turns userinfo into a real `Authorization: Basic` header, so a base
    URL alone puts a secret on the wire with no RAIL_AUTH_TOKEN set anywhere —
    and the guard reads the token, not the URL."""
    with pytest.raises(ValueError, match="in the clear"):
        TicketSource("http://svc:s3cret@rc.invalid", host_id="h", sandbox_name="s")

    source = TicketSource(
        "https://svc:s3cret@rc.invalid", host_id="h", sandbox_name="s"
    )
    assert source.describe() == (
        "https://rc.invalid/v1/tickets?host_id=h&sandbox_name=s (url-credential)"
    )

    plain = TicketSource("https://rc.invalid", host_id="h", sandbox_name="s")
    assert plain.describe().endswith("(unauthenticated)")


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("rail-center:8000", r"http\(s\) URL"),
        ("//rail-center:8000", r"http\(s\) URL"),
        ("ftp://rail-center", r"http\(s\) URL"),
        ("https://", r"http\(s\) URL"),
        ("https://rc.invalid#frag", "no query or fragment"),
        ("https://rc.invalid?tenant=a", "no query or fragment"),
    ],
    ids=[
        "no-scheme",
        "no-scheme-netloc",
        "wrong-scheme",
        "no-host",
        "fragment",
        "query",
    ],
)
def test_a_base_url_that_cannot_carry_the_route_is_refused(base, expected):
    """A base that is concatenated onto rather than parsed silently changes the
    route. `rail-center:8000` — the packaged example minus its scheme — is a
    configuration error, not a fetch failure to discover at startup."""
    with pytest.raises(ValueError, match=expected):
        TicketSource(base, host_id="h", sandbox_name="s")


def test_a_rejected_base_url_is_reported_without_its_credential():
    """`main` logs this message at ERROR. The raw string is what an operator
    typed, and what they typed may be a password — the redaction filter is a
    second line, not the first, and it cannot see a value with a space in it."""
    with pytest.raises(ValueError) as info:
        TicketSource(
            "https://svc:s3cret@rc.invalid?tenant=a", host_id="h", sandbox_name="s"
        )

    assert "s3cret" not in str(info.value)
    assert "rc.invalid" in str(info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base",
    [
        "https://rc.invalid",
        "https://rc.invalid/",
        "https://rc.invalid#",
        "https://rc.invalid?",
    ],
    ids=["plain", "trailing-slash", "empty-fragment", "empty-query"],
)
async def test_the_route_survives_whatever_punctuation_the_base_ends_in(base):
    """Appended to a raw string, `https://rc.invalid#` puts `/v1/tickets?host_id=…`
    inside a fragment, which is never sent: the request arrives as `GET /` with
    no parameters at all — the unnamed, host-wide fetch this class exists to
    make impossible."""
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=_payload())

    await TicketSource(
        base,
        host_id="e2e-host",
        sandbox_name="e2e-sandbox",
        clock=lambda: BEFORE_FIXTURE_EXPIRY,
        transport=httpx.MockTransport(handler),
    ).fetch()

    assert seen[0].path == "/v1/tickets"
    assert seen[0].params["host_id"] == "e2e-host"
    assert seen[0].params["sandbox_name"] == "e2e-sandbox"


def test_a_fingerprint_is_a_digest_rather_than_a_prefix_of_the_ticket():
    """It stands in for the ticket in logs, so a fingerprint derived from the
    value verbatim would publish the first characters of the secret."""
    value = "rc_ticket_supersecret_value"

    assert token_fingerprint(value) not in value
    assert token_fingerprint(value) == token_fingerprint(value)
    assert token_fingerprint(value) != token_fingerprint(value + "x")


def test_a_token_never_renders_itself():
    """`repr` is what an unguarded log call, a traceback frame and a debugger
    all reach for."""
    token = Token("rc_ticket_supersecret_value", 1_785_233_700.0)

    assert "supersecret" not in repr(token)
    assert token_fingerprint(token.value) in repr(token)
    assert token.remaining(1_785_233_600.0) == 100.0
    assert token.remaining(1_785_233_800.0) == -100.0


def test_a_ticket_stops_being_valid_at_its_expiry_and_not_after():
    """`is_valid` is what `snapshot`, `status`, `next_refresh_delay` and both
    branches of `refresh_once` key on, which makes it the one predicate a
    fail-open would have to pass through — a lapsed ticket injected in place of
    `x-rail-status`. `expires_at` is the moment it stops being valid, not the
    last moment it is, and no grace is added to either side."""
    token = Token("rc_ticket", 1500.0)

    assert token.is_valid(1499.9)
    assert not token.is_valid(1500.0)
    assert not token.is_valid(1500.1)


def test_an_implausible_lifetime_is_visible_without_being_refused(caplog):
    """Warned about, never refused. `IMPLAUSIBLE_TICKET_LIFETIME_SEC` says
    why."""
    with caplog.at_level("WARNING"):
        check_lifetime(31 * 86400, None)  # just over the threshold
        check_lifetime(29 * 86400, None)  # just under it
        check_lifetime(3600, None)

    warnings = [r for r in caplog.records if "non-expiring" in r.getMessage()]
    assert len(warnings) == 1


def test_a_hostile_value_is_sliced_before_it_is_rendered():
    """What this bounds is the cost, not the output: `repr()` of the whole value
    is materialised synchronously on the loop every mount shares, and truncating
    afterwards has already paid for it — so the gate has to observe whether the
    render happened, not how long its result is.
    """
    from fastmcp_proxy import xrail_auth

    class Loud(str):
        rendered_whole = False

        def __repr__(self) -> str:
            type(self).rendered_whole = True
            return str.__repr__(self)

    rendered = xrail_auth._clip(Loud("x" * 200_000))

    # Slicing first hands `repr` a plain str, so `Loud.__repr__` never runs.
    # Rendering first calls it, on all 200k characters.
    assert Loud.rendered_whole is False
    assert len(rendered) < 200
    assert xrail_auth._clip(10**100_000).startswith("<int of ")


def test_a_mapping_is_described_without_every_key_being_rendered():
    """The guard bounds a cost, and an output length cannot see one: rendering
    all 20k keys and slicing afterwards produces the same short string, having
    already paid for it on the loop every mount shares. So the gate is whether
    the keys past the sample were rendered at all."""
    from fastmcp_proxy import xrail_auth

    class Loud(str):
        rendered = 0

        def __repr__(self) -> str:
            type(self).rendered += 1
            return str.__repr__(self)

    described = xrail_auth._describe_keys(
        dict.fromkeys(Loud(f"k{i}") for i in range(20_000))
    )

    assert Loud.rendered <= 5
    assert described.startswith("20000 keys:")
    assert len(described) < 200


@pytest.mark.asyncio
async def test_the_base_url_keeps_its_port_and_its_path_prefix():
    """A base is parsed and rebuilt rather than concatenated onto, and a
    rebuild can drop what it did not copy. `http://rail-center:8000` is the
    packaged compose shape, and a Rail Center behind a path prefix is the
    shape a reverse proxy gives it."""
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=_payload())

    await TicketSource(
        "http://127.0.0.1:8000/rc/",
        host_id="e2e-host",
        sandbox_name="e2e-sandbox",
        clock=lambda: BEFORE_FIXTURE_EXPIRY,
        transport=httpx.MockTransport(handler),
    ).fetch()

    assert seen[0].port == 8000
    assert seen[0].path == "/rc/v1/tickets"


def test_a_plaintext_fetch_is_reported_even_with_no_credential_to_protect():
    """The ticket comes back on the wire whether or not one went out, and it is
    the more valuable of the two — it is what everything downstream trusts. Not
    a refusal, because an unauthenticated plaintext control plane is the shape
    a local stack takes."""
    import logging

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("fastmcp_proxy.xrail")
    handler = _Collect()
    logger.addHandler(handler)
    try:
        TicketSource("http://rc.invalid", host_id="h", sandbox_name="s")
        TicketSource("https://rc.invalid", host_id="h", sandbox_name="s")
        TicketSource("http://127.0.0.1:8000", host_id="h", sandbox_name="s")
    finally:
        logger.removeHandler(handler)

    messages = [r.getMessage() for r in records if "plaintext http" in r.getMessage()]
    assert len(messages) == 1
    assert "rc.invalid" in messages[0]


@pytest.mark.asyncio
async def test_the_whole_exchange_is_bounded_not_each_read_of_it():
    """One deadline over the whole exchange, not one per read. `_request` says
    why the difference matters."""
    import asyncio

    async def dribble():
        while True:
            await asyncio.sleep(0.01)
            yield b" "

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=dribble())

    async def slow_to_answer(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=_payload())

    def source(h):
        return TicketSource(
            "https://rail-center.invalid",
            host_id="e2e-host",
            sandbox_name="e2e-sandbox",
            timeout_seconds=0.05,
            transport=httpx.MockTransport(h),
        )

    # Slow to start, and slow throughout. The second is the one a per-operation
    # timeout cannot see: every individual read is well inside its budget.
    with pytest.raises(asyncio.TimeoutError):
        await source(slow_to_answer).fetch()
    with pytest.raises(asyncio.TimeoutError):
        await source(handler).fetch()


@pytest.mark.asyncio
async def test_the_response_is_never_compressed():
    """httpx decodes a chunk before the cap can look at it, so under gzip a
    small body expands past the cap having already been allocated: the issuer
    would choose this process's memory use through the ratio rather than the
    length."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding"))
        return httpx.Response(200, json=_payload())

    await _source(handler).fetch()

    assert seen == ["identity"]


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("2026-07-28T10:15:00z", 1_785_233_700.0),
        ("2026-07-28t10:15:00Z", 1_785_233_700.0),
    ],
    ids=["lower-z", "lower-t"],
)
def test_rfc_3339_literals_are_case_insensitive(stamp, expected):
    """A conformant issuer emitting lowercase would otherwise get its ticket
    refused as unparseable, and the proxy would hold none."""
    assert parse_expires_at(stamp) == expected


@pytest.mark.parametrize(
    "stamp", ["2026-07-28", "2026-W30-1"], ids=["date-only", "week-date"]
)
def test_an_expiry_with_no_time_of_day_is_refused(stamp):
    """Read as midnight it names an instant up to a whole day earlier than the
    issuer meant, and the schema does not call it valid either."""
    with pytest.raises(ValueError, match="no time of day"):
        parse_expires_at(stamp)


@pytest.mark.asyncio
async def test_an_ambient_proxy_variable_cannot_redirect_the_fetch(monkeypatch):
    """httpx applies HTTP_PROXY to loopback addresses too, so a proxy set in a
    base image or a pod spec would turn the one destination the plaintext guard
    exempts — traffic that never leaves the machine — into a hop across the
    network carrying the credential and the ticket."""
    from fastmcp_proxy import xrail_auth

    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    seen: list[dict] = []
    real = httpx.AsyncClient

    def capture(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(xrail_auth.httpx, "AsyncClient", capture)
    await _source(_answers(_payload())).fetch()

    # The kwarg rather than its consequence: httpx already disables env proxies
    # whenever a transport is injected, so the mounts this would produce cannot
    # be observed through the stub the rest of the suite fetches against.
    assert seen and all(k["trust_env"] is False for k in seen)


def test_a_base_url_that_will_not_parse_at_all_is_reported_without_it():
    """`urlsplit` refuses a netloc that fails NFKC normalisation and puts the
    netloc in the message — the userinfo the sanitised form exists to withhold,
    and in a shape the redaction filter cannot match because it has no scheme."""
    with pytest.raises(ValueError, match="RAIL_CENTER_URL is not a URL") as info:
        TicketSource(
            "https://svc:hunter\uff202@rc.invalid", host_id="h", sandbox_name="s"
        )

    assert "hunter" not in str(info.value)


def test_the_plaintext_warning_names_the_credential_when_one_is_going_out(caplog):
    """The override path reaches the same warning with a bearer token on the
    wire, and a line naming only the ticket understates what is exposed."""
    with caplog.at_level("WARNING"):
        TicketSource(
            "http://rc.invalid",
            host_id="h",
            sandbox_name="s",
            auth_token="rc_svc_x",
            allow_insecure_credential=True,
        )

    messages = [
        r.getMessage() for r in caplog.records if "plaintext http" in r.getMessage()
    ]
    assert len(messages) == 1
    assert "the credential and the ticket it returns" in messages[0]
    assert "rc_svc_x" not in messages[0]


@pytest.mark.asyncio
async def test_a_url_credential_reaches_the_wire_as_well_as_the_label():
    """`safe_url` is what `describe()` renders; `url` is what goes out. Built
    from the sanitised form, the request carries no credential at all while the
    startup line still claims one — and Rail Center answers 401 to a proxy that
    reports itself as authenticated."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=_payload())

    await TicketSource(
        "https://svc:s3cret@rail-center.invalid",
        host_id="e2e-host",
        sandbox_name="e2e-sandbox",
        clock=lambda: BEFORE_FIXTURE_EXPIRY,
        transport=httpx.MockTransport(handler),
    ).fetch()

    assert seen == ["Basic " + base64.b64encode(b"svc:s3cret").decode()]


def test_a_password_with_no_username_is_still_a_credential():
    """`http://:s3cret@host` puts `Basic OnMzY3JldA==` on the wire exactly as a
    named one does, so reading only the username lets it past the guard."""
    with pytest.raises(ValueError, match="in the clear"):
        TicketSource("http://:s3cret@rc.invalid", host_id="h", sandbox_name="s")


def test_a_username_with_no_password_is_still_a_credential():
    """The mirror of the case above, and the one `proxy.build_gateway` calls out
    by name: httpx derives Basic auth from `username or password`, so
    `http://token@host` puts `Basic cmNfdG9rX2FiY2RlZjo=` on a plaintext wire
    exactly as a named pair does. A guard reading only the password lets it
    past, and the source then describes itself as unauthenticated."""
    with pytest.raises(ValueError, match="in the clear"):
        TicketSource("http://rc_tok_abcdef@rc.invalid", host_id="h", sandbox_name="s")


def test_the_refusal_names_the_setting_that_carries_the_credential():
    """An operator told to fix `RAIL_AUTH_TOKEN` when the credential is in the
    URL is told to change a variable they never set."""
    with pytest.raises(ValueError, match="the credential in RAIL_CENTER_URL"):
        TicketSource("http://svc:s3cret@rc.invalid", host_id="h", sandbox_name="s")

    with pytest.raises(ValueError, match="RAIL_AUTH_TOKEN"):
        TicketSource("http://rc.invalid", host_id="h", sandbox_name="s", auth_token="t")


@pytest.mark.parametrize(
    "token",
    ["rc_svc_LIVE-SECRET\nmore", "rc_svc_LIVE-SECRET\x00", "rc_svc_LIVE-SECRÉT"],
    ids=["newline", "nul", "non-ascii"],
)
def test_a_token_that_cannot_be_a_header_is_refused_without_being_echoed(token):
    """`auth_token()` only strips, so an internal newline in `RAIL_AUTH_TOKEN`
    survives configuration and reaches h11 — whose `LocalProtocolError` renders
    the whole value, at every refresh attempt for the life of the process.
    `redact_credentials` cannot match a bare token, so that line is the
    credential in the log."""
    with pytest.raises(ValueError, match="not a valid header value") as info:
        TicketSource(
            "https://rc.invalid", host_id="h", sandbox_name="s", auth_token=token
        )

    assert "LIVE-SECR" not in str(info.value)


@pytest.mark.asyncio
async def test_a_single_enormous_key_is_clipped_like_every_other_value():
    """`_describe_keys` bounds the key count; each key is the issuer's to
    lengthen, and one 200k key fits well inside the response cap."""
    entry = {"sandbox_name": "e2e-sandbox", "x" * 200_000: 1}

    with pytest.raises(ValueError, match="no token") as info:
        await _source(_answers({"host_id": "e2e-host", "tickets": [entry]})).fetch()

    assert len(str(info.value)) < 500


def test_a_container_is_summarised_rather_than_rendered():
    """The branch the `_clip` docstring calls out. A 400k-element list reaches
    it from three issuer-controlled sites, and rendering one costs milliseconds
    on the loop every mount shares."""
    from fastmcp_proxy import xrail_auth

    assert xrail_auth._clip(list(range(400_000))) == "<list of 400000 items>"
    assert xrail_auth._clip({"a": 1, "b": 2}) == "<dict of 2 items>"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encoding", ["gzip", "GZIP", "  gzip  ", "identity, gzip", "deflate", "br"]
)
async def test_a_compressed_response_is_refused_however_it_is_spelled(encoding):
    """httpx decodes `GZIP` and `  gzip  ` exactly as it decodes `gzip`, so a
    guard that compares the header verbatim is a guard an issuer walks past."""

    import gzip
    import zlib

    plain = b'{"host_id": "e2e-host", "tickets": []}'
    body = (
        zlib.compress(plain)
        if "deflate" in encoding
        else gzip.compress(plain)
        if "gzip" in encoding.lower()
        else plain
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-encoding": encoding})

    with pytest.raises(ValueError, match="a ticket fetch reads only identity"):
        await _source(handler).fetch()


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["identity", "IDENTITY", " identity ", ""])
async def test_an_uncompressed_response_is_accepted_however_it_is_spelled(encoding):
    """The other direction of the same normalisation. Compared verbatim, an
    issuer answering `Identity` is refused a ticket it sent correctly."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_payload(), headers={"content-encoding": encoding}
        )

    ticket = await _source(handler).fetch()

    assert ticket.value == "e2e-opaque-ticket-value"


def test_every_constraint_the_parser_enforces_is_in_the_published_schema():
    """The schema is what an issuer builds against. A constraint the parser
    enforces and the schema omits is a response Rail Center can conform to and
    this proxy will then refuse — the two documents disagreeing about the same
    wire, which is the failure the pair exists to prevent.

    The values are asserted as well as the keys: `maxItems: 5` under a present
    `maxItems`, or `minLength: 0` under a present `minLength`, would pass a
    check that only looked for the name."""
    schema = json.loads(SPEC.read_text())
    ticket = schema["$defs"]["ticket"]

    assert sorted(schema["required"]) == ["host_id", "tickets"]
    assert schema["properties"]["tickets"]["type"] == "array"
    assert schema["properties"]["tickets"]["maxItems"] == 1
    assert sorted(ticket["required"]) == ["expires_at", "sandbox_name", "token"]
    assert ticket["properties"]["sandbox_name"] == {
        "type": "string",
        "minLength": 1,
        "description": ticket["properties"]["sandbox_name"]["description"],
    }
    assert ticket["properties"]["token"]["minLength"] == 1
    assert ticket["properties"]["expires_at"]["format"] == "date-time"


@pytest.mark.asyncio
async def test_a_failure_message_is_truncated_before_it_is_logged():
    """The exception carries text the issuer chose. A refresh failure renders
    it into a warning, and a message the length of the response cap is a log
    line nobody can read — and one that every stacked filter on the root
    handlers then walks, which is why the bound is worth more than tidiness."""
    from fastmcp_proxy.xrail_auth import TicketHolder

    class _Loud(Exception):
        def __str__(self) -> str:
            return "x" * 5_000

    class _Source:
        def describe(self):
            return "https://rc.invalid (unauthenticated)"

        async def fetch(self):
            raise _Loud

    import logging

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("fastmcp_proxy")
    handler = _Collect()
    logger.addHandler(handler)
    try:
        await TicketHolder(_Source()).refresh_once()
    finally:
        logger.removeHandler(handler)

    # Lengths rather than the messages: a failure here would otherwise have
    # pytest render the whole oversized string it is complaining about.
    lengths = [len(r.getMessage()) for r in records]
    assert any("ticket refresh failed" in r.getMessage()[:80] for r in records)
    assert max(lengths) < 500


def test_a_forgotten_scheme_does_not_take_its_password_into_the_log():
    """`svc:hunter2@rail-center` — the missing `https://` — has no authority at
    all, so `urlsplit` puts the whole credential in the path. This is the
    likeliest hand-edit there is, and `main` logs the refusal at ERROR before
    exit 2, where the redaction filter cannot help: it matches on `scheme://`
    and this string has none."""
    with pytest.raises(ValueError, match=r"http\(s\) URL") as info:
        TicketSource("svc:hunter2@rail-center.invalid", host_id="h", sandbox_name="s")

    assert "hunter2" not in str(info.value)
    assert "rail-center.invalid" in str(info.value)


@pytest.mark.asyncio
async def test_the_fetch_finds_its_roots_where_the_environment_says(
    monkeypatch, tmp_path
):
    """`trust_env=False` shuts out `HTTP_PROXY`, and the same flag governs where
    httpx looks for CA roots — so shutting it out without building the context
    separately refuses every internal-CA Rail Center, which is the private https
    the plaintext guard exists to push operators towards."""
    import ssl

    import certifi

    from fastmcp_proxy import xrail_auth

    # A bundle holding exactly one root, so the context that read the
    # environment is distinguishable from the one that did not. `SSL_CERT_FILE`
    # replaces the trust store rather than adding to it, which is what makes
    # this observable at all.
    one = tmp_path / "one-root.pem"
    loaded = ssl.create_default_context(cafile=certifi.where())
    one.write_text(ssl.DER_cert_to_PEM_cert(loaded.get_ca_certs(binary_form=True)[0]))
    monkeypatch.setenv("SSL_CERT_FILE", str(one))

    seen: list[dict] = []
    real = httpx.AsyncClient

    def capture(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(xrail_auth.httpx, "AsyncClient", capture)
    await _source(_answers(_payload())).fetch()

    assert seen and isinstance(seen[0]["verify"], ssl.SSLContext)
    assert len(seen[0]["verify"].get_ca_certs()) == 1


def test_fractional_seconds_are_read_at_whatever_precision_they_arrive_in():
    """RFC 3339 puts no limit on the digits; `fromisoformat` on the 3.10 floor
    reads three or six and rejects the rest, which would leave a conformant
    issuer's ticket unparseable on one leg of the matrix and fine on the other.

    Its teeth are on that leg: 3.12 parses any precision, so this passes there
    with the normalisation deleted."""
    assert parse_expires_at("2026-07-28T10:15:00.5Z") == 1_785_233_700.5
    assert parse_expires_at("2026-07-28T10:15:00.500000000Z") == 1_785_233_700.5
    assert parse_expires_at("2026-07-28T10:15:00.1234Z") == pytest.approx(
        1_785_233_700.1234
    )


@pytest.mark.asyncio
async def test_an_unauthenticated_fetch_sends_no_authorization_header():
    """`RAIL_AUTH_MODE=none` is the default and the open-source deployment
    shape. An issuer that reads a present-but-empty `Authorization` as a failed
    attempt answers 401, and the proxy holds no ticket while still logging
    itself as unauthenticated."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=_payload())

    source = _source(handler)
    await source.fetch()

    assert seen == [None]
    assert source.describe().endswith("(unauthenticated)")


def test_a_fingerprint_is_long_enough_to_tell_two_tickets_apart():
    """Its whole job is showing an operator *that* a ticket rotated. Cut short
    enough, unrelated tickets collide and a rotation looks like a repeat — at
    one hex character, one pair in sixteen. A floor rather than a constant: a
    longer digest is only better at this."""
    assert len(token_fingerprint("anything")) >= 12


# ─────────────────────────────────────────────────────────────────────
#  Holding a ticket between requests
# ─────────────────────────────────────────────────────────────────────


class _Answers:
    """A source that returns, or raises, whatever the test queued."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def describe(self):
        return "https://rc.invalid/v1/tickets (unauthenticated)"

    async def fetch(self):
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else self.last
        self.last = answer
        if isinstance(answer, Exception):
            raise answer
        return answer


def _ticket(value="rc_ticket", expires_at=2000.0):
    return Token(value, expires_at)


@pytest.mark.asyncio
async def test_a_held_ticket_is_what_the_injector_reads():
    holder = TicketHolder(_Answers(_ticket()), clock=lambda: 1000.0)
    await holder.refresh_once()

    assert holder.current == "rc_ticket"
    assert holder.unavailable_reason is None


@pytest.mark.asyncio
async def test_a_transient_failure_keeps_a_ticket_that_is_still_valid():
    """An issuer being briefly unreachable says nothing about whether this
    proxy's identity is still good. Clearing on the first failed poll would
    make a blip on Rail Center an outage for every agent behind a proxy."""
    source = _Answers(_ticket(), httpx.ConnectError("no route"))
    holder = TicketHolder(source, clock=lambda: 1000.0)

    await holder.refresh_once()
    assert await holder.refresh_once() is False

    assert holder.current == "rc_ticket"
    assert holder.unavailable_reason is None


@pytest.mark.asyncio
async def test_an_authoritative_empty_answer_clears_a_ticket_still_valid():
    """The opposite of a failure — see `NoTicketAvailable`."""
    source = _Answers(_ticket(), NoTicketAvailable("not-found", "none for that host"))
    holder = TicketHolder(source, clock=lambda: 1000.0)

    await holder.refresh_once()
    await holder.refresh_once()

    assert holder.current is None
    assert holder.unavailable_reason == "not-found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answers", "now", "expected"),
    [
        ((httpx.ConnectError("x"),), 1000.0, "issuer-unreachable"),
        ((_ticket(expires_at=1500.0),), 9000.0, "expired"),
        ((NoTicketAvailable("not-found", ""),), 1000.0, "not-found"),
    ],
    ids=["never-answered", "lapsed", "authoritatively-none"],
)
async def test_the_three_reasons_are_told_apart(answers, now, expected):
    """`x-rail-status` carries this verbatim, and the three call for different
    responses: a wrong sandbox name, a dead refresh loop, and an issuer that is
    down are not the same incident."""
    holder = TicketHolder(_Answers(*answers), clock=lambda: now)
    await holder.refresh_once()

    assert holder.current is None
    assert holder.unavailable_reason == expected


@pytest.mark.asyncio
async def test_a_recovered_ticket_clears_the_reason():
    """A holder that answered `not-found` once must stop saying so the moment
    Rail Center issues one, or the status header outlives the condition."""
    source = _Answers(NoTicketAvailable("not-found", ""), _ticket())
    holder = TicketHolder(source, clock=lambda: 1000.0)

    await holder.refresh_once()
    await holder.refresh_once()

    assert holder.current == "rc_ticket"
    assert holder.unavailable_reason is None


def test_refresh_is_paced_by_the_tickets_own_life_not_the_configured_interval():
    """A ticket shorter-lived than the interval must be replaced before it
    lapses. Left to the configured hour, a five-minute ticket spends most of
    its time expired and every call fails closed."""
    holder = TicketHolder(_Answers(), refresh_seconds=3600.0, clock=lambda: 1000.0)
    holder._ticket = _ticket(expires_at=1000.0 + 600)

    assert holder.next_refresh_delay() == 300.0  # half of what is left


def test_a_very_short_ticket_does_not_become_a_poll_loop():
    """The floor is a safety property, not a preference: without it a
    one-second ticket has every proxy in a fleet asking Rail Center twice a
    second."""
    holder = TicketHolder(_Answers(), clock=lambda: 1000.0)
    holder._ticket = _ticket(expires_at=1000.5)

    assert holder.next_refresh_delay() == TicketHolder.MIN_REFRESH_INTERVAL_SEC


def test_the_floor_is_five_seconds_and_not_merely_a_floor():
    """Every other assertion about the floor is written as a multiple of this
    constant, so all of them move with it and a value lowered to 0.1 leaves
    them green. That is the regression the class comment names: every proxy in
    a fleet hammering Rail Center, which nothing local would notice.
    `REFRESH_RATIO` is pinned absolutely below for the same reason."""
    assert TicketHolder.MIN_REFRESH_INTERVAL_SEC == 5.0


def test_a_long_ticket_is_still_refreshed_at_the_configured_interval():
    """The configured value is an upper bound. A 30-day ticket left to half its
    life would go a fortnight without the proxy noticing it was revoked."""
    holder = TicketHolder(_Answers(), refresh_seconds=3600.0, clock=lambda: 1000.0)
    holder._ticket = _ticket(expires_at=1000.0 + 30 * 86400)

    assert holder.next_refresh_delay() == 3600.0


@pytest.mark.asyncio
async def test_repeated_failure_backs_off_rather_than_hammering_the_issuer():
    """Failing closed is a reason to retry promptly, not forever at the floor:
    a Rail Center that is down stays down while every proxy behind it asks."""
    holder = TicketHolder(
        _Answers(httpx.ConnectError("x")), refresh_seconds=3600.0, clock=lambda: 1000.0
    )

    delays = []
    for _ in range(5):
        await holder.refresh_once()
        delays.append(holder.next_refresh_delay())

    assert delays == sorted(delays)
    assert delays[0] == TicketHolder.MIN_REFRESH_INTERVAL_SEC * 2
    assert delays[-1] > delays[0]


@pytest.mark.asyncio
async def test_the_backoff_restarts_when_a_ticket_lapses_rather_than_carrying_over():
    """Failures counted while a ticket was still valid do not enter the ramp at
    all, so losing that ticket starts from the floor."""
    now = 1000.0
    source = _Answers(_ticket(expires_at=1500.0), *[httpx.ConnectError("x")] * 6)
    holder = TicketHolder(source, refresh_seconds=3600.0, clock=lambda: now)

    await holder.refresh_once()
    for _ in range(5):  # fail repeatedly while the ticket is still valid
        await holder.refresh_once()
    assert holder.next_refresh_delay() > 0

    now = 9000.0  # the ticket lapses
    await holder.refresh_once()

    assert holder.unavailable_reason == "expired"
    assert holder.next_refresh_delay() == TicketHolder.MIN_REFRESH_INTERVAL_SEC * 2


@pytest.mark.asyncio
async def test_an_already_expired_ticket_is_reported_as_such_not_as_acquired(caplog):
    """A ticket past its expiry is a well-formed answer, so it arrives on the
    success path. Announcing it as acquired reports a dead identity as a
    healthy one while injection is already failing closed."""
    holder = TicketHolder(_Answers(_ticket(expires_at=1500.0)), clock=lambda: 9000.0)

    with caplog.at_level("INFO"):
        assert await holder.refresh_once() is True

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "already-expired" in logged
    assert "acquired" not in logged
    assert holder.current is None


@pytest.mark.asyncio
async def test_the_status_reports_the_state_and_never_the_ticket():
    """`/health` serves this, on an endpoint anything on the network can reach."""
    holder = TicketHolder(_Answers(_ticket("rc_secret_ticket")), clock=lambda: 1000.0)
    await holder.refresh_once()

    status = holder.status

    assert status["ticket_held"] is True
    assert status["ticket_valid"] is True
    assert status["expires_in_sec"] == 1000
    assert status["fingerprint"] == token_fingerprint("rc_secret_ticket")
    assert "rc_secret_ticket" not in repr(status)


@pytest.mark.asyncio
async def test_the_refresh_loop_stops_when_the_holder_is_closed():
    """`asyncio.run` cancels a surviving task during interpreter shutdown, which
    surfaces as a traceback on an otherwise clean SIGTERM."""
    holder = TicketHolder(_Answers(_ticket()), clock=lambda: 1000.0)

    import asyncio

    await holder.start()
    assert holder._task is not None
    # Bounded: a close that does not cancel waits on a loop that never ends, and
    # an unbounded assertion would hang the CI job rather than fail it.
    await asyncio.wait_for(holder.aclose(), 5)

    assert holder._task is None
    await asyncio.wait_for(holder.aclose(), 5)  # idempotent


def test_closing_a_holder_that_never_started_is_not_an_error():
    """`main` closes in a `finally`, which runs whether or not `start` did."""
    import asyncio as _asyncio

    _asyncio.run(TicketHolder(_Answers()).aclose())


# ─────────────────────────────────────────────────────────────────────
#  Putting it on the request
# ─────────────────────────────────────────────────────────────────────


def _injected(ticket=None, reason=None) -> httpx.Request:
    """One request through the injector, against a real holder.

    A stub with the two answers set independently can hold a pair the state
    machine cannot produce, and a test written against one proves less than it
    looks like it does.
    """
    from tests.conftest import wound_holder

    request = httpx.Request("POST", "https://gateway.invalid/mcp")
    holder = wound_holder(ticket=ticket) if ticket else wound_holder(reason=reason)
    next(XRailInjector(holder).auth_flow(request))
    return request


def test_the_reason_never_goes_in_the_ticket_header():
    """That would turn absence into presence and make this component an author
    of ticket content — a gateway reading `x-rail: expired` has been handed
    something that looks like an identity."""
    request = _injected(reason="expired")

    assert "x-rail" not in request.headers
    assert request.headers["x-rail-status"] == "expired"


def test_a_ticket_and_a_status_are_never_both_present():
    """A gateway seeing both has no way to decide which to believe."""
    request = _injected(ticket="rc_ticket")

    assert request.headers["x-rail"] == "rc_ticket"
    assert "x-rail-status" not in request.headers


def test_the_header_names_are_the_protocol_ones():
    """The gateway looks for exactly these. A rename fails silently: injection
    logs success, the gateway sees no `x-rail`, every call is refused, and
    nothing names the cause."""
    assert XRailInjector.HEADER == "x-rail"
    assert XRailInjector.STATUS_HEADER == "x-rail-status"


@pytest.mark.asyncio
async def test_the_refresh_loop_keeps_fetching_after_the_first():
    """The point of holding a ticket is that it is replaced before it lapses.
    A loop that starts and never fetches again satisfies every other assertion
    here, and the proxy fails closed for good the moment the first one expires."""
    import asyncio

    source = _Answers(_ticket("first"), _ticket("second"), _ticket("third"))
    holder = TicketHolder(source, refresh_seconds=0.001, clock=lambda: 1000.0)
    # Both under the floor on purpose, so the loop turns over inside the test
    # rather than inside a sleep nobody waits out.
    holder.MIN_REFRESH_INTERVAL_SEC = 0.001

    await holder.start()
    for _ in range(200):
        await asyncio.sleep(0.005)
        if source.calls >= 3:
            break
    await asyncio.wait_for(holder.aclose(), 5)

    assert source.calls >= 3, f"the loop fetched {source.calls} times"
    assert holder.current == "third"


@pytest.mark.asyncio
async def test_the_loop_survives_a_defect_in_its_own_body(caplog):
    """A defect in the loop body must not end refreshing. `_refresh_loop` says
    why."""
    import asyncio

    holder = TicketHolder(
        _Answers(_ticket()), refresh_seconds=0.001, clock=lambda: 1000.0
    )
    holder.MIN_REFRESH_INTERVAL_SEC = 0.001
    calls = {"n": 0}

    async def sometimes_broken():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("a defect, not a condition at the issuer")
        return True

    caplog.set_level("ERROR")
    await holder.start()
    holder.refresh_once = sometimes_broken
    for _ in range(200):
        await asyncio.sleep(0.005)
        if calls["n"] >= 3:
            break
    await asyncio.wait_for(holder.aclose(), 5)

    assert calls["n"] >= 3, "the loop stopped at the first exception"
    assert any("retrying" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_closing_does_not_re_raise_what_the_loop_died_of():
    """`aclose` says why it never raises; this is the case that would."""
    import asyncio

    holder = TicketHolder(_Answers(_ticket()), clock=lambda: 1000.0)

    async def dies():
        raise RuntimeError("already dead")

    holder._task = asyncio.get_running_loop().create_task(dies())
    await asyncio.sleep(0)  # let it die

    await asyncio.wait_for(holder.aclose(), 5)  # must not raise
    assert holder._task is None


@pytest.mark.asyncio
async def test_an_authoritative_no_ticket_backs_off_like_a_failure():
    """Re-asking faster will not change the answer. Left at the floor, an agent
    with no registered ticket has every proxy in the fleet asking Rail Center
    twelve times a minute, for ever."""
    holder = TicketHolder(
        _Answers(NoTicketAvailable("not-found", "")),
        refresh_seconds=3600.0,
        clock=lambda: 1000.0,
    )

    delays = []
    for _ in range(4):
        await holder.refresh_once()
        delays.append(holder.next_refresh_delay())

    assert delays[0] == TicketHolder.MIN_REFRESH_INTERVAL_SEC * 2
    assert delays == sorted(delays)
    assert delays[-1] > delays[0]


def test_the_backoff_is_floored_as_well_as_capped():
    """A configured interval under the floor must not become a poll loop.
    `next_refresh_delay` says why the floor is not negotiable."""
    holder = TicketHolder(_Answers(), refresh_seconds=0.5, clock=lambda: 1000.0)

    assert holder._ticket is None
    assert holder.next_refresh_delay() == TicketHolder.MIN_REFRESH_INTERVAL_SEC
    holder._failures = 3
    assert holder.next_refresh_delay() >= TicketHolder.MIN_REFRESH_INTERVAL_SEC


def test_the_backoff_never_exceeds_the_configured_interval():
    """The configured value is the upper bound it says it is. Unbounded, a down
    issuer extends the identity outage past its own recovery."""
    holder = TicketHolder(_Answers(), refresh_seconds=30.0, clock=lambda: 1000.0)
    holder._failures = 20

    assert holder.next_refresh_delay() == 30.0


@pytest.mark.asyncio
async def test_the_status_reports_every_state_it_claims_to():
    """`/health` serves this, and it is the one diagnostic an operator reaches
    for when the gateway starts denying. A field that is always None is worse
    than an absent one: it reads as an answer."""
    holder = TicketHolder(
        _Answers(_ticket()), refresh_seconds=3600.0, clock=lambda: 1000.0
    )

    cold = holder.status
    assert cold["ticket_held"] is False
    assert cold["ticket_valid"] is False
    assert cold["unavailable_reason"] == "issuer-unreachable"
    assert cold["last_refresh_age_sec"] is None
    assert cold["next_refresh_in_sec"] == TicketHolder.MIN_REFRESH_INTERVAL_SEC

    await holder.refresh_once()
    warm = holder.status
    assert warm["ticket_held"] is True
    assert warm["unavailable_reason"] is None
    assert warm["last_refresh_age_sec"] == 0
    assert warm["next_refresh_in_sec"] == 500  # half of what is left


@pytest.mark.asyncio
async def test_an_expired_ticket_is_not_reported_as_valid():
    """`ticket_held` and `ticket_valid` are different questions. Reporting a
    lapsed ticket as valid tells an operator the identity is fine while every
    call fails closed."""
    holder = TicketHolder(_Answers(_ticket(expires_at=1500.0)), clock=lambda: 9000.0)
    await holder.refresh_once()

    status = holder.status
    assert status["ticket_held"] is True
    assert status["ticket_valid"] is False
    assert status["unavailable_reason"] == "expired"


@pytest.mark.asyncio
async def test_an_expired_ticket_does_not_stamp_the_last_good_refresh():
    """`last_refresh_age_sec` is the age of the last *good* refresh, and a fetch
    returning an already-lapsed ticket is not one. Stamped anyway, `/health`
    reports an identity zero seconds old in the state an operator most needs to
    catch — injection failing closed."""
    holder = TicketHolder(_Answers(_ticket(expires_at=1500.0)), clock=lambda: 9000.0)

    assert await holder.refresh_once() is True

    assert holder.status["ticket_valid"] is False
    assert holder.status["last_refresh_age_sec"] is None


def test_the_status_never_names_the_rail_center():
    """`/health` is on the listener the sandbox reaches for `/mcp`; `status`
    says what that rules out."""
    holder = TicketHolder(_Answers(), clock=lambda: 1000.0)

    assert "source" not in holder.status
    assert "rc.invalid" not in repr(holder.status)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token",
    [
        "rc\nticket\ninjected",
        "rc\rinjected",
        "rc\x00injected",
        "\ud800injected",
        " injected",
        "injected ",
        "rc\x7finjected",
    ],
    ids=["lf", "cr", "nul", "surrogate", "leading-space", "trailing-space", "del"],
)
async def test_a_malformed_token_is_refused_at_the_fetch_without_being_echoed(token):
    """The regex is asserted above; this is the rejection. Stored instead, a
    surrogate takes `token_fingerprint` down with it — and the whole value
    reaches a log through h11's own error."""
    # `ensure_ascii=True` and raw content: httpx serialises `json=` without
    # escaping, and a lone surrogate cannot be encoded as UTF-8 — the wire form
    # an issuer would actually send is the escaped one.
    body = json.dumps(_payload(token=token), ensure_ascii=True).encode()

    with pytest.raises(ValueError, match="not a valid header value") as info:
        await _source(lambda _r: httpx.Response(200, content=body)).fetch()

    assert "injected" not in str(info.value)


@pytest.mark.asyncio
async def test_a_snapshot_never_carries_a_ticket_and_a_reason_together():
    """Exactly one of the two, always. `snapshot` says why asking twice is not
    the same question."""
    holder = TicketHolder(_Answers(_ticket()), clock=lambda: 1000.0)

    for _ in range(2):
        ticket, reason = holder.snapshot()
        assert (ticket is None) != (reason is None)
        await holder.refresh_once()

    ticket, reason = holder.snapshot()
    assert (ticket is None) != (reason is None)


def test_the_no_ticket_warning_is_not_once_per_request(caplog):
    """Once per outage, not once per request. `XRailInjector._warned` says why
    the sandbox must not be able to choose this process's log volume."""
    from tests.conftest import wound_holder

    injector = XRailInjector(wound_holder(reason="not-found"))

    with caplog.at_level("WARNING"):
        for _ in range(20):
            next(injector.auth_flow(httpx.Request("POST", "https://gw.invalid/mcp")))

    warnings = [r for r in caplog.records if "no valid ticket" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_a_success_restarts_the_backoff_ramp():
    """Failures counted *before* a ticket landed are discarded by the success,
    so losing that ticket also starts from the floor."""
    source = _Answers(
        *[httpx.ConnectError("x")] * 6,
        _ticket(expires_at=2000.0),
        httpx.ConnectError("x"),
    )
    now = 1000.0
    holder = TicketHolder(source, refresh_seconds=3600.0, clock=lambda: now)

    for _ in range(6):
        await holder.refresh_once()
    at_the_ceiling = holder.next_refresh_delay()

    await holder.refresh_once()  # a ticket lands
    now = 9000.0  # it lapses, so the identity is lost again
    await holder.refresh_once()  # and the issuer is still down

    assert at_the_ceiling > TicketHolder.MIN_REFRESH_INTERVAL_SEC * 8
    assert holder.unavailable_reason == "expired"
    assert holder.next_refresh_delay() == TicketHolder.MIN_REFRESH_INTERVAL_SEC * 2


@pytest.mark.asyncio
async def test_a_stale_not_found_does_not_outlive_its_condition():
    """`not-found` is Rail Center saying this agent has no ticket. Left set, a
    later expiry is reported as one — conflating a dead refresh loop with an
    identity that was never issued."""
    source = _Answers(NoTicketAvailable("not-found", ""), _ticket(expires_at=2000.0))
    now = 1000.0
    holder = TicketHolder(source, clock=lambda: now)

    await holder.refresh_once()
    assert holder.unavailable_reason == "not-found"
    await holder.refresh_once()
    now = 9000.0  # the recovered ticket lapses

    assert holder.unavailable_reason == "expired"


@pytest.mark.asyncio
async def test_an_issuer_that_stops_answering_stops_reporting_not_found():
    """The two call for different responses — register the agent, or fix the
    issuer. `refresh_once`'s generic-failure branch says why the older answer
    stops being reported."""
    source = _Answers(NoTicketAvailable("not-found", ""), httpx.ConnectError("x"))
    holder = TicketHolder(source, clock=lambda: 1000.0)

    await holder.refresh_once()
    await holder.refresh_once()

    assert holder.unavailable_reason == "issuer-unreachable"


def test_the_backoff_ramp_is_capped_before_it_reaches_the_interval():
    """Uncapped, ten consecutive failures put the next retry a full hour out at
    the default interval — extending the identity outage past Rail Center's own
    recovery."""
    holder = TicketHolder(_Answers(), refresh_seconds=3600.0, clock=lambda: 1000.0)

    holder._failures = 6
    ceiling = holder.next_refresh_delay()
    for failures in (7, 10, 40):
        holder._failures = failures
        assert holder.next_refresh_delay() == ceiling
    assert ceiling == TicketHolder.MIN_REFRESH_INTERVAL_SEC * 64


def test_a_second_outage_is_warned_about_like_the_first(caplog):
    """Suppression is within one outage. Carried across a recovery, every
    outage after the first is DEBUG only."""
    from tests.conftest import wound_holder

    injector = XRailInjector(wound_holder(reason="not-found"))

    def once(holder):
        injector.holder = holder
        next(injector.auth_flow(httpx.Request("POST", "https://gw.invalid/mcp")))

    with caplog.at_level("WARNING"):
        once(wound_holder(reason="not-found"))
        once(wound_holder(ticket="recovered"))
        once(wound_holder(reason="not-found"))

    warnings = [r for r in caplog.records if "no valid ticket" in r.getMessage()]
    assert len(warnings) == 2


def test_a_cold_status_reports_no_fingerprint_and_no_expiry():
    """A monitor reading a fingerprint concludes an identity is held while
    every call fails closed."""
    holder = TicketHolder(_Answers(), clock=lambda: 1000.0)

    assert holder.status["fingerprint"] is None
    assert holder.status["expires_in_sec"] is None


@pytest.mark.asyncio
async def test_closing_waits_for_the_loop_it_cancelled():
    """Cancelled and not awaited, the task is still pending when `asyncio.run`
    tears the loop down, and that is the traceback on a clean SIGTERM."""
    import asyncio

    holder = TicketHolder(_Answers(_ticket()), clock=lambda: 1000.0)
    await holder.start()
    task = holder._task

    await asyncio.wait_for(holder.aclose(), 5)

    assert task is not None and task.done()


@pytest.mark.asyncio
async def test_a_cancel_aimed_at_the_caller_is_not_swallowed():
    """Swallowed, a cancelled shutdown carries on as though it were clean."""
    import asyncio
    import contextlib
    import sys

    if sys.version_info < (3, 11):  # pragma: no cover - the floor cannot tell
        pytest.skip("Task.cancelling() is 3.11+; the floor assumes the loop's own")

    holder = TicketHolder(_Answers(_ticket()), clock=lambda: 1000.0)
    await holder.start()
    outcome = []

    async def closer():
        try:
            await holder.aclose()
        except asyncio.CancelledError:
            outcome.append("propagated")
            raise
        outcome.append("swallowed")

    task = asyncio.get_running_loop().create_task(closer())
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert outcome == ["propagated"]


@pytest.mark.asyncio
async def test_a_rotation_is_announced_with_the_new_fingerprint(caplog):
    """The ticket is never logged, but an operator still needs to see *that* it
    changed — a silent rotation and a stuck one look identical from outside."""
    source = _Answers(_ticket("first"), _ticket("second"), _ticket("second"))
    holder = TicketHolder(source, clock=lambda: 1000.0)

    with caplog.at_level("INFO"):
        await holder.refresh_once()
        await holder.refresh_once()
        await holder.refresh_once()

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "ticket acquired" in logged
    assert "ticket rotated" in logged
    assert token_fingerprint("second") in logged
    assert "second" not in logged
    # The third fetch returned the same value, so it is not a rotation.
    assert logged.count("ticket rotated") == 1


@pytest.mark.asyncio
async def test_an_issuer_handing_out_expired_tickets_still_backs_off(caplog):
    """A succeeding fetch that yields nothing usable is not a reason to stop
    backing off — `_failures` says what the ramp measures."""
    source = _Answers(_ticket(expires_at=1500.0))
    holder = TicketHolder(source, refresh_seconds=3600.0, clock=lambda: 9000.0)

    delays = []
    for _ in range(4):
        assert await holder.refresh_once() is True
        delays.append(holder.next_refresh_delay())

    assert holder.current is None
    assert delays == sorted(delays)
    assert delays[-1] > delays[0]


@pytest.mark.asyncio
async def test_a_failure_after_a_success_does_not_claim_nothing_was_ever_fetched():
    """The line an operator reads to decide whether the agent was ever
    registered. "No ticket has ever been fetched" sends them to check the
    sandbox name; "nothing is held" sends them to the issuer. Both halves,
    because a message that is always one of them tells nobody anything."""
    import logging

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("fastmcp_proxy.xrail")
    handler = _Collect()
    logger.addHandler(handler)
    try:
        # Cold: nothing has ever landed.
        cold = TicketHolder(_Answers(httpx.ConnectError("x")), clock=lambda: 1000.0)
        await cold.refresh_once()
        first = "\n".join(r.getMessage() for r in records)
        records.clear()

        # Warm, then cleared by an authoritative empty answer, then a failure.
        warm = TicketHolder(
            _Answers(
                _ticket(expires_at=2000.0),
                NoTicketAvailable("not-found", ""),
                httpx.ConnectError("x"),
            ),
            clock=lambda: 1000.0,
        )
        for _ in range(3):
            await warm.refresh_once()
        second = "\n".join(r.getMessage() for r in records)
    finally:
        logger.removeHandler(handler)

    assert "no ticket has ever been fetched" in first
    assert "nothing is held" in second
    assert "never been fetched" not in second


@pytest.mark.asyncio
async def test_an_authoritative_no_ticket_names_the_pair_it_asked_about():
    """`not-found` means one of the two keys is wrong, and which one is the
    whole of what an operator can act on."""
    with pytest.raises(NoTicketAvailable) as info:
        await _source(_answers({"host_id": "e2e-host", "tickets": []})).fetch()

    assert "e2e-host" in str(info.value)
    assert "e2e-sandbox" in str(info.value)


@pytest.mark.asyncio
async def test_the_loop_sleeps_before_it_fetches():
    """`start` has already fetched once. Fetching again immediately makes every
    proxy in a fleet issue two back-to-back requests at boot, the second one
    bypassing the floor."""
    import asyncio

    source = _Answers(_ticket(), _ticket(), _ticket())
    holder = TicketHolder(source, refresh_seconds=3600.0, clock=lambda: 1000.0)

    await holder.start()
    assert source.calls == 1
    await asyncio.sleep(0.02)
    assert source.calls == 1, "the loop fetched without waiting"
    await asyncio.wait_for(holder.aclose(), 5)


def test_two_credentials_are_refused_before_the_plaintext_guard_speaks():
    """Told to fix the plaintext first, an operator sets the override, retries,
    and only then meets the real fault — a bearer that never leaves the process
    while every log line says it did."""
    with pytest.raises(ValueError, match="Configure one"):
        TicketSource(
            "http://svc:s3cret@rc.invalid",
            host_id="h",
            sandbox_name="s",
            auth_token="rc_svc_x",
        )


@pytest.mark.asyncio
async def test_an_expired_answer_does_not_discard_a_ticket_that_is_still_good(caplog):
    """A failed fetch keeps a valid ticket, so a *successful* one returning an
    already-lapsed ticket must not be what discards it — that fails closed for
    whatever life the held one had left."""
    now = 2000.0
    source = _Answers(_ticket("good", expires_at=5000.0), _ticket("stale", 1500.0))
    holder = TicketHolder(source, clock=lambda: now)

    await holder.refresh_once()
    now = 2600.0
    with caplog.at_level("WARNING"):
        assert await holder.refresh_once() is True

    assert holder.current == "good"
    assert holder.unavailable_reason is None
    assert "keeping the one held" in caplog.text
    # Neither counter moves: an identity is held, and nothing good came back.
    # A ramp that climbed here would put the first retry after the good ticket
    # finally lapses at the ceiling, and `/health` would report the age of a
    # refresh that produced nothing usable as though it had.
    assert holder._failures == 0
    assert holder.status["last_refresh_age_sec"] == 600


@pytest.mark.asyncio
async def test_a_transient_failure_leaves_the_ramp_alone_while_a_ticket_holds(caplog):
    """The other half of the same rule, and the line that tells the two apart."""
    source = _Answers(_ticket("good", expires_at=5000.0), httpx.ConnectError("x"))
    holder = TicketHolder(source, clock=lambda: 2000.0)

    await holder.refresh_once()
    with caplog.at_level("WARNING"):
        await holder.refresh_once()

    assert holder.current == "good"
    assert holder._failures == 0
    assert "keeping the ticket held until it expires" in caplog.text


@pytest.mark.asyncio
async def test_clearing_a_held_ticket_is_said_out_loud(caplog):
    """An authoritative empty answer discards an identity that was working. An
    operator reading "none was held" would not know one had just gone."""
    source = _Answers(_ticket(expires_at=5000.0), NoTicketAvailable("not-found", ""))
    holder = TicketHolder(source, clock=lambda: 2000.0)

    await holder.refresh_once()
    with caplog.at_level("WARNING"):
        await holder.refresh_once()

    assert "cleared the one held" in caplog.text


def test_the_plaintext_warning_renders_the_url_without_its_credential(caplog):
    """The one rendering site of `url` rather than `safe_url` that is reached
    with a credential in the URL — the override path, where the operator has
    already said to send it anyway."""
    with caplog.at_level("WARNING"):
        TicketSource(
            "http://svc:s3cret@rc.invalid",
            host_id="h",
            sandbox_name="s",
            allow_insecure_credential=True,
        )

    assert "plaintext http" in caplog.text
    assert "s3cret" not in caplog.text
    assert "rc.invalid" in caplog.text


def test_the_injector_reads_the_holder_once_not_twice():
    """Wall time is not monotonic, and this module uses it because an expiry is
    an absolute instant. Asked twice across a clock that steps backwards, the
    two questions disagree and the header value is None."""
    ticks = iter([2000.0, 2000.0, 1000.0, 1000.0, 1000.0])
    holder = TicketHolder(_Answers(), clock=lambda: next(ticks))
    holder._ticket = Token("t", 1500.0)  # lapsed at 2000, live again at 1000

    request = httpx.Request("POST", "https://gw.invalid/mcp")
    next(XRailInjector(holder).auth_flow(request))

    written = [h for h in ("x-rail", "x-rail-status") if h in request.headers]
    assert len(written) == 1
    assert request.headers[written[0]] is not None


def test_an_upstream_password_with_no_username_is_a_credential_too():
    """`urlsplit("https://:s3cret@h/").username` is the empty string, so a guard
    reading only the username lets the one-part form past."""
    from urllib.parse import urlsplit as _split

    from fastmcp_proxy import proxy as proxy_module

    parts = _split("https://:s3cret@gateway.invalid/mcp")
    assert not parts.username
    assert parts.password
    assert bool(parts.username or parts.password)
    assert proxy_module._in_the_clear("http://gateway.invalid/mcp") is True
