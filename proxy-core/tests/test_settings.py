"""What the ticket half of a proxy's configuration accepts, and what it refuses.

These settings are shared by every interface, so they are tested here, called
directly. That each interface wires them in — exit 2 on a refusal, the refresh
interval reaching its holder — is tested by that interface.
"""

import pytest

from proxy.core import settings
from proxy.core.xrail_auth import TicketHolder, token_fingerprint


async def _hold(source):
    """What `main` does with a source: hold it, and fetch once.

    `TicketHolder.start()` also launches the refresh loop, which a test would
    then have to cancel; `refresh_once` is the half these assertions are about.
    """
    holder = TicketHolder(source)
    await holder.refresh_once()
    return holder


def test_no_rail_center_configured_is_a_supported_state():
    """An open-source deployment with no control plane. The proxy forwards and
    fetches nothing, and says nothing to get there — an empty environment is
    the plain-proxy configuration."""
    assert settings.ticket_settings() is None
    assert settings.build_ticket_source() is None


@pytest.mark.parametrize(
    ("present", "missing"),
    [
        (["RAIL_CENTER_URL"], ["RAIL_HOST_ID", "RAIL_SANDBOX_NAME"]),
        (["RAIL_CENTER_URL", "RAIL_HOST_ID"], ["RAIL_SANDBOX_NAME"]),
        (["RAIL_HOST_ID"], ["RAIL_CENTER_URL", "RAIL_SANDBOX_NAME"]),
    ],
    ids=["url-only", "no-sandbox", "host-only"],
)
def test_a_partly_configured_control_plane_is_refused(
    no_rail_center, monkeypatch, present, missing
):
    """Silence here is the dangerous reading: a host id without a sandbox name
    would otherwise invite an unnamed fetch, which answers for the whole host
    and cannot prove which entry is this proxy's own."""
    for name in present:
        monkeypatch.setenv(name, "https://rc.invalid" if "URL" in name else "value")

    with pytest.raises(settings.ConfigError) as info:
        settings.ticket_settings()

    for name in missing:
        assert name in str(info.value)


@pytest.mark.parametrize(
    ("mode", "token", "expected"),
    [(None, None, None), ("none", None, None), ("bearer", "rc_svc_x", "rc_svc_x")],
    ids=["unset", "none", "bearer"],
)
def test_the_auth_mode_resolves_to_a_token_or_to_nothing(
    no_rail_center, monkeypatch, mode, token, expected
):
    if mode is not None:
        monkeypatch.setenv("RAIL_AUTH_MODE", mode)
    if token is not None:
        monkeypatch.setenv("RAIL_AUTH_TOKEN", token)

    assert settings.auth_token() == expected


@pytest.mark.parametrize(
    "mode", ["gcp", "gateway", "gcp-workload"], ids=["gcp", "junk", "near-miss"]
)
def test_an_auth_mode_this_component_does_not_implement_is_refused(
    no_rail_center, monkeypatch, mode
):
    """`gcp` is a value the platform defines and this does not implement.
    Falling back to `none` would 401 on every fetch with nothing saying why.

    The token is set, and the match is on the allow-list's own wording: without
    both, this passes on the `bearer requires RAIL_AUTH_TOKEN` error instead —
    which a widened allow-list would still raise, so the check it names could be
    deleted outright and nothing would fail."""
    monkeypatch.setenv("RAIL_AUTH_MODE", mode)
    monkeypatch.setenv("RAIL_AUTH_TOKEN", "rc_svc_x")

    with pytest.raises(settings.ConfigError, match="is not one of"):
        settings.auth_token()


@pytest.mark.parametrize("token", ["", "   "], ids=["empty", "whitespace"])
def test_bearer_without_a_token_is_refused(no_rail_center, monkeypatch, token):
    """Sending nothing instead is indistinguishable from an unauthenticated
    deployment, and would appear to work against an issuer that does not yet
    require a credential."""
    monkeypatch.setenv("RAIL_AUTH_MODE", "bearer")
    monkeypatch.setenv("RAIL_AUTH_TOKEN", token)

    with pytest.raises(settings.ConfigError, match="requires RAIL_AUTH_TOKEN"):
        settings.auth_token()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", None),
        ("3600", 3600.0),
        ("0", None),
        ("-1", None),
        ("abc", None),
        ("inf", None),
    ],
    ids=["unset", "seconds", "zero", "negative", "junk", "infinite"],
)
def test_the_ticket_lifetime_bound_is_optional_and_refuses_nonsense(
    no_rail_center, monkeypatch, value, expected
):
    """Optional, and a value that is not a positive number of seconds is no
    bound at all — so it is reported rather than taken silently."""
    monkeypatch.setenv("RAIL_PROXY_MAX_TICKET_LIFETIME_SECONDS", value)

    assert settings.max_ticket_lifetime() == expected


