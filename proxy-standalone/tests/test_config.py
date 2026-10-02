"""What the proxy does with a configuration it cannot serve.

Every case here ends the same way for an operator — the container stops with a
sentence naming the file and the problem — and the point of the parametrisation
is that a hand-edited YAML file goes wrong in more shapes than an empty one.
"""

from __future__ import annotations

import logging
import pathlib
import re

import pytest

from proxy.core import logs as core_logs
from proxy.core import settings as core_settings
from proxy.standalone import server as proxy_module


async def _hold(source):
    """What `main` does with a source: hold it, and fetch once.

    `TicketHolder.start()` also launches the refresh loop, which a test would
    then have to cancel; `refresh_once` is the half these assertions are about.
    """
    holder = proxy_module.TicketHolder(source)
    await holder.refresh_once()
    return holder


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
def test_a_blank_config_path_falls_back_to_the_default(monkeypatch, value):
    """An unset compose interpolation yields an empty string, and `Path("")` is
    the current directory — which exists, so a naive check passes and the read
    fails on a directory instead of reporting a missing config. Whitespace is
    the same mistake with a space in it."""
    monkeypatch.setenv("RAIL_PROXY_CONFIG_FILE", value)

    # Compared against the path itself rather than against the constant the
    # function returns, which would hold however the constant was defined.
    # Relative, so the image's WORKDIR makes it /app/standalone/bridge.yaml.
    assert proxy_module.config_file() == pathlib.Path("standalone/bridge.yaml")


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


def test_an_unparseable_url_is_reported_rather_than_raised_from_the_build(
    write_config,
):
    """`http://[::1:8080/mcp` passes the scheme check and dies in `urlsplit`,
    which `build_gateway` calls outside any handler — a traceback and exit 1
    where the file's other mistakes give a sentence and exit 2."""
    write_config(
        'mcp:\n  servers:\n    - name: delivery\n      url: "http://[::1:8080/mcp"\n'
    )

    with pytest.raises(proxy_module.ConfigError, match="cannot be parsed"):
        proxy_module.load_servers()

    with pytest.raises(proxy_module.ConfigError, match="cannot be parsed"):
        proxy_module.build_app(None)


def test_an_unparseable_url_never_echoes_its_credential(write_config):
    """The refusal renders the url, and a url carries a password. `urlsplit`'s
    own message would too: it puts the netloc in it for a value that fails NFKC
    normalisation."""
    write_config(
        "mcp:\n  servers:\n    - name: paid\n"
        '      url: "http://svc:s3cret@[::1:8080/mcp"\n'
    )

    with pytest.raises(proxy_module.ConfigError) as info:
        proxy_module.load_servers()

    assert "s3cret" not in str(info.value)


def test_a_python_tag_is_refused_rather_than_constructed(write_config):
    """`safe_load` is the one property that makes reading an operator-supplied
    file safe. `yaml.load` and `yaml.unsafe_load` construct arbitrary Python
    from a `!!python/...` tag, so anyone who can write this file — or mount it
    — runs code in this process, and every other assertion here stays green.

    The tag resolves to a function rather than calling one: a test that pins
    arbitrary execution must not perform it to find out."""
    write_config(
        "mcp:\n  servers:\n"
        "    - name: delivery\n      url: http://upstream.invalid/mcp\n"
        "pwn: !!python/name:os.system\n"
    )

    with pytest.raises(proxy_module.ConfigError, match="not valid YAML"):
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


@pytest.mark.asyncio
async def test_an_unusable_port_is_reported_rather_than_crashing(config, monkeypatch):
    """Read where it can be reported, not at import — a module that cannot be
    imported has nowhere to say why."""
    monkeypatch.setenv("RAIL_PROXY_PORT", "8091x")

    assert await proxy_module.main() == 2


