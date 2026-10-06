"""Tests for the Envoy extension's settings: the protected hosts and the socket
path. The ticket's settings are tested in proxy-core."""

from __future__ import annotations

from pathlib import Path

import pytest

from proxy.core.settings import ConfigError
from proxy.envoy_grpc import settings
from proxy.envoy_grpc.settings import ProtectedHost


def _hosts(monkeypatch, value, *, plugin_on=True):
    monkeypatch.setenv("RAIL_PROXY_PROTECTED_HOSTS", value)
    return settings.get_protected_hosts(plugin_on=plugin_on)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("mcp.example.com", ProtectedHost("mcp.example.com", tls=True, port=None)),
        ("mcp.example.com:8443", ProtectedHost("mcp.example.com", True, 8443)),
        ("https://mcp.example.com", ProtectedHost("mcp.example.com", True, None)),
        ("https://mcp.example.com:9443", ProtectedHost("mcp.example.com", True, 9443)),
        ("http://gw.internal", ProtectedHost("gw.internal", tls=False, port=None)),
        ("http://gw.internal:8080", ProtectedHost("gw.internal", False, 8080)),
        ("HTTP://GW.Internal", ProtectedHost("gw.internal", False, None)),
        ("mcp.example.com.", ProtectedHost("mcp.example.com", True, None)),
        ("10.0.0.7", ProtectedHost("10.0.0.7", True, None)),
        ("[2001:db8::1]:8443", ProtectedHost("2001:db8::1", True, 8443)),
        ("http://[::1]", ProtectedHost("::1", False, None)),
        ("tool_server.internal", ProtectedHost("tool_server.internal", True, None)),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_each_entry_form_is_read(monkeypatch, entry, expected):
    assert _hosts(monkeypatch, entry) == {expected.host: expected}


def test_entries_are_comma_separated_and_blanks_are_skipped(monkeypatch):
    hosts = _hosts(monkeypatch, " a.example , http://b.example:8080,, ")

    assert list(hosts) == ["a.example", "b.example"]


@pytest.mark.parametrize(
    ("entry", "says"),
    [
        ("https://mcp.example.com/mcp", "no path"),
        ("mcp.example.com/", "no path"),
        ("mcp.example.com?x=1", "no path, query or fragment"),
        ("mcp.example.com?", "no path, query or fragment"),
        ("mcp.example.com#frag", "no path, query or fragment"),
        ("ftp://mcp.example.com", "other than http:// or https://"),
        ("mcp.example.com:", "empty or zero port"),
        ("mcp.example.com:0", "empty or zero port"),
        ("mcp.example.com:99999", "cannot be parsed"),
        ("mcp.example.com:https", "cannot be parsed"),
        ("[2001:db8::1", "cannot be parsed"),
        ("https://", "names no host"),
        (":8443", "names no host"),
        ("mcp.example.com other.example", "not a host name"),
        ("mcp..example.com", "not a host name"),
    ],
)
def test_an_entry_that_is_more_or_less_than_a_host_is_refused(monkeypatch, entry, says):
    with pytest.raises(ConfigError, match="RAIL_PROXY_PROTECTED_HOSTS") as info:
        _hosts(monkeypatch, entry)

    assert says in str(info.value)


@pytest.mark.parametrize(
    "entry",
    ["user:secret@[2001:db8::1", "user:secret@mcp.example.com\uff03x"],
    ids=["unclosed-bracket", "nfkc"],
)
def test_an_unparseable_entry_is_not_shown(monkeypatch, entry):
    """The entry may contain a credential, so the error must not repeat it."""
    with pytest.raises(ConfigError, match="cannot be parsed") as info:
        _hosts(monkeypatch, entry)

    assert "secret" not in str(info.value)


@pytest.mark.parametrize(
    "entry",
    ["user:secret@mcp.example.com", "https://token@mcp.example.com:8443"],
    ids=["no-scheme", "with-scheme"],
)
@pytest.mark.parametrize("plugin_on", [True, False], ids=["plugin-on", "plugin-off"])
def test_a_credential_in_an_entry_is_refused_and_never_shown(
    monkeypatch, entry, plugin_on
):
    """The error names the host, never the credential."""
    with pytest.raises(ConfigError) as info:
        _hosts(monkeypatch, entry, plugin_on=plugin_on)

    message = str(info.value)
    assert "credential" in message
    assert "mcp.example.com" in message
    assert "secret" not in message and "token@" not in message
    if plugin_on:
        assert "unset RAIL_PLUGIN_ENABLED" in message


@pytest.mark.parametrize(
    "value",
    [
        "mcp.example.com,mcp.example.com",
        "mcp.example.com,https://MCP.example.com:8443",
        "http://gw.internal,gw.internal.",
    ],
    ids=["same", "other-port", "other-scheme"],
)
def test_one_host_listed_twice_is_refused(monkeypatch, value):
    """Requests are matched on the host alone, so each host has one entry."""
    with pytest.raises(ConfigError, match="listed more than once"):
        _hosts(monkeypatch, value)


@pytest.mark.parametrize("value", [None, "", " , "], ids=["unset", "empty", "blank"])
def test_the_plugin_on_with_no_protected_host_is_refused(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("RAIL_PROXY_PROTECTED_HOSTS", value)

    with pytest.raises(ConfigError, match="at least one host"):
        settings.get_protected_hosts(plugin_on=True)


def test_the_plugin_off_reads_the_list_and_accepts_none(monkeypatch):
    assert settings.get_protected_hosts(plugin_on=False) == {}
    assert list(_hosts(monkeypatch, "mcp.example.com", plugin_on=False)) == [
        "mcp.example.com"
    ]


@pytest.mark.parametrize(
    ("entry", "warned"),
    [
        ("http://gw.internal", True),
        ("http://gw.internal:8080", True),
        ("mcp.example.com", False),
        ("https://mcp.example.com", False),
        ("http://127.0.0.1:8080", False),
        ("http://[::1]", False),
    ],
)
def test_a_plaintext_entry_is_warned_about_with_the_plugin_on(
    monkeypatch, caplog, entry, warned
):
    """A bare host is upgraded to HTTPS, and loopback traffic never leaves the
    machine, so neither is warned about."""
    with caplog.at_level("WARNING"):
        _hosts(monkeypatch, entry)

    plaintext = [r for r in caplog.records if "plaintext http" in r.getMessage()]
    assert bool(plaintext) is warned


def test_no_plaintext_warning_with_the_plugin_off(monkeypatch, caplog):
    with caplog.at_level("WARNING"):
        _hosts(monkeypatch, "http://gw.internal", plugin_on=False)

    assert not caplog.records


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, Path("/run/rail/ext.sock")),
        ("", Path("/run/rail/ext.sock")),
        ("  ", Path("/run/rail/ext.sock")),
        ("/tmp/rail/ext.sock", Path("/tmp/rail/ext.sock")),
    ],
    ids=["unset", "empty", "blank", "set"],
)
def test_the_socket_path(monkeypatch, value, expected):
    """An empty value falls back to the default rather than meaning the current
    directory."""
    if value is not None:
        monkeypatch.setenv("RAIL_PROXY_EXT_SOCKET", value)

    assert settings.get_socket_path() == expected
