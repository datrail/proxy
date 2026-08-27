"""What the proxy does with a configuration it cannot serve.

Every case here ends the same way for an operator — the container stops with a
sentence naming the file and the problem — and the point of the parametrisation
is that a hand-edited YAML file goes wrong in more shapes than an empty one.
"""

from __future__ import annotations

import pytest

from fastmcp_proxy import proxy as proxy_module


def test_a_missing_config_file_is_reported_by_path(write_config, monkeypatch, tmp_path):
    """The image bakes RAIL_PROXY_CONFIG_FILE to a path holding no file, so a
    container started without a mounted config lands here."""
    monkeypatch.setenv("RAIL_PROXY_CONFIG_FILE", str(tmp_path / "absent.yaml"))

    with pytest.raises(proxy_module.ConfigError, match="cannot read"):
        proxy_module.load_servers()


def test_an_unset_config_path_falls_back_rather_than_reading_the_directory(monkeypatch):
    """An unset compose interpolation yields an empty string, and `Path("")` is
    the current directory — which exists, so a naive check passes and the read
    fails on a directory instead of reporting a missing config."""
    monkeypatch.setenv("RAIL_PROXY_CONFIG_FILE", "")

    assert proxy_module.config_file() == proxy_module.DEFAULT_CONFIG_FILE


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