@pytest.mark.asyncio
async def test_a_startup_fetch_reports_a_fingerprint_and_never_the_ticket(
    no_rail_center, monkeypatch, caplog
):
    """The point of fetching at startup is that a wrong sandbox name or a
    rejected credential is found while an operator is watching."""
    import json
    import pathlib as _pathlib

    import httpx

    body = json.loads(
        (
            _pathlib.Path(__file__).resolve().parents[2]
            / "proxy-core/tests/fixtures/tickets.json"
        ).read_text()
    )
    # The holder reads the wall clock, and the fixture names a fixed instant.
    # Left alone this lands on the already-expired branch, and the one whose
    # whole contract is "a fingerprint, never the ticket" is never reached.
    body["tickets"][0]["expires_at"] = "2099-01-01T00:00:00Z"
    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", "true")
    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "e2e-host")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "e2e-sandbox")

    real = settings.TicketSource

    def recording(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda _r: httpx.Response(200, json=body)
        )
        return real(*args, **kwargs)

    monkeypatch.setattr(settings, "TicketSource", recording)

    with caplog.at_level("INFO"):
        await _hold(settings.build_ticket_source())

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "ticket acquired (" in logged
    assert body["tickets"][0]["token"] not in logged
    assert token_fingerprint(body["tickets"][0]["token"]) in logged


@pytest.mark.asyncio
async def test_an_issuer_that_is_down_does_not_stop_startup(
    no_rail_center, monkeypatch, caplog
):
    """An issuer being unreachable is a normal condition; a configuration that
    could not be right is refused earlier."""
    import httpx

    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", "true")
    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")

    real = settings.TicketSource

    def failing(*args, **kwargs):
        def boom(_request):
            raise httpx.ConnectError("no route")

        kwargs["transport"] = httpx.MockTransport(boom)
        return real(*args, **kwargs)

    monkeypatch.setattr(settings, "TicketSource", failing)

    with caplog.at_level("WARNING"):
        await _hold(settings.build_ticket_source())

    # "no route" is the fake's own message: a real lookup of rc.invalid also
    # fails, so the failure alone would not show the fake was the one asked.
    assert any(
        "ticket refresh failed" in r.getMessage() and "no route" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize(
    "name",
    ["RAIL_CENTER_URL", "RAIL_HOST_ID", "RAIL_SANDBOX_NAME"],
)
def test_a_padded_ticket_setting_reads_as_unset(monkeypatch, name):
    """An unset compose interpolation yields whitespace as readily as an empty
    string, and a padded value here reads as *set* — so a deployment with no
    control plane exits 2 saying one is partly configured."""
    monkeypatch.setenv(name, "   ")

    assert settings.ticket_settings() is None


def test_a_padded_auth_mode_is_still_read(monkeypatch):
    """Same shape, different consequence: a padded `bearer` would fall through
    to the unknown-mode error rather than asking for a token."""
    monkeypatch.setenv("RAIL_AUTH_MODE", "  BEARER \n")
    monkeypatch.setenv("RAIL_AUTH_TOKEN", "rc_svc_x")

    assert settings.auth_token() == "rc_svc_x"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        (" yes ", True),
        ("1", True),
        ("false", False),
        ("", False),
        ("maybe", False),
    ],
    ids=["true", "upper", "padded", "one", "false", "unset", "junk"],
)
def test_the_insecure_credential_override_is_read_from_its_variable(
    monkeypatch, value, expected
):
    """The one setting that turns a security control off. Read wrongly in the
    permissive direction it is a token on a plaintext network; read wrongly in
    the other, a deployment that documented it exits 2."""
    monkeypatch.setenv("RAIL_PROXY_ALLOW_INSECURE_CREDENTIAL", value)

    assert settings.allow_insecure_credential() is expected


