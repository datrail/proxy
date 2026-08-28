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
    TicketSource,
    Token,
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
    """The list is the complete answer for that host and sandbox. Treating an
    empty one as a failure would let a caller keep a ticket Rail Center has
    stopped issuing, which is the case the host-scoped route exists to prevent."""
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


def test_two_credentials_are_a_configuration_error_rather_than_a_silent_choice():
    """httpx builds Basic auth from userinfo and it overwrites the bearer header
    this class sets, so the token would never leave the process while the
    startup line said it had."""
    with pytest.raises(ValueError, match="Configure one"):
        TicketSource(
            "https://svc:s3cret@rc.invalid",
            host_id="h",
            sandbox_name="s",
            auth_token="rc_svc_x",
        )


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
    """`httpx.Timeout` is per-operation and its read budget re-arms on every
    chunk, so an issuer dribbling a byte at a time is bounded only by the size
    cap — and the listener is not bound until this returns."""
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


def test_the_refusal_names_the_setting_that_carries_the_credential():
    """An operator told to fix `RAIL_AUTH_TOKEN` when the credential is in the
    URL is told to change a variable they never set."""
    with pytest.raises(ValueError, match="the credential in RAIL_CENTER_URL"):
        TicketSource("http://svc:s3cret@rc.invalid", host_id="h", sandbox_name="s")

    with pytest.raises(ValueError, match="RAIL_AUTH_TOKEN"):
        TicketSource("http://rc.invalid", host_id="h", sandbox_name="s", auth_token="t")


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
    """The exception carries text the issuer chose. `report_ticket` renders it
    into a warning, and a message the length of the response cap is a log line
    nobody can read — and one that every stacked filter on the root handlers
    then walks, which is why the bound is worth more than tidiness."""
    from fastmcp_proxy import proxy as proxy_module

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
        await proxy_module.report_ticket(_Source())
    finally:
        logger.removeHandler(handler)

    # Lengths rather than the messages: a failure here would otherwise have
    # pytest render the whole oversized string it is complaining about.
    lengths = [len(r.getMessage()) for r in records]
    assert any("could not fetch" in r.getMessage()[:80] for r in records)
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
