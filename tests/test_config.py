"""What the proxy does with a configuration it cannot serve.

Every case here ends the same way for an operator — the container stops with a
sentence naming the file and the problem — and the point of the parametrisation
is that a hand-edited YAML file goes wrong in more shapes than an empty one.
"""

from __future__ import annotations

import logging
import pathlib
import re
import sys

import pytest

from fastmcp_proxy import proxy as proxy_module


def test_a_missing_config_file_is_reported_by_path(write_config, monkeypatch, tmp_path):
    """The image bakes RAIL_PROXY_CONFIG_FILE to a path holding no file, so a
    container started without a mounted config lands here."""
    monkeypatch.setenv("RAIL_PROXY_CONFIG_FILE", str(tmp_path / "absent.yaml"))

    # The path is what the message is for: an operator seeing this needs to know
    # which file was looked for, not that a file was.
    with pytest.raises(proxy_module.ConfigError, match=r"cannot read .*absent\.yaml"):
        proxy_module.load_servers()


@pytest.mark.parametrize(
    "value", ["", "   ", "\t\n"], ids=["empty", "spaces", "whitespace"]
)
def test_a_blank_config_path_falls_back_to_the_packaged_default(monkeypatch, value):
    """An unset compose interpolation yields an empty string, and `Path("")` is
    the current directory — which exists, so a naive check passes and the read
    fails on a directory instead of reporting a missing config. Whitespace is
    the same mistake with a space in it."""
    monkeypatch.setenv("RAIL_PROXY_CONFIG_FILE", value)

    # Compared against the path itself rather than against the constant the
    # function returns, which would hold however the constant was defined.
    assert proxy_module.config_file() == (
        pathlib.Path(proxy_module.__file__).parent / "bridge.yaml"
    )


def test_a_config_that_is_not_utf_8_is_reported_rather_than_raised(
    write_config, tmp_path, monkeypatch
):
    """UnicodeDecodeError is not an OSError, so it escapes the read guard
    unless it is caught on its own."""
    path = tmp_path / "bridge.yaml"
    path.write_bytes(b"mcp:\n  servers:\n    - name: \xff\xfe\n")
    monkeypatch.setenv("RAIL_PROXY_CONFIG_FILE", str(path))

    with pytest.raises(proxy_module.ConfigError, match="not valid UTF-8"):
        proxy_module.load_servers()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("", "names no upstream"),
        ("mcp:\n  servers: []\n", "names no upstream"),
        ("mcp:\n  servers:\n    - name: no-url\n", "names no upstream"),
        (
            "mcp:\n  servers:\n    - url: http://no-name.invalid/mcp\n",
            "names no upstream",
        ),
        ("- a\n- b\n", "must hold a mapping"),
        ("just a string\n", "must hold a mapping"),
        ("mcp: a string\n", "`mcp` must be a mapping"),
        ("mcp:\n  servers: a string\n", "must be a list"),
        ("mcp:\n  servers:\n   - name: x\n  bad indent\n", "not valid YAML"),
    ],
    ids=[
        "empty-file",
        "empty-list",
        "name-without-url",
        "url-without-name",
        "top-level-list",
        "top-level-scalar",
        "mcp-not-a-mapping",
        "servers-not-a-list",
        "malformed-yaml",
    ],
)
def test_an_unusable_config_raises_rather_than_crashing(write_config, body, expected):
    """A container that stops with the reason, rather than crash-looping on a
    stack trace."""
    write_config(body)

    with pytest.raises(proxy_module.ConfigError, match=expected):
        proxy_module.load_servers()


@pytest.mark.parametrize(
    "url",
    ["gateway:8080/mcp", "//gateway:8080/mcp", "ftp://gateway/mcp", "/mcp"],
    ids=["no-scheme", "protocol-relative", "wrong-scheme", "path-only"],
)
def test_an_unusable_url_is_reported_rather_than_raised_from_the_mount(
    write_config, url
):
    """The transport rejects these with a ValueError from the mount loop, past
    every handler — a traceback and exit 1, where the file's other mistakes give
    a sentence and exit 2. `gateway:8080/mcp` is the packaged example minus its
    scheme, which is the likeliest hand-edit of this file."""
    write_config(f"mcp:\n  servers:\n    - name: delivery\n      url: {url}\n")

    with pytest.raises(proxy_module.ConfigError, match="scheme"):
        proxy_module.load_servers()


def test_usable_entries_survive_alongside_unusable_ones(write_config):
    write_config(
        "mcp:\n"
        "  servers:\n"
        "    - name: good\n      url: http://upstream.invalid/mcp\n"
        "    - name: half\n"
        "    - not-a-mapping\n"
    )

    assert [s["name"] for s in proxy_module.load_servers()] == ["good"]


@pytest.mark.asyncio
async def test_the_entrypoint_turns_an_unusable_config_into_exit_2(write_config):
    """`load_servers` raises so that importing this module cannot take its
    caller down; `main` is where that becomes the container's exit code."""
    write_config("- not a mapping\n")

    assert await proxy_module.main() == 2


@pytest.mark.parametrize(
    ("level", "effective"),
    [("debug", 10), ("WARNING", 30), ("not-a-level", 20)],
    ids=["lowercase", "exact", "unusable-falls-back"],
)
def test_the_log_level_is_applied_or_fallen_back_from(monkeypatch, level, effective):
    """An unusable level is a complaint, not an exit: the proxy's job does not
    depend on it, and dying over a log setting loses the traffic too."""
    monkeypatch.setenv("RAIL_PROXY_LOG_LEVEL", level)
    monkeypatch.setattr(proxy_module.logging.root, "handlers", [])

    proxy_module.configure_logging()

    assert proxy_module.logging.root.level == effective


