"""What the proxy does with a configuration it cannot serve.

Every case here ends the same way for an operator — the container stops with a
sentence naming the file and the problem — and the point of the parametrisation
is that a hand-edited YAML file goes wrong in more shapes than an empty one.
"""

from __future__ import annotations

import pathlib

import pytest

from fastmcp_proxy import proxy as proxy_module


def test_a_missing_config_file_is_reported_by_path(write_config, monkeypatch, tmp_path):
    """The image bakes RAIL_PROXY_CONFIG_FILE to a path holding no file, so a
    container started without a mounted config lands here."""
    monkeypatch.setenv("RAIL_PROXY_CONFIG_FILE", str(tmp_path / "absent.yaml"))

    with pytest.raises(proxy_module.ConfigError, match="cannot read"):
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
    """Each of these used to reach `.get` on a list or a parser error and leave
    a traceback: a container crash-looping on a stack trace rather than stopping
    with the reason."""
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
    """It used to be parsed at import, which made the module unimportable."""
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
    """uvicorn resolves a level by dict lookup and raises KeyError on a miss.
    `logging` accepts WARN, FATAL and NOTSET, so validating against `logging`
    alone let those through and killed the server after startup had begun."""
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
    """bind() raises OverflowError past every handler; 0 binds an ephemeral port,
    so the process comes up somewhere nothing is configured to look."""
    monkeypatch.setenv("RAIL_PROXY_PORT", port)

    assert await proxy_module.main() == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", 30.0), ("5", 5.0), ("2.5", 2.5), ("0", 30.0), ("-1", 30.0), ("abc", 30.0)],
    ids=["unset", "integer", "fractional", "zero", "negative", "junk"],
)
def test_the_upstream_timeout_falls_back_rather_than_disabling_itself(
    monkeypatch, value, expected
):
    """Zero and negative are refused rather than honoured: a timeout of none is
    how a hung upstream becomes an agent waiting forever, which is the state
    this setting exists to bound."""
    monkeypatch.setenv("RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS", value)

    assert proxy_module.upstream_timeout() == expected