def test_the_override_reaches_the_source_it_governs(monkeypatch):
    """Parsing the variable and never passing it on is the same as not having
    it, and only the direction that breaks an operator would be silent."""
    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", "true")
    monkeypatch.setenv("RAIL_CENTER_URL", "http://rail-center:8000")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")
    monkeypatch.setenv("RAIL_AUTH_MODE", "bearer")
    monkeypatch.setenv("RAIL_AUTH_TOKEN", "rc_svc_x")

    with pytest.raises(settings.ConfigError, match="in the clear"):
        settings.build_ticket_source()

    monkeypatch.setenv("RAIL_PROXY_ALLOW_INSECURE_CREDENTIAL", "true")
    assert settings.build_ticket_source() is not None


@pytest.mark.parametrize(
    ("value", "expected", "warns"),
    [
        ("", 10.0, False),
        ("5", 5.0, False),
        ("0", 10.0, True),
        ("-1", 10.0, True),
        ("abc", 10.0, True),
        ("inf", 10.0, True),
    ],
    ids=["unset", "seconds", "zero", "negative", "junk", "infinite"],
)
def test_the_ticket_timeout_falls_back_rather_than_disabling_itself(
    monkeypatch, caplog, value, expected, warns
):
    """This wait happens before the port is bound, so no timeout is a container
    that never comes up — and somebody who asked for one should be told they
    did not get it."""
    monkeypatch.setenv("RAIL_PROXY_TICKET_TIMEOUT_SECONDS", value)

    with caplog.at_level("WARNING"):
        assert settings.ticket_timeout() == expected
    assert bool(caplog.records) is warns


def test_the_ticket_timeout_is_not_the_upstream_one(monkeypatch):
    """Separate knobs, because the waits fall in different places: raising the
    upstream timeout for a slow tool server must not also lengthen how long an
    unreachable Rail Center delays the bind."""
    monkeypatch.setenv("RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("RAIL_PROXY_TICKET_TIMEOUT_SECONDS", "4")
    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", "true")
    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")

    # A value that is neither the upstream timeout nor the constructor default,
    # so neither the wrong variable nor no variable at all can produce it.
    assert settings.build_ticket_source().timeout_seconds == 4.0


def test_the_lifetime_bound_reaches_the_source_it_configures(monkeypatch):
    """Parsed correctly and never passed on is the same as not having it."""
    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", "true")
    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")
    monkeypatch.setenv("RAIL_PROXY_MAX_TICKET_LIFETIME_SECONDS", "3600")

    assert settings.build_ticket_source().max_lifetime_seconds == 3600.0


def test_the_bearer_token_reaches_the_source_it_configures(monkeypatch):
    """Only the token's truthiness is pinned elsewhere, as a side effect of the
    plaintext refusal. A wrong value 401s at Rail Center, holds no ticket, and
    still logs `(bearer)` — the same shape as no wiring at all."""
    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", "true")
    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")
    monkeypatch.setenv("RAIL_AUTH_MODE", "bearer")
    monkeypatch.setenv("RAIL_AUTH_TOKEN", "  rc_svc_the_real_one  ")

    assert settings.build_ticket_source().auth_token == "rc_svc_the_real_one"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", False),
        ("false", False),
        ("true", True),
        ("  TRUE  ", True),
        ("  False  ", False),
    ],
    ids=["unset", "false", "true", "padded-true", "padded-false"],
)
def test_the_plugin_is_off_unless_it_is_turned_on(monkeypatch, value, expected):
    """A proxy nobody has given RailXia configuration needs no variable at all.
    What makes that default safe is not the default — it is the refusal in
    `build_ticket_source` below, which stops the one deployment where absence
    would be dangerous: the one that still names a Rail Center."""
    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", value)

    assert settings.plugin_enabled() is expected


@pytest.mark.parametrize("value", ["ture", "1", "yes", "on", "enforce"], ids=str)
def test_anything_that_is_not_true_or_false_is_refused(monkeypatch, value):
    """Parsed strictly rather than truthily. `1` and `yes` are what an operator
    means, but accepting them means accepting that everything else is `false` —
    and a proxy that read `ture` as off would unenroll on a typo, which is the
    silent failure this variable exists to end."""
    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", value)

    with pytest.raises(settings.ConfigError, match="RAIL_PLUGIN_ENABLED"):
        settings.plugin_enabled()