@pytest.mark.asyncio
async def test_an_unusable_port_is_reported_rather_than_crashing(config, monkeypatch):
    """Read where it can be reported, not at import — a module that cannot be
    imported has nowhere to say why."""
    monkeypatch.setenv("RAIL_PROXY_PORT", "8091x")

    assert await proxy_module.main() == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("WARN", "WARNING"),
        ("FATAL", "CRITICAL"),
        ("NOTSET", "INFO"),
        ("TRACE", "TRACE"),
        ("debug", "DEBUG"),
        ("", "INFO"),
        ("not-a-level", "INFO"),
    ],
    ids=["warn-alias", "fatal-alias", "notset", "trace", "lowercase", "empty", "junk"],
)
def test_the_level_resolves_to_one_uvicorn_accepts(monkeypatch, value, expected):
    """uvicorn's vocabulary is the narrower one, and it resolves a level by dict
    lookup — a miss is a KeyError after startup has begun."""
    monkeypatch.setenv("RAIL_PROXY_LOG_LEVEL", value)

    assert proxy_module.log_level() == expected


def test_every_resolvable_level_is_one_uvicorn_can_use():
    """The guard this pins is that the two vocabularies stay reconciled: uvicorn
    is the narrower, and nothing else in the suite exercises its leg."""
    import uvicorn.config

    for level in proxy_module._LEVELS:
        assert level.lower() in uvicorn.config.LOG_LEVELS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "port", ["0", "-1", "70000"], ids=["zero", "negative", "too-big"]
)
async def test_a_port_outside_the_range_is_reported(config, monkeypatch, port):
    """Every one of these is reported rather than left to `bind()`."""
    monkeypatch.setenv("RAIL_PROXY_PORT", port)

    assert await proxy_module.main() == 2


@pytest.mark.parametrize(
    ("value", "expected", "warns"),
    [
        ("", 30.0, False),
        ("5", 5.0, False),
        ("2.5", 2.5, False),
        ("0", 30.0, True),
        ("-1", 30.0, True),
        ("abc", 30.0, True),
        ("inf", 30.0, True),
        ("1e400", 30.0, True),
    ],
    ids=[
        "unset",
        "integer",
        "fractional",
        "zero",
        "negative",
        "junk",
        "inf",
        "overflow",
    ],
)
def test_the_upstream_timeout_falls_back_rather_than_disabling_itself(
    monkeypatch, caplog, value, expected, warns
):
    """A timeout of none is how a hung upstream becomes an agent waiting for
    ever, which is what this setting exists to bound — so zero, negative and
    non-finite are refused, and refused audibly: somebody who asked for one
    should be told they did not get it. `inf` also reaches the transport as an
    OverflowError past every handler if it is let through."""
    monkeypatch.setenv("RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS", value)

    with caplog.at_level("WARNING"):
        assert proxy_module.upstream_timeout() == expected

    assert bool(caplog.records) is warns


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", "0.0.0.0"), ("   ", "0.0.0.0"), (" 127.0.0.1 ", "127.0.0.1")],
    ids=["empty", "whitespace", "padded"],
)
def test_the_bind_address_is_stripped_like_every_other_setting(
    monkeypatch, value, expected
):
    """Nothing validates a bind address beyond the strip — a genuinely bad host
    fails inside uvicorn, which is uvicorn's to report."""
    monkeypatch.setenv("RAIL_PROXY_BIND", value)

    assert proxy_module.bind_address() == expected


@pytest.mark.parametrize(
    ("value", "warns"),
    [("WARNING", False), ("WARN", False), ("", False), ("not-a-level", True)],
    ids=["exact", "alias", "unset", "junk"],
)
def test_only_an_unusable_level_is_complained_about(monkeypatch, caplog, value, warns):
    """An alias and an unset value are both handled rather than wrong, so
    neither should produce a line an operator has to read past."""
    # Root's handlers are left alone: caplog captures through one, so clearing
    # them removes the very thing this asserts against.
    monkeypatch.setenv("RAIL_PROXY_LOG_LEVEL", value)

    with caplog.at_level("WARNING"):
        proxy_module.configure_logging()

    assert any("is not a level" in r.message for r in caplog.records) is warns


@pytest.mark.asyncio
async def test_the_settings_reach_uvicorn(config, monkeypatch):
    """Each accessor is pinned above; this pins that `main` uses them. A call
    site reading the environment raw — or passing a literal — leaves every one
    of those assertions green, because none of them touches `main`.

    The log level in particular: uvicorn installs its own loggers, so a level
    that does not reach this config leaves the access log and the startup lines
    at INFO whatever the variable said.
    """
    monkeypatch.setenv("RAIL_PROXY_BIND", "  127.0.0.1  ")
    monkeypatch.setenv("RAIL_PROXY_PORT", " 9099 ")
    monkeypatch.setenv("RAIL_PROXY_LOG_LEVEL", "warning")
    monkeypatch.setattr(logging.root, "handlers", [], raising=False)
    captured: dict = {}

    import uvicorn

    class StubServer:
        def __init__(self, config):
            captured.update(
                host=config.host,
                port=config.port,
                log_level=config.log_level,
                app=config.app,
            )

        async def serve(self):
            return None

    monkeypatch.setattr(uvicorn, "Server", StubServer)

    assert await proxy_module.main() == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9099
    assert captured["log_level"] == "warning"
    # The shim, not the app inside it. Served unwrapped, every non-POST verb
    # reaches the session manager on the endpoint an untrusted sandbox can
    # reach — and every end-to-end test builds the app itself, so none of them
    # would notice.
    assert isinstance(captured["app"], proxy_module.McpMethodCompat)
    # Installed by `main`, not left over from a test that called
    # `configure_logging` itself — hence the handlers cleared above. Without
    # this the container runs with no redaction at all, and httpx puts the whole
    # url on stdout once per request.
    assert any(
        isinstance(f, proxy_module.RedactingFilter)
        for h in logging.root.handlers
        for f in h.filters
    )