def test_every_resolvable_level_is_one_uvicorn_can_use():
    """The guard this pins is that the two vocabularies stay reconciled: uvicorn
    is the narrower, and nothing else in the suite exercises its leg."""
    import uvicorn.config

    for level in core_logs._LEVELS:
        assert level.lower() in uvicorn.config.LOG_LEVELS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "port", ["0", "-1", "70000"], ids=["zero", "negative", "too-big"]
)
async def test_a_port_outside_the_range_is_reported(config, monkeypatch, port):
    """Every one of these is reported rather than left to `bind()`.

    The server is stubbed even though this test expects never to reach it: `0`
    binds an ephemeral port, so a regression here would start a real listener
    and serve for ever — a hung CI job rather than a red one.
    """
    import uvicorn

    class _NeverStarted:
        def __init__(self, config):
            raise AssertionError("the port check let a listener through")

        async def serve(self):  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(uvicorn, "Server", _NeverStarted)
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


@pytest.mark.asyncio
async def test_the_settings_reach_uvicorn(config, monkeypatch):
    """Each accessor is pinned above; this pins that `main` uses them. A call
    site reading the environment raw — or passing a literal — leaves every one
    of those assertions green, because none of them touches `main`.

    The log level in particular: uvicorn installs its own loggers, so a level
    that does not reach this config leaves the access log and the startup lines
    at INFO whatever the variable said.

    `proxy_headers` is not an accessor but belongs here for the same reason: it
    defaults on, `forwarded_allow_ips` defaults to 127.0.0.1 — which in a
    sidecar is the sandbox — and nothing terminates TLS in front of this
    listener. Left on, the agent chooses the client address and scheme in this
    process's own access log by sending `X-Forwarded-For`.
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
                proxy_headers=config.proxy_headers,
                forwarded_allow_ips=config.forwarded_allow_ips,
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
    assert captured["proxy_headers"] is False
    assert "*" not in captured["forwarded_allow_ips"]
    # Installed by `main`, not left over from a test that called
    # `configure_logging` itself — hence the handlers cleared above. Without
    # this the container runs with no redaction at all, and httpx puts the whole
    # url on stdout once per request.
    assert any(
        isinstance(f, core_logs.RedactingFilter)
        for h in logging.root.handlers
        for f in h.filters
    )


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
    assert proxy_module.redact_credentials(message) == expected


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
    like any other: a sentence and exit 2, not a traceback. Fetching is
    deliberately never fatal, so the source is built where the other config
    errors are caught rather than inside the thing that fetches."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert await proxy_module.main() == 2


@pytest.mark.asyncio
async def test_an_expired_ticket_is_not_reported_as_held(monkeypatch, caplog):
    """An expired ticket is a well-formed answer, so it arrives on the success
    path. Announcing it as held would report a dead identity as a healthy one,
    in the one log line this fetch exists to produce."""
    import httpx

    from proxy.core.xrail_auth import TicketSource

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
        await _hold(source)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "already-expired" in logged
    assert "expires in" not in logged
    # A positive number of seconds. Rendered from `remaining()` unnegated, the
    # line reads "expired -3600s ago".
    assert re.search(r"expired (\d+)s ago", logged)


@pytest.mark.asyncio
async def test_startup_fetches_the_ticket_rather_than_only_building_the_source(
    config, upstream, monkeypatch, caplog
):
    """The holder tested directly says nothing about whether `main` starts it.
    Disconnected, every other test still passes and the feature is gone."""
    import httpx

    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", "true")
    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")
    real = core_settings.TicketSource

    def recording(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"host_id": "h", "tickets": []})
        )
        return real(*args, **kwargs)

    # Where `build_ticket_source` looks it up: patched on this module instead,
    # the fake is never built and the fetch goes to rc.invalid.
    monkeypatch.setattr(core_settings, "TicketSource", recording)
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
    # generic failure handler it reads as "ticket refresh failed", which is the
    # opposite thing: an issuer that is down, rather than one saying this proxy
    # has no identity.
    assert "Rail Center holds no ticket" in logged
    # The auth state, which is `describe()`'s and not the raw url's. It is the
    # only thing in this line telling an operator how the proxy authenticated.
    assert "(unauthenticated)" in logged
    # Started, and started *after* the fetch — which is the whole reason the
    # fetch has a timeout of its own rather than borrowing the upstream one.
    # Moved below `serve()`, every assertion above still holds, because the
    # stub returns at once and production would not.
    assert served[1:] == [True], "the fetch did not precede the listener"


@pytest.mark.parametrize(
    ("name", "value", "fallback"),
    [
        ("RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS", "0", "30"),
        # The other warning branch. `0` is a number and takes the not-positive
        # one, so without this the not-a-number message need not name anything.
        ("RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS", "abc", "30"),
    ],
)
def test_a_rejected_setting_names_itself_and_what_it_fell_back_to(
    monkeypatch, caplog, name, value, fallback
):
    """One parser serves several variables, so the name in the message is the
    only thing telling an operator which of them they got wrong. The ticket's
    are checked in proxy-core; this is the one standalone adds."""
    monkeypatch.setenv(name, value)

    with caplog.at_level("WARNING"):
        proxy_module.upstream_timeout()

    messages = [r.getMessage() for r in caplog.records]
    assert any(name in m and f"using {fallback}" in m for m in messages), messages


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


@pytest.mark.asyncio
async def test_a_failure_with_no_message_is_named_by_its_type(caplog):
    """`asyncio.TimeoutError` renders as the empty string, and a fetch bounded
    by a deadline is exactly where one arrives. Interpolated blindly, the line
    reads `ticket refresh failed ()`."""

    class _Source:
        def describe(self):
            return "https://rc.invalid (unauthenticated)"

        async def fetch(self):
            raise TimeoutError

    with caplog.at_level("WARNING"):
        await _hold(_Source())

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "ticket refresh failed (TimeoutError)" in logged


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
                proxy_module.redact_credentials(payload)
            best = min(best, _time.perf_counter() - start)
        return best

    ratio = cost(8000) / cost(4000)

    assert ratio < 3.0, f"doubling the input cost {ratio:.1f}x — not linear"


def test_every_credential_in_a_line_is_redacted_not_only_the_first():
    """One record can name two upstreams — the mount loop logs one line each,
    and a failure message can quote both ends of a hop."""
    redacted = proxy_module.redact_credentials(
        "http://a:1@one.invalid/mcp -> http://b:2@two.invalid/mcp"
    )

    assert redacted == "http://***@one.invalid/mcp -> http://***@two.invalid/mcp"


def test_the_mount_is_announced_with_its_name_and_its_url(config, caplog):
    """Which upstream was mounted where is the one thing an operator cannot
    recover from the outside: the tools are namespaced, but nothing on the wire
    says which address a namespace resolved to."""
    with caplog.at_level("INFO"):
        proxy_module.build_gateway(None)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "mounted 'delivery' -> http://upstream.invalid/mcp" in logged


@pytest.mark.asyncio
async def test_pass_through_is_announced_rather_than_silent(
    config, upstream, monkeypatch, caplog
):
    """Attaching nothing is a supported state and a surprising one to meet in a
    log. Silence makes it indistinguishable from a proxy that meant to attach
    and lost its configuration."""
    import uvicorn

    class _Server:
        def __init__(self, config):
            pass

        async def serve(self):
            return None

    monkeypatch.setattr(uvicorn, "Server", _Server)

    with caplog.at_level("INFO"):
        assert await proxy_module.main() == 0

    assert "RAIL_PLUGIN_ENABLED is off" in caplog.text


def test_a_long_credential_survives_neither_truncation_site(write_config, caplog):
    """Both sites redact before they truncate, for the reason
    `TicketHolder.refresh_once` gives. A JWT-as-url-password is comfortably
    long enough to reach either."""
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
    """The same cut, on the other site: a refresh failure truncates the
    exception at 300 characters, and an `HTTPStatusError` renders the whole
    url."""
    secret = "S3CR3T" * 60

    class _Source:
        def describe(self):
            return "https://rc.invalid (unauthenticated)"

        async def fetch(self):
            raise RuntimeError(
                f"Client error '401' for url 'https://svc:{secret}@rc.invalid/v1/tickets'"
            )

    with caplog.at_level("WARNING"):
        await _hold(_Source())

    assert "S3CR3T" not in caplog.text
    assert "***@rc.invalid" in caplog.text


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
            any(isinstance(f, core_logs.RedactingFilter) for f in h.filters)
            for h in handlers
        ), name


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

    core_logs.RedactingFilter().filter(access)
    core_logs.RedactingFilter().filter(mapping)

    assert len(access.args) == 5
    assert uvicorn.logging.AccessFormatter("%(message)s").format(access) == (
        '10.0.0.9:5555 - "POST /mcp?cb=https://***@evil.invalid/x HTTP/1.1" 200'
    )
    # The same mapping object, not a plain dict rebuilt from it: rebuilding
    # turns a mapping that answers for a missing key into one that raises,
    # which `handleError` then swallows along with the whole line.
    assert isinstance(mapping.args, defaultdict)
    assert mapping.getMessage() == "1/n/a"


# ─────────────────────────────────────────────────────────────────────
#  RAIL_PLUGIN_ENABLED
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_refresh_interval_reaches_the_holder(config, upstream, monkeypatch):
    """Parsed correctly and never passed on is the same as not having it."""
    import httpx

    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", "true")
    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")
    monkeypatch.setenv("RAIL_PROXY_REFRESH_SECONDS", "42")

    real = core_settings.TicketSource
    built: list = []
    monkeypatch.setattr(
        core_settings,
        "TicketSource",
        lambda *a, **k: (
            built.append(None)
            or real(
                *a,
                **{
                    **k,
                    "transport": httpx.MockTransport(lambda _r: httpx.Response(500)),
                },
            )
        ),
    )
    held: list = []

    class _Server:
        def __init__(self, config):
            pass

        async def serve(self):
            return None

    monkeypatch.setattr("uvicorn.Server", _Server)
    real_holder = proxy_module.TicketHolder

    def capture(*a, **k):
        holder = real_holder(*a, **k)
        held.append(holder)
        return holder

    monkeypatch.setattr(proxy_module, "TicketHolder", capture)

    assert await proxy_module.main() == 0
    assert built, "the stubbed source was never built; the fetch went elsewhere"
    assert held and held[0].refresh_seconds == 42.0


@pytest.mark.asyncio
async def test_the_refresh_loop_does_not_outlive_the_server(
    config, upstream, monkeypatch
):
    """`asyncio.run` cancels a surviving task during interpreter shutdown, and
    that surfaces as a traceback on an otherwise clean SIGTERM."""
    import httpx

    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", "true")
    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")

    real = core_settings.TicketSource
    built: list = []
    monkeypatch.setattr(
        core_settings,
        "TicketSource",
        lambda *a, **k: (
            built.append(None)
            or real(
                *a,
                **{
                    **k,
                    "transport": httpx.MockTransport(lambda _r: httpx.Response(500)),
                },
            )
        ),
    )
    held: list = []
    real_holder = proxy_module.TicketHolder
    monkeypatch.setattr(
        proxy_module,
        "TicketHolder",
        lambda *a, **k: held.append(real_holder(*a, **k)) or held[-1],
    )

    class _Server:
        def __init__(self, config):
            pass

        async def serve(self):
            return None

    monkeypatch.setattr("uvicorn.Server", _Server)

    # Bounded: `main` closes the holder in a `finally`, and a close that does
    # not cancel waits on a loop that never ends. Unbounded, a regression here
    # hangs the CI job instead of failing it.
    import asyncio

    assert await asyncio.wait_for(proxy_module.main(), 10) == 0
    assert built, "the stubbed source was never built; the fetch went elsewhere"
    assert held[0]._task is None, "the refresh loop was left running"


def test_an_unread_upstream_key_is_announced_rather_than_refused(write_config, caplog):
    """`transport: streamable_http` names the only transport this proxy speaks,
    so refusing to start over it is a worse outcome than the silence the
    warning replaces — but `headers:` is the same shape and does lose a
    credential, so it is said out loud."""
    write_config(
        "mcp:\n  servers:\n"
        "    - name: delivery\n      url: http://gateway.invalid:8080/mcp\n"
        "      transport: streamable_http\n"
        "      headers:\n        Authorization: Bearer upstream-key\n"
    )

    with caplog.at_level("WARNING"):
        servers = proxy_module.load_servers()

    assert [s["name"] for s in servers] == ["delivery"]
    warned = [
        r.getMessage() for r in caplog.records if "does not read" in r.getMessage()
    ]
    assert len(warned) == 1
    assert "`headers`" in warned[0]
    assert "`transport`" in warned[0]


# ─────────────────────────────────────────────────────────────────────
#  schema_version
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ['"1.0"', '"1.7"', '"1.0.0"', "1.0", "1"], ids=str)
def test_any_minor_of_the_major_this_proxy_reads_is_served(write_config, caplog, value):
    """The major is what is compared. A minor bump is a compatible addition, so
    an older proxy reads the file and warns about the keys it does not know —
    which is the machinery `headers:` already exercises. Quoting is the
    operator's choice and not a version: `1.0` unquoted arrives as a float and
    `1` as an int, and both are what they meant."""
    write_config(
        f"schema_version: {value}\n"
        "mcp:\n  servers:\n    - name: delivery\n      url: http://a.invalid/mcp\n"
    )

    with caplog.at_level("WARNING"):
        servers = proxy_module.load_servers()

    assert [s["name"] for s in servers] == ["delivery"]
    assert not [r for r in caplog.records if "schema_version" in r.getMessage()]


@pytest.mark.parametrize(
    "value",
    ['"2.0"', '"0.9"', '"one"', '"1.x"', '""', '"1²"', '"١.0"'],
    ids=str,
)
def test_a_version_this_proxy_cannot_read_refuses_to_start(write_config, value):
    """Reported and refused rather than half-read. A file written for a reader
    this is not is not a file to serve the recognised parts of — that is the
    silent drift the field exists to end, and it is read at startup where an
    operator sees the refusal rather than discovering it.

    The last two are the digits that are not ASCII: a superscript, which `int`
    rejects, and another script's decimal one, which `int` reads as a 1. Both
    arrive here as a refusal naming the version rather than as a traceback or
    as a served file."""
    write_config(
        f"schema_version: {value}\n"
        "mcp:\n  servers:\n    - name: delivery\n      url: http://a.invalid/mcp\n"
    )

    with pytest.raises(proxy_module.ConfigError, match="schema_version"):
        proxy_module.load_servers()


def test_a_file_written_before_the_field_existed_is_read_and_warned_about(
    write_config, caplog
):
    """Every file written before the field existed omits it, and stopping those
    is a cost with nothing bought: a file with no version is a file with no
    field this proxy is missing. The warning is what gets the line added before
    the format does move."""
    write_config(
        "mcp:\n  servers:\n    - name: delivery\n      url: http://a.invalid/mcp\n"
    )

    with caplog.at_level("WARNING"):
        servers = proxy_module.load_servers()

    assert [s["name"] for s in servers] == ["delivery"]
    assert "no schema_version" in caplog.text


def test_the_version_is_settled_before_the_upstreams_are_parsed(write_config):
    """A loader that read `mcp.servers` first would report the shape it assumed
    rather than the version that told it not to assume one — so a 2.x file whose
    upstream list also moved reports the list, and an operator fixes the wrong
    thing."""
    write_config('schema_version: "2.0"\nmcp: not-a-mapping\n')

    with pytest.raises(proxy_module.ConfigError, match="schema_version"):
        proxy_module.load_servers()


#: The config files this repository ships, each of which some instruction tells
#: someone to run: the README's quick start copies the example and mounts it,
#: and the e2e stack mounts its own.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SHIPPED_CONFIGS = (
    _REPO_ROOT / "proxy-standalone/bridge.yaml.example",
    _REPO_ROOT / "e2e" / "bridge.yaml",
)


@pytest.mark.parametrize(
    "path", SHIPPED_CONFIGS, ids=lambda p: str(p.relative_to(_REPO_ROOT))
)
def test_a_config_this_repository_ships_is_one_this_proxy_reads(
    monkeypatch, caplog, path
):
    """The version line is startup-fatal, and these are the two files an
    instruction hands someone: the README's quick start copies the example and
    mounts it, and `e2e/compose.yml` mounts the other. Wrong or missing, the
    first is a container that stops and the second is a warning nobody reads —
    neither of which any other test sees, because nothing else loads a file that
    is not written by the test itself."""
    monkeypatch.setenv("RAIL_PROXY_CONFIG_FILE", str(path))

    with caplog.at_level("WARNING"):
        servers = proxy_module.load_servers()

    assert [s["name"] for s in servers]
    assert not [r for r in caplog.records if "schema_version" in r.getMessage()]