@pytest.mark.parametrize("value", ["enforce", " enforce ", "\tobserve\n"], ids=repr)
def test_a_leftover_ticket_mode_stops_the_proxy_and_names_its_replacement(
    monkeypatch, value
):
    """Ignoring it is the failure this whole change exists to end: an operator
    who sets a variable believing it configures a posture, and silently gets
    whatever the default happens to be. Padding is not what decides it: a value
    surrounded by spaces is a value an operator wrote."""
    monkeypatch.setenv("RAIL_TICKET_MODE", value)

    with pytest.raises(settings.ConfigError) as info:
        settings.plugin_enabled()

    assert "RAIL_TICKET_MODE" in str(info.value)
    assert "RAIL_PLUGIN_ENABLED" in str(info.value)


@pytest.mark.parametrize("value", ["", " ", "\t\n"], ids=repr)
def test_a_ticket_mode_carrying_no_value_is_not_a_leftover(monkeypatch, value):
    """The other side of that boundary, and the side a migration lands on:
    `v0.1.0` shipped `RAIL_TICKET_MODE=` in its `.env.example`, so an operator
    who blanks the line rather than deleting it holds an empty variable and not
    a posture. Refusing that would stop a deployment over a line configuring
    nothing."""
    monkeypatch.setenv("RAIL_TICKET_MODE", value)

    assert settings.plugin_enabled() is False


def test_attaching_without_an_issuer_to_fetch_from_is_a_config_error(monkeypatch):
    """The half of the cross-check that matters most: a proxy that meant to
    identify its agent and lost `RAIL_CENTER_URL` would otherwise come up
    healthy, forward everything unstamped, and be discovered downstream."""
    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", "true")

    with pytest.raises(settings.ConfigError) as info:
        settings.build_ticket_source()

    # Every one that is missing, by name. This is a container log, and an
    # operator reading it cannot see which of three they got wrong.
    assert "RAIL_CENTER_URL" in str(info.value)
    assert "RAIL_HOST_ID" in str(info.value)
    assert "RAIL_SANDBOX_NAME" in str(info.value)
    # Nothing else names a Rail Center here, so unsetting the flag is the whole
    # action and the advice adds nothing to it.
    assert str(info.value).endswith("unset RAIL_PLUGIN_ENABLED.")


@pytest.mark.parametrize(
    "env",
    [
        {
            "RAIL_AUTH_MODE": "bearer",
            "RAIL_AUTH_TOKEN": "rc_service_token_abc123",
        },
        {"RAIL_CENTER_URL": "https://rc.invalid"},
    ],
    ids=["credential-only", "address-only"],
)
def test_attaching_without_an_issuer_advises_shedding_the_whole_intent(
    monkeypatch, env
):
    """A proxy that cannot find its Rail Center may still name one by the part
    it does hold, so advice naming the flag alone sends an operator straight
    into the contradictory-intent refusal above — a second container-down
    message for following the first one exactly."""
    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", "true")
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(settings.ConfigError) as info:
        settings.build_ticket_source()

    assert "unset RAIL_PLUGIN_ENABLED" in str(info.value)
    # From `_naming_a_rail_center()`, the expression the cross-check evaluates,
    # so the advice cannot be short of what that check keys on.
    assert all(name in str(info.value) for name in settings._naming_a_rail_center())
    # Which is what that has to mean: unset exactly what the message names and
    # this proxy forwards as a plain one, with nothing left to stop on.
    for name in ("RAIL_PLUGIN_ENABLED", *env):
        if name in str(info.value):
            monkeypatch.delenv(name)
    assert settings.build_ticket_source() is None