def test_trace_is_translated_for_the_root_logger(monkeypatch):
    """TRACE is uvicorn's alone. `logging.basicConfig(level="TRACE")` raises
    ValueError, which would escape configure_logging before main's ConfigError
    handler — a crash-loop from a log setting. Nothing else calls
    configure_logging with it."""
    monkeypatch.setenv("RAIL_PROXY_LOG_LEVEL", "TRACE")
    monkeypatch.setattr(proxy_module.logging.root, "handlers", [])

    proxy_module.configure_logging()

    assert proxy_module.logging.root.level == proxy_module.logging.DEBUG


@pytest.mark.asyncio
@pytest.mark.parametrize("port", ["   ", "\t"], ids=["spaces", "tab"])
async def test_a_blank_port_falls_back_rather_than_failing_to_parse(
    config, monkeypatch, port
):
    """A padded unset compose interpolation. `int(" 9099 ")` already tolerates
    padding on a real value, so only a blank one pins the strip — without it
    this exits 2 on a variable whose documented default is 8091."""
    monkeypatch.setenv("RAIL_PROXY_PORT", port)
    captured: dict = {}

    import uvicorn

    class StubServer:
        def __init__(self, config):
            captured["port"] = config.port

        async def serve(self):
            return None

    monkeypatch.setattr(uvicorn, "Server", StubServer)

    assert await proxy_module.main() == 0
    assert captured["port"] == 8091


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "HTTP Request: POST http://svc:s3cr3t@upstream.invalid/mcp",
            "HTTP Request: POST http://***@upstream.invalid/mcp",
        ),
        # No username, only a password: httpx sends `Basic OnMzY3JldA==` for
        # this, so it is a working credential and not junk.
        (
            "mounted 'd' -> https://:s3cr3t@upstream.invalid/mcp",
            "mounted 'd' -> https://***@upstream.invalid/mcp",
        ),
        (
            "http://user:pw@[::1]:8080/mcp",
            "http://***@[::1]:8080/mcp",
        ),
        # A password containing its own `@`. Taken to the first one, the tail of
        # the secret stays in the line the filter exists to scrub.
        (
            "HTTP Request: GET https://svc:p@ssw0rd@rc.invalid/v1/tickets",
            "HTTP Request: GET https://***@rc.invalid/v1/tickets",
        ),
        # Nothing to redact must survive untouched.
        (
            "mounted 'd' -> http://upstream.invalid/mcp",
            "mounted 'd' -> http://upstream.invalid/mcp",
        ),
        ("an email addr@example.com in prose", "an email addr@example.com in prose"),
    ],
    ids=[
        "httpx-line",
        "password-only",
        "ipv6",
        "at-in-password",
        "no-credential",
        "not-a-url",
    ],
)
def test_a_credential_in_a_url_is_redacted_from_any_message(message, expected):
    assert proxy_module._redact(message) == expected


def test_an_entry_missing_a_key_is_announced_rather_than_dropped(write_config, caplog):
    """`urls:` for `url:` is a plausible hand-edit, and every other rejected
    setting in this module says so."""
    write_config(
        "mcp:\n  servers:\n"
        "    - name: delivery\n      url: http://a.invalid/mcp\n"
        "    - name: payments\n      urls: http://b.invalid/mcp\n"
    )

    with caplog.at_level("WARNING"):
        assert [s["name"] for s in proxy_module.load_servers()] == ["delivery"]

    assert any("payments" in r.getMessage() for r in caplog.records)


def test_two_upstreams_cannot_share_a_namespace(write_config):
    """The name is the prefix every tool carries, so a shared one shadows: the
    loser is listed by nobody and called by nobody."""
    write_config(
        "mcp:\n  servers:\n"
        "    - name: delivery\n      url: http://a.invalid/mcp\n"
        "    - name: delivery\n      url: http://b.invalid/mcp\n"
    )

    with pytest.raises(proxy_module.ConfigError, match="duplicate upstream name"):
        proxy_module.load_servers()


def test_no_rail_center_configured_is_a_supported_state(no_rail_center):
    """An open-source deployment with no control plane. The proxy forwards and
    fetches nothing."""
    assert proxy_module.ticket_settings() is None
    assert proxy_module.build_ticket_source() is None


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

    with pytest.raises(proxy_module.ConfigError) as info:
        proxy_module.ticket_settings()

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

    assert proxy_module.auth_token() == expected


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

    with pytest.raises(proxy_module.ConfigError, match="is not one of"):
        proxy_module.auth_token()


