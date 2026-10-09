"""What the Envoy extension reads from the environment besides the ticket.

The ticket's settings are `proxy.core.settings`, shared with every interface.
Here: which hosts are protected and how to reach them, and the socket Envoy
calls.
"""

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from proxy.core.settings import ConfigError, warn_if_in_the_clear

_PROTECTED_HOSTS = "RAIL_PROXY_PROTECTED_HOSTS"
_DEFAULT_SOCKET = Path("/run/rail/ext.sock")

_ENTRY_FORMAT = "[http(s)://]host[:port]"

_HOSTNAME = re.compile(r"[a-z0-9_-]+(\.[a-z0-9_-]+)*")


@dataclass(frozen=True)
class ProtectedHost:
    """One entry of RAIL_PROXY_PROTECTED_HOSTS.

    `host` is normalized as an agent's `:authority` will be, and is what a
    request is matched on. `tls` and `port` only say where a matched request
    goes: HTTPS unless the entry says `http://`, and the entry's port if it
    names one, else the agent's.
    """

    host: str
    tls: bool
    port: int | None


def _normalize_host(host: str) -> str:
    """Lowercase, without one trailing dot or IPv6 brackets."""
    host = host.lower().removeprefix("[").removesuffix("]")
    return host.removesuffix(".")


def _refuse(entry: str, why: str) -> ConfigError:
    return _refuse_without_entry(f"{entry!r} {why}")


def _refuse_without_entry(message: str) -> ConfigError:
    return ConfigError(f"{_PROTECTED_HOSTS}: {message}; an entry is {_ENTRY_FORMAT}")


def _parse(entry: str, plugin_on: bool) -> ProtectedHost:
    try:
        parts = urlsplit(entry if "://" in entry else "//" + entry)
    except ValueError:
        # Don't include the entry or the original error message: either might
        # contain a credential, since some of urlsplit's errors quote the netloc.
        raise _refuse_without_entry("an entry cannot be parsed") from None

    if parts.username or parts.password:
        raise _refuse_without_entry(
            f"the entry for {parts.hostname or ''!r} carries a credential"
        )

    if parts.scheme not in ("", "http", "https"):
        raise _refuse(entry, "has a scheme other than http:// or https://")
    try:
        port = parts.port
    except ValueError as exc:
        raise _refuse(entry, f"cannot be parsed: {exc}") from None

    if parts.path or "?" in entry or "#" in entry:
        raise _refuse(entry, "names more than a host: no path, query or fragment")
    if parts.netloc.endswith(":") or port == 0:
        raise _refuse(entry, "has an empty or zero port")

    host = _normalize_host(parts.hostname or "")
    if not host:
        raise _refuse(entry, "names no host")
    if not _is_ip(host) and not _HOSTNAME.fullmatch(host):
        raise _refuse(entry, "is not a host name or an IP address")

    tls = parts.scheme != "http"
    if plugin_on:
        warn_if_in_the_clear(host, f"{'https' if tls else 'http'}://{parts.netloc}")
    return ProtectedHost(host=host, tls=tls, port=port)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _get_comma_separated_env(name: str) -> list[str]:
    """The non-blank items of a comma-separated variable, stripped."""
    return [
        item.strip() for item in os.environ.get(name, "").split(",") if item.strip()
    ]


def get_protected_hosts(*, plugin_on: bool) -> dict[str, ProtectedHost]:
    """The protected hosts, by the normalized host a request is matched on."""
    hosts: dict[str, ProtectedHost] = {}
    for entry in _get_comma_separated_env(_PROTECTED_HOSTS):
        parsed = _parse(entry, plugin_on)
        # Matching ignores scheme and port, so two entries for one host would
        # leave a request with two places to go.
        if parsed.host in hosts:
            raise ConfigError(
                f"{_PROTECTED_HOSTS}: {parsed.host!r} is listed more than once; "
                "one entry per host"
            )
        hosts[parsed.host] = parsed

    # When the plugin is on, at least one is needed.
    if plugin_on and not hosts:
        raise ConfigError(
            f"RAIL_PLUGIN_ENABLED=true attaches an identity, so {_PROTECTED_HOSTS} "
            "must name at least one host to send it to"
        )
    return hosts


def get_socket_path() -> Path:
    """The unix socket Envoy calls."""
    raw = os.environ.get("RAIL_PROXY_EXT_SOCKET", "").strip()
    return Path(raw) if raw else _DEFAULT_SOCKET