@pytest.mark.parametrize(
    ("present", "absent"),
    [
        ("RAIL_CENTER_URL", ("RAIL_HOST_ID", "RAIL_SANDBOX_NAME")),
        ("RAIL_HOST_ID", ("RAIL_CENTER_URL", "RAIL_SANDBOX_NAME")),
        ("RAIL_SANDBOX_NAME", ("RAIL_CENTER_URL", "RAIL_HOST_ID")),
    ],
    ids=lambda p: p if isinstance(p, str) else "",
)
def test_a_partly_configured_issuer_names_only_what_is_missing(
    monkeypatch, present, absent
):
    """Naming all three when two are already set sends an operator to re-check
    work they did correctly."""
    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", "true")
    monkeypatch.setenv(present, "value")

    with pytest.raises(settings.ConfigError) as info:
        settings.build_ticket_source()

    # The diagnosis only. The advice below it names the one that is set on
    # purpose — that is what an operator sheds to forward as a plain proxy —
    # so the message is read up to where it stops describing and starts
    # advising.
    diagnosis = str(info.value).split(". To forward without one")[0]
    assert present not in diagnosis
    assert all(name in diagnosis for name in absent)


def test_pass_through_beside_a_stray_variable_reports_the_contradiction(monkeypatch):
    """One naming variable left set beside an off plugin is contradictory
    intent, not an unfinished configuration — telling that operator to set the
    other two would be advising them to finish something they did not start."""
    monkeypatch.setenv("RAIL_HOST_ID", "h")

    with pytest.raises(settings.ConfigError) as info:
        settings.build_ticket_source()

    assert "was not meant" in str(info.value)
    assert "RAIL_HOST_ID" in str(info.value)
    assert "also required" not in str(info.value)


def test_a_configured_issuer_that_will_never_be_asked_is_a_config_error(monkeypatch):
    """The other half, and the one that makes the off default safe. A Rail
    Center configured beside a plugin nobody turned on is the shape of a
    deployment that lost its flag, so it stops rather than quietly becoming a
    plain proxy — which is what PTH.G1 rejected deciding from presence alone."""
    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")

    with pytest.raises(settings.ConfigError) as info:
        settings.build_ticket_source()

    assert "was not meant" in str(info.value)
    # Both remedies, because the diagnosis alone leaves an operator holding a
    # container that is down and a choice about which half they meant. This is
    # also where `refuse_a_credential_in_the_url` sends them, so it
    # is the advice that has to survive.
    assert "Set RAIL_PLUGIN_ENABLED=true to attach" in str(info.value)
    assert "unset them to forward without a ticket" in str(info.value)


@pytest.mark.parametrize(
    ("env", "named"),
    [
        ({"RAIL_AUTH_TOKEN": "rc_service_token_abc123"}, "RAIL_AUTH_TOKEN"),
        ({"RAIL_AUTH_MODE": "bearer"}, "RAIL_AUTH_MODE"),
    ],
    ids=["token", "mode"],
)
def test_a_rail_center_credential_alone_is_a_configured_issuer_too(
    monkeypatch, env, named
):
    """A split-source loss, which is the ordinary Kubernetes shape: the token
    comes from a Secret and the address from a ConfigMap, so losing one leaves
    the other. Without this the proxy comes up forwarding unstamped while
    holding a credential nothing but an enrolled proxy has a use for."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(settings.ConfigError) as info:
        settings.build_ticket_source()

    assert "was not meant" in str(info.value)
    assert named in str(info.value)


@pytest.mark.parametrize(
    "env",
    [
        {"RAIL_AUTH_MODE": "", "RAIL_AUTH_TOKEN": ""},
        {"RAIL_AUTH_MODE": "none", "RAIL_AUTH_TOKEN": ""},
        {"RAIL_AUTH_MODE": "NONE", "RAIL_AUTH_TOKEN": "  "},
        {"RAIL_AUTH_MODE": " none ", "RAIL_AUTH_TOKEN": ""},
    ],
    ids=["env-example", "compose-default", "padded-token", "padded-mode"],
)
def test_auth_variables_carrying_no_credential_are_not_a_configured_issuer(
    monkeypatch, env
):
    """The shapes that ship: `.env.example` carries both keys empty, and the
    demo compose passes `RAIL_AUTH_MODE=${RAIL_AUTH_MODE:-none}` beside an empty
    token to every proxy it runs. A proxy that authenticates with nothing names
    no intent to attach, and stopping those would refuse a configuration that
    forwards correctly today."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert settings.build_ticket_source() is None