@pytest.mark.parametrize("token", ["", "   "], ids=["empty", "whitespace"])
def test_bearer_without_a_token_is_refused(no_rail_center, monkeypatch, token):
    """Sending nothing instead is indistinguishable from an unauthenticated
    deployment, and would appear to work against an issuer that does not yet
    require a credential."""
    monkeypatch.setenv("RAIL_AUTH_MODE", "bearer")
    monkeypatch.setenv("RAIL_AUTH_TOKEN", token)

    with pytest.raises(proxy_module.ConfigError, match="requires RAIL_AUTH_TOKEN"):
        proxy_module.auth_token()


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

    assert proxy_module.max_ticket_lifetime() == expected


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
        (_pathlib.Path(__file__).parent / "fixtures" / "tickets.json").read_text()
    )
    # `report_ticket` reads the wall clock, and the fixture names a fixed
    # instant. Left alone, this lands on the expired branch and the success
    # branch — the one whose whole contract is "a fingerprint, never the
    # ticket" — is never reached at all.
    body["tickets"][0]["expires_at"] = "2099-01-01T00:00:00Z"
    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "e2e-host")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "e2e-sandbox")

    real = proxy_module.TicketSource

    def recording(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda _r: httpx.Response(200, json=body)
        )
        return real(*args, **kwargs)

    monkeypatch.setattr(proxy_module, "TicketSource", recording)

    with caplog.at_level("INFO"):
        await proxy_module.report_ticket(proxy_module.build_ticket_source())

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "ticket held (" in logged
    assert body["tickets"][0]["token"] not in logged
    assert proxy_module.token_fingerprint(body["tickets"][0]["token"]) in logged


@pytest.mark.asyncio
async def test_an_issuer_that_is_down_does_not_stop_startup(
    no_rail_center, monkeypatch, caplog
):
    """An issuer being unreachable is a normal condition; a configuration that
    could not be right is refused earlier."""
    import httpx

    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")

    real = proxy_module.TicketSource

    def failing(*args, **kwargs):
        def boom(_request):
            raise httpx.ConnectError("no route")

        kwargs["transport"] = httpx.MockTransport(boom)
        return real(*args, **kwargs)

    monkeypatch.setattr(proxy_module, "TicketSource", failing)

    with caplog.at_level("WARNING"):
        await proxy_module.report_ticket(proxy_module.build_ticket_source())

    assert any("could not fetch a ticket" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (
            {"RAIL_CENTER_URL": "https://rc.invalid", "RAIL_HOST_ID": "h"},
            "RAIL_SANDBOX_NAME",
        ),
        (
            {
                "RAIL_CENTER_URL": "https://rc.invalid",
                "RAIL_HOST_ID": "h",
                "RAIL_SANDBOX_NAME": "s",
                "RAIL_AUTH_MODE": "gcp",
            },
            "RAIL_AUTH_MODE",
        ),
    ],
    ids=["partly-configured", "unimplemented-auth-mode"],
)
async def test_a_bad_ticket_configuration_exits_2_like_every_other(
    config, no_rail_center, monkeypatch, env, expected
):
    """A ticket configuration that could not be right is a configuration error
    like any other: a sentence and exit 2, not a traceback. `report_ticket` is
    deliberately never fatal, so the source is built where the other config
    errors are caught rather than inside it."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert await proxy_module.main() == 2


@pytest.mark.parametrize(
    "name",
    ["RAIL_CENTER_URL", "RAIL_HOST_ID", "RAIL_SANDBOX_NAME"],
)
def test_a_padded_ticket_setting_reads_as_unset(monkeypatch, name):
    """An unset compose interpolation yields whitespace as readily as an empty
    string, and a padded value here reads as *set* — so a deployment with no
    control plane exits 2 saying one is partly configured."""
    monkeypatch.setenv(name, "   ")

    assert proxy_module.ticket_settings() is None


def test_a_padded_auth_mode_is_still_read(monkeypatch):
    """Same shape, different consequence: a padded `bearer` would fall through
    to the unknown-mode error rather than asking for a token."""
    monkeypatch.setenv("RAIL_AUTH_MODE", "  BEARER \n")
    monkeypatch.setenv("RAIL_AUTH_TOKEN", "rc_svc_x")

    assert proxy_module.auth_token() == "rc_svc_x"


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

    assert proxy_module.allow_insecure_credential() is expected


def test_the_override_reaches_the_source_it_governs(monkeypatch):
    """Parsing the variable and never passing it on is the same as not having
    it, and only the direction that breaks an operator would be silent."""
    monkeypatch.setenv("RAIL_CENTER_URL", "http://rail-center:8000")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")
    monkeypatch.setenv("RAIL_AUTH_MODE", "bearer")
    monkeypatch.setenv("RAIL_AUTH_TOKEN", "rc_svc_x")

    with pytest.raises(proxy_module.ConfigError, match="in the clear"):
        proxy_module.build_ticket_source()

    monkeypatch.setenv("RAIL_PROXY_ALLOW_INSECURE_CREDENTIAL", "true")
    assert proxy_module.build_ticket_source() is not None


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
        assert proxy_module.ticket_timeout() == expected
    assert bool(caplog.records) is warns


def test_the_ticket_timeout_is_not_the_upstream_one(monkeypatch):
    """Separate knobs, because the waits fall in different places: raising the
    upstream timeout for a slow tool server must not also lengthen how long an
    unreachable Rail Center delays the bind."""
    monkeypatch.setenv("RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("RAIL_PROXY_TICKET_TIMEOUT_SECONDS", "4")
    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")

    # A value that is neither the upstream timeout nor the constructor default,
    # so neither the wrong variable nor no variable at all can produce it.
    assert proxy_module.build_ticket_source().timeout_seconds == 4.0


@pytest.mark.asyncio
async def test_an_expired_ticket_is_not_reported_as_held(monkeypatch, caplog):
    """An expired ticket is a well-formed answer, so it arrives on the success
    path. Announcing it as held would report a dead identity as a healthy one,
    in the one log line this fetch exists to produce."""
    import httpx

    from fastmcp_proxy.xrail_auth import TicketSource

    source = TicketSource(
        "https://rc.invalid",
        host_id="h",
        sandbox_name="s",
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(
                200,
                json={
                    "host_id": "h",
                    "tickets": [
                        {
                            "token": "t",
                            "sandbox_name": "s",
                            "expires_at": "2020-01-01T00:00:00Z",
                        }
                    ],
                },
            )
        ),
    )

    with caplog.at_level("INFO"):
        await proxy_module.report_ticket(source)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "already expired" in logged
    assert "expires in" not in logged
    # A positive number of seconds. Rendered from `remaining()` unnegated, the
    # line reads "expired -3600s ago".
    assert re.search(r"expired (\d+)s ago", logged)


@pytest.mark.asyncio
async def test_startup_fetches_the_ticket_rather_than_only_building_the_source(
    config, upstream, monkeypatch, caplog
):
    """`report_ticket` tested directly says nothing about whether `main` calls
    it. Disconnected, every other test still passes and the feature is gone."""
    import httpx

    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")
    real = proxy_module.TicketSource

    def recording(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"host_id": "h", "tickets": []})
        )
        return real(*args, **kwargs)

    monkeypatch.setattr(proxy_module, "TicketSource", recording)
    served: list[object] = []

    class _Server:
        def __init__(self, config):
            served.append(config)

        async def serve(self):
            served.append(
                "fetching this proxy's ticket"
                in "\n".join(r.getMessage() for r in caplog.records)
            )

    monkeypatch.setattr("uvicorn.Server", _Server)

    with caplog.at_level("INFO"):
        assert await proxy_module.main() == 0

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "fetching this proxy's ticket" in logged
    # The authoritative-empty answer, reported as itself. Folded into the
    # generic failure handler it reads as "could not fetch a ticket", which is
    # the opposite thing: an issuer that is down, rather than one saying this
    # proxy has no identity.
    assert "no ticket held" in logged
    # The auth state, which is `describe()`'s and not the raw url's. It is the
    # only thing in this line telling an operator how the proxy authenticated.
    assert "(unauthenticated)" in logged
    # Started, and started *after* the fetch — which is the whole reason the
    # fetch has a timeout of its own rather than borrowing the upstream one.
    # Moved below `serve()`, every assertion above still holds, because the
    # stub returns at once and production would not.
    assert served[1:] == [True], "the fetch did not precede the listener"


def test_the_lifetime_bound_reaches_the_source_it_configures(monkeypatch):
    """Parsed correctly and never passed on is the same as not having it."""
    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")
    monkeypatch.setenv("RAIL_PROXY_MAX_TICKET_LIFETIME_SECONDS", "3600")

    assert proxy_module.build_ticket_source().max_lifetime_seconds == 3600.0


@pytest.mark.parametrize(
    ("name", "value", "fallback"),
    [
        ("RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS", "0", "30"),
        ("RAIL_PROXY_TICKET_TIMEOUT_SECONDS", "0", "10"),
        ("RAIL_PROXY_MAX_TICKET_LIFETIME_SECONDS", "0", "no bound"),
        # The other warning branch. `0` is a number and takes the not-positive
        # one, so without these the not-a-number message need not name anything.
        ("RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS", "abc", "30"),
        ("RAIL_PROXY_TICKET_TIMEOUT_SECONDS", "abc", "10"),
        ("RAIL_PROXY_MAX_TICKET_LIFETIME_SECONDS", "abc", "no bound"),
    ],
)
def test_a_rejected_setting_names_itself_and_what_it_fell_back_to(
    monkeypatch, caplog, name, value, fallback
):
    """One parser serves three variables, so the name in the message is the only
    thing telling an operator which of them they got wrong."""
    monkeypatch.setenv(name, value)

    with caplog.at_level("WARNING"):
        proxy_module.upstream_timeout()
        proxy_module.ticket_timeout()
        proxy_module.max_ticket_lifetime()

    messages = [r.getMessage() for r in caplog.records]
    assert any(name in m and f"using {fallback}" in m for m in messages), messages


def test_the_filter_rewrites_a_record_rather_than_only_passing_it(monkeypatch, caplog):
    """A filter that is attached and does nothing passes every test that checks
    it is attached. What matters is that httpx's own INFO line — which carries
    the full url, credentials and all, once per request — comes out redacted."""
    import logging

    monkeypatch.setattr(logging.root, "handlers", [], raising=False)
    proxy_module.configure_logging()
    handler = caplog.handler
    handler.addFilter(proxy_module.RedactingFilter())

    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "HTTP Request: GET https://svc:hunter2@rc.invalid/v1/tickets",
        None,
        None,
    )
    handler.handle(record)

    assert "hunter2" not in caplog.text
    assert "rc.invalid" in caplog.text


def test_a_long_rejected_config_entry_is_reported_at_a_bounded_length(
    write_config, caplog
):
    """The entry is operator-authored rather than issuer-controlled, so this
    bounds a log line rather than an attack — but a hand-edited file with a
    pasted blob in it should not produce a message nobody can read."""
    write_config(
        "mcp:\n  servers:\n"
        "    - name: delivery\n      url: http://a.invalid/mcp\n"
        "    - name: payments\n      urls: " + "x" * 5000 + "\n"
    )

    with caplog.at_level("WARNING"):
        proxy_module.load_servers()

    assert caplog.records
    assert all(len(r.getMessage()) < 400 for r in caplog.records)


def test_the_bearer_token_reaches_the_source_it_configures(monkeypatch):
    """Only the token's truthiness is pinned elsewhere, as a side effect of the
    plaintext refusal. A wrong value 401s at Rail Center, holds no ticket, and
    still logs `(bearer)` — the same shape as no wiring at all."""
    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")
    monkeypatch.setenv("RAIL_AUTH_MODE", "bearer")
    monkeypatch.setenv("RAIL_AUTH_TOKEN", "  rc_svc_the_real_one  ")

    assert proxy_module.build_ticket_source().auth_token == "rc_svc_the_real_one"


@pytest.mark.asyncio
async def test_a_failure_with_no_message_is_named_by_its_type(caplog):
    """`asyncio.TimeoutError` renders as the empty string, and the startup fetch
    is exactly where one arrives. Interpolated blindly, the single line this
    fetch exists to produce reads `could not fetch a ticket ()`."""
    import asyncio

    class _Source:
        def describe(self):
            return "https://rc.invalid (unauthenticated)"

        async def fetch(self):
            raise asyncio.TimeoutError

    with caplog.at_level("WARNING"):
        await proxy_module.report_ticket(_Source())

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "could not fetch a ticket (TimeoutError)" in logged


def test_redaction_stays_linear_on_text_it_will_never_match():
    """This filter runs on every record, on the loop that serves every mount,
    and a record can carry text an untrusted sandbox chose — the MCP transport
    logs a rejected `Content-Type` verbatim, on a logger that propagates to
    root. An unbounded scheme run is quadratic on text that never satisfies it,
    and the curve is 4x per doubling, so a header a few times this size is a
    minute of stalled traffic per request.

    A ratio rather than a budget: doubling the input roughly doubles a linear
    match and roughly quadruples a quadratic one, so the shape is what is
    asserted. An absolute threshold would have to hold on a contended 2-vCPU
    runner sharing itself with the lint and image jobs, where the headroom is
    under 2x; a ratio cancels the contention out."""
    import time as _time

    def cost(repeats: int) -> float:
        payload = "a." * repeats + "://x"
        best = float("inf")
        for _ in range(5):
            start = _time.perf_counter()
            for _ in range(10):
                proxy_module._redact(payload)
            best = min(best, _time.perf_counter() - start)
        return best

    ratio = cost(8000) / cost(4000)

    assert ratio < 3.0, f"doubling the input cost {ratio:.1f}x — not linear"


def test_every_credential_in_a_line_is_redacted_not_only_the_first():
    """One record can name two upstreams — the mount loop logs one line each,
    and a failure message can quote both ends of a hop."""
    redacted = proxy_module._redact(
        "http://a:1@one.invalid/mcp -> http://b:2@two.invalid/mcp"
    )

    assert redacted == "http://***@one.invalid/mcp -> http://***@two.invalid/mcp"


def test_the_mount_is_announced_with_its_name_and_its_url(config, caplog):
    """Which upstream was mounted where is the one thing an operator cannot
    recover from the outside: the tools are namespaced, but nothing on the wire
    says which address a namespace resolved to."""
    with caplog.at_level("INFO"):
        proxy_module.build_gateway()

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "mounted 'delivery' -> http://upstream.invalid/mcp" in logged


def test_a_log_line_carries_its_level_and_its_logger(monkeypatch, capsys):
    """Bare messages are unreadable in a container's stdout, where this
    process's lines are interleaved with uvicorn's and httpx's."""
    import logging

    monkeypatch.setattr(logging.root, "handlers", [], raising=False)
    proxy_module.configure_logging()
    logging.getLogger("fastmcp_proxy").warning("a message")

    err = capsys.readouterr().err
    assert "[WARNING] fastmcp_proxy: a message" in err