@pytest.mark.parametrize(
    ("value", "expected", "warns"),
    [
        ("", 3600.0, False),
        ("60", 60.0, False),
        ("0", 3600.0, True),
        ("x", 3600.0, True),
    ],
    ids=["unset", "seconds", "zero", "junk"],
)
def test_the_refresh_interval_falls_back_rather_than_disabling_itself(
    monkeypatch, caplog, value, expected, warns
):
    """An upper bound, not the interval: a ticket's own expiry drives the real
    cadence. Zero would be a poll loop against Rail Center."""
    monkeypatch.setenv("RAIL_PROXY_REFRESH_SECONDS", value)

    with caplog.at_level("WARNING"):
        assert settings.refresh_seconds() == expected
    assert bool(caplog.records) is warns


@pytest.mark.parametrize(
    ("name", "value", "fallback"),
    [
        ("RAIL_PROXY_TICKET_TIMEOUT_SECONDS", "0", "10"),
        ("RAIL_PROXY_MAX_TICKET_LIFETIME_SECONDS", "0", "no bound"),
        # The other warning branch. `0` is a number and takes the not-positive
        # one, so without these the not-a-number message need not name anything.
        ("RAIL_PROXY_TICKET_TIMEOUT_SECONDS", "abc", "10"),
        ("RAIL_PROXY_MAX_TICKET_LIFETIME_SECONDS", "abc", "no bound"),
    ],
)
def test_a_rejected_setting_names_itself_and_what_it_fell_back_to(
    monkeypatch, caplog, name, value, fallback
):
    """One parser serves several variables, so the name in the message is the
    only thing telling an operator which of them they got wrong."""
    monkeypatch.setenv(name, value)

    with caplog.at_level("WARNING"):
        settings.ticket_timeout()
        settings.max_ticket_lifetime()

    messages = [r.getMessage() for r in caplog.records]
    assert any(name in m and f"using {fallback}" in m for m in messages), messages


def _attaching(monkeypatch):
    """A proxy configured to attach, which is when the url checks run."""
    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", "true")
    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")


@pytest.mark.parametrize(
    "url",
    [
        "https://u:p@gateway.invalid/mcp",
        "https://token@gateway.invalid/mcp",
        "https://:s3cret@gateway.invalid/mcp",
    ],
    ids=["both", "username-only", "password-only"],
)
def test_an_upstream_url_carrying_a_credential_is_refused(monkeypatch, url):
    """Either part is a credential: httpx derives Basic auth from either, and
    `urlsplit("https://:s3cret@h/").username` is the empty string, so a guard
    reading one part lets the other form past."""
    _attaching(monkeypatch)

    with pytest.raises(settings.ConfigError, match="carries a credential") as info:
        settings.refuse_a_credential_in_the_url("delivery", url)

    message = str(info.value)
    assert "upstream 'delivery'" in message
    # The advice is the whole action, so following it does not land on the
    # contradictory-intent refusal.
    for name in ("RAIL_CENTER_URL", "RAIL_HOST_ID", "RAIL_SANDBOX_NAME"):
        assert name in message


def test_an_upstream_url_without_a_credential_is_accepted(monkeypatch):
    _attaching(monkeypatch)

    settings.refuse_a_credential_in_the_url("delivery", "https://gateway.invalid/mcp")


@pytest.mark.parametrize(
    ("url", "warns"),
    [
        ("http://gateway.invalid/mcp", True),
        ("https://gateway.invalid/mcp", False),
        ("http://127.0.0.1:8080/mcp", False),
        ("http://localhost:8080/mcp", False),
    ],
    ids=["plaintext", "https", "loopback-ip", "loopback-name"],
)
def test_a_plaintext_upstream_is_warned_about_by_name_and_url(caplog, url, warns):
    """Not refused: an http upstream on a private network is ordinary. Loopback
    never leaves the host, so it is not worth a warning."""
    with caplog.at_level("WARNING"):
        settings.warn_if_in_the_clear("delivery", url)

    lines = [
        r.getMessage() for r in caplog.records if "plaintext http" in r.getMessage()
    ]
    assert bool(lines) is warns
    if warns:
        assert lines == [
            (
                "'delivery' is plaintext http — the x-rail ticket is readable by "
                f"anyone on the path to {url}"
            )
        ]