def test_no_control_plane_is_announced_rather_than_silent(caplog):
    """`ticket_settings()` returning None is the supported open-source state.
    Silence makes it indistinguishable from three mis-spelled variable names."""
    import asyncio

    with caplog.at_level("INFO"):
        asyncio.run(proxy_module.report_ticket(None))

    assert "no Rail Center configured" in caplog.text


def test_redaction_reaches_a_logger_that_never_propagates_to_root():
    """A library that configures its own handler and sets `propagate = False`
    never reaches root, so a filter installed only there never sees it. fastmcp
    does exactly that at import, and its aggregate provider logs an upstream's
    `HTTPStatusError` — url, credential and all — every time a `tools/list` the
    agent asked for fails."""
    import logging

    lines: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    library = logging.getLogger("some-library-that-configures-itself")
    library.propagate = False
    handler = _Collect()
    library.addHandler(handler)
    try:
        proxy_module.configure_logging()
        library.error(
            "Error during list_tools: %s",
            "Client error '401' for url 'https://svc:hunter2@up.invalid/mcp'",
        )
    finally:
        library.removeHandler(handler)

    assert lines
    assert "hunter2" not in "\n".join(lines)
    assert "https://***@up.invalid/mcp" in "\n".join(lines)


def test_a_traceback_is_redacted_along_with_the_message_above_it():
    """`Formatter.format` builds the traceback from `exc_info` after every
    filter has run and appends it, so a credential removed from the message is
    printed in full two lines below. The MCP transport calls `logger.exception`
    on the request path."""
    import logging

    lines: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    logger = logging.getLogger("test-traceback-redaction")
    handler = _Collect()
    handler.addFilter(proxy_module.RedactingFilter())
    logger.addHandler(handler)
    try:
        raise RuntimeError("for url 'https://svc:hunter2@up.invalid/mcp'")
    except RuntimeError:
        logger.exception("Error handling POST request")
    finally:
        logger.removeHandler(handler)

    rendered = "\n".join(lines)
    assert "Traceback (most recent call last)" in rendered
    assert "hunter2" not in rendered
    assert "https://***@up.invalid/mcp" in rendered


def test_a_long_credential_survives_neither_truncation_site(write_config, caplog):
    """`_USERINFO` needs the closing `@` to match, so a cut that lands inside a
    password hands the filter a stump it cannot recognise. Both sites redact
    before they truncate; a JWT-as-url-password is comfortably long enough to
    reach either."""
    secret = "S3CR3T" * 60

    write_config(
        "mcp:\n  servers:\n"
        "    - name: delivery\n      url: http://a.invalid/mcp\n"
        f"    - name: payments\n      urls: https://svc:{secret}@b.invalid/mcp\n"
    )
    with caplog.at_level("WARNING"):
        proxy_module.load_servers()

    assert "S3CR3T" not in caplog.text
    assert "***@b.invalid" in caplog.text


@pytest.mark.asyncio
async def test_a_long_credential_in_a_fetch_failure_is_redacted_before_the_cut(
    caplog,
):
    """The same cut, on the other site: `report_ticket` truncates the exception
    at 300 characters, and an `HTTPStatusError` renders the whole url."""
    secret = "S3CR3T" * 60

    class _Source:
        def describe(self):
            return "https://rc.invalid (unauthenticated)"

        async def fetch(self):
            raise RuntimeError(
                f"Client error '401' for url 'https://svc:{secret}@rc.invalid/v1/tickets'"
            )

    with caplog.at_level("WARNING"):
        await proxy_module.report_ticket(_Source())

    assert "S3CR3T" not in caplog.text
    assert "***@rc.invalid" in caplog.text


def test_the_filter_never_raises_into_the_call_that_logged(monkeypatch):
    """`Handler.handle` guards `emit` and not `filter`, and `callHandlers`
    guards neither — so anything raised here surfaces inside whatever called
    `log.info`, in library code on a request path. This filter sits on every
    handler in the process, so the blast radius is every log call in it."""
    import logging

    monkeypatch.setattr(
        proxy_module,
        "_redact",
        lambda _text: (_ for _ in ()).throw(RuntimeError("redaction broke")),
    )
    emitted: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            emitted.append(record.getMessage())

    logger = logging.getLogger("test-filter-never-raises")
    logger.propagate = False
    handler = _Collect()
    handler.addFilter(proxy_module.RedactingFilter())
    logger.addHandler(handler)
    try:
        logger.error("a url: %s", "https://svc:hunter2@rc.invalid/x")
    finally:
        logger.removeHandler(handler)

    # Withheld rather than dropped or passed on: a record that could not be
    # redacted might be carrying the thing the filter exists to remove.
    assert emitted == ["a log record could not be redacted and was withheld"]


def test_a_traceback_that_holds_a_credential_is_dropped_rather_than_re_rendered():
    """`exc_text` is only read by a formatter that builds its own traceback.
    fastmcp's handler renders from `exc_info` directly, so the record that
    carries a secret loses its traceback — and one that does not, keeps it."""
    import logging

    def record_for(message: str) -> logging.LogRecord:
        try:
            raise RuntimeError(message)
        except RuntimeError:
            return logging.LogRecord(
                "x", logging.ERROR, __file__, 1, "boom", None, sys.exc_info()
            )

    dirty = record_for("for url 'https://svc:hunter2@up.invalid/mcp'")
    clean = record_for("nothing sensitive here")
    proxy_module.RedactingFilter().filter(dirty)
    proxy_module.RedactingFilter().filter(clean)

    assert dirty.exc_info is None
    assert "hunter2" not in dirty.exc_text
    assert "***@up.invalid" in dirty.exc_text
    assert clean.exc_info is not None

    # And an `exc_text` a previous handler already cached. Filters run per
    # handler, so the second one sees a rendered traceback rather than the
    # `exc_info` it was built from.
    cached = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, None)
    cached.exc_text = "Traceback:\n  for url 'https://svc:hunter2@up.invalid/mcp'"
    proxy_module.RedactingFilter().filter(cached)
    assert "hunter2" not in cached.exc_text
    assert "***@up.invalid" in cached.exc_text


@pytest.mark.asyncio
async def test_the_redaction_is_reinstalled_after_uvicorn_makes_its_loggers(
    config, monkeypatch
):
    """`uvicorn.Config` runs `dictConfig`, which creates `uvicorn` and
    `uvicorn.access` with fresh handlers and `propagate = False` — after
    `configure_logging` has walked everything that existed. Their error logger
    is where an ASGI exception's traceback goes."""
    import logging

    import uvicorn

    monkeypatch.setattr(logging.root, "handlers", [], raising=False)
    for name in ("uvicorn", "uvicorn.access"):
        monkeypatch.setattr(logging.getLogger(name), "handlers", [], raising=False)

    class StubServer:
        def __init__(self, config):
            pass

        async def serve(self):
            return None

    monkeypatch.setattr(uvicorn, "Server", StubServer)

    assert await proxy_module.main() == 0

    for name in ("uvicorn", "uvicorn.access"):
        handlers = logging.getLogger(name).handlers
        assert handlers, f"{name} installed no handler"
        assert all(
            any(isinstance(f, proxy_module.RedactingFilter) for f in h.filters)
            for h in handlers
        ), name


def test_a_credential_that_is_not_a_string_is_still_redacted():
    """Every argument is rendered, because the value carrying the credential is
    usually not a `str`. httpx passes an `httpx.URL`, whose `str` is the whole
    url and whose `repr` masks it; library code passes exceptions. A test for
    `isinstance(str)` walks straight past both."""
    import logging

    import httpx

    for template, args in (
        (
            'HTTP Request: %s %s "%s %d %s"',
            (
                "GET",
                httpx.URL("https://svc:hunter2@rc.invalid/v1/tickets"),
                "HTTP/1.1",
                200,
                "OK",
            ),
        ),
        (
            "boom: %s",
            (RuntimeError("for url 'https://svc:hunter2@rc.invalid/x'"),),
        ),
    ):
        record = logging.LogRecord(
            "httpx", logging.INFO, __file__, 1, template, args, None
        )
        proxy_module.RedactingFilter().filter(record)

        assert "hunter2" not in record.getMessage(), template
        assert "***@rc.invalid" in record.getMessage(), template


def test_a_record_that_is_an_exception_rather_than_a_template_is_redacted():
    """`log.error(exc)` puts the exception in `msg`, where an `isinstance(str)`
    test does not see it. `main` logs a `ConfigError` this way, and one can
    quote a url from `bridge.yaml`."""
    import logging

    record = logging.LogRecord(
        "x",
        logging.ERROR,
        __file__,
        1,
        RuntimeError("scheme: 'ftp://svc:hunter2@gateway:8080/mcp'"),
        None,
        None,
    )
    proxy_module.RedactingFilter().filter(record)

    assert "hunter2" not in record.getMessage()


def test_a_record_whose_arguments_are_read_by_a_formatter_keeps_them():
    """Redacted in place, one argument at a time, so `args` keeps its shape.
    uvicorn's access formatter unpacks it into exactly five values — and an
    access line *does* carry userinfo whenever a caller asks for one, because
    the query string goes in undecoded. Replace such a record with a single
    rendered string and the untrusted sandbox drops its own request from the
    access log, and turns one request into fifteen lines of `--- Logging
    error ---`, by choosing a query."""
    import logging
    from collections import defaultdict

    import uvicorn.logging

    access = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.9:5555", "POST", "/mcp?cb=https://a:b@evil.invalid/x", "1.1", 200),
        None,
    )
    mapping = logging.LogRecord(
        "x",
        logging.INFO,
        __file__,
        1,
        "%(a)s/%(b)s",
        defaultdict(lambda: "n/a", {"a": 1}),
        None,
    )

    proxy_module.RedactingFilter().filter(access)
    proxy_module.RedactingFilter().filter(mapping)

    assert len(access.args) == 5
    assert uvicorn.logging.AccessFormatter("%(message)s").format(access) == (
        '10.0.0.9:5555 - "POST /mcp?cb=https://***@evil.invalid/x HTTP/1.1" 200'
    )
    # The same mapping object, not a plain dict rebuilt from it: rebuilding
    # turns a mapping that answers for a missing key into one that raises,
    # which `handleError` then swallows along with the whole line.
    assert isinstance(mapping.args, defaultdict)
    assert mapping.getMessage() == "1/n/a"


def test_a_withheld_record_keeps_no_traceback_either(monkeypatch):
    """The message is replaced because it might hold a credential; the
    traceback it came with might hold the same one."""
    import logging

    monkeypatch.setattr(
        proxy_module,
        "_redact",
        lambda _text: (_ for _ in ()).throw(RuntimeError("redaction broke")),
    )
    try:
        raise RuntimeError("for url 'https://svc:hunter2@rc.invalid/x'")
    except RuntimeError:
        record = logging.LogRecord(
            "x", logging.ERROR, __file__, 1, "boom", None, sys.exc_info()
        )
    record.exc_text = "cached: https://svc:hunter2@rc.invalid/x"

    proxy_module.RedactingFilter().filter(record)

    assert record.exc_info is None
    assert record.exc_text is None


def test_a_second_install_does_not_stack_a_second_filter():
    """`main` calls it twice — once before uvicorn builds its loggers and once
    after. `addFilter` dedupes by equality, which this class does not define."""
    import logging

    handler = logging.StreamHandler()
    proxy_module.logging.root.addHandler(handler)
    try:
        proxy_module._install_redaction()
        proxy_module._install_redaction()
        proxy_module._install_redaction()
        installed = [
            f for f in handler.filters if isinstance(f, proxy_module.RedactingFilter)
        ]
    finally:
        proxy_module.logging.root.removeHandler(handler)

    assert len(installed) == 1


def test_an_argument_that_needs_no_redaction_keeps_its_own_type():
    """Replaced wholesale, a `%d` argument becomes a string and the line
    becomes a `--- Logging error ---`. Only what changed is replaced."""
    import logging

    args = (200, 1.5, None, "plain", b"bytes")
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "%d %s %s %s %s", args, None
    )

    proxy_module.RedactingFilter().filter(record)

    assert record.args == args
    assert all(a is b for a, b in zip(record.args, args))


def test_a_template_that_does_not_match_its_arguments_is_still_redacted():
    """`handleError` prints `Arguments: %s` — the raw tuple — to stderr when the
    formatter fails, so a mismatch is a path where unredacted arguments reach a
    log sink even though no message ever renders."""
    import logging

    record = logging.LogRecord(
        "x",
        logging.INFO,
        __file__,
        1,
        "upstream refused the handshake",
        ("https://svc:hunter2@rc.invalid/v1/tickets",),
        None,
    )

    proxy_module.RedactingFilter().filter(record)

    assert record.args == ("https://***@rc.invalid/v1/tickets",)
