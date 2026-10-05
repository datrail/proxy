"""What the extension does to one request, decided from its host and headers.

Pure: no I/O, no logging. The gRPC servicer reads a request, calls `decide`,
and turns the result into Envoy's answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from proxy.core.xrail_auth import (
    XRAIL_HEADER,
    XRAIL_UPSTREAM_HEADER,
    OutboundHeaders,
)
from proxy.envoy_grpc.settings import ProtectedHost

# The agent's port that means "no port": the default for the `http` it speaks.
_AGENT_DEFAULT_PORT = 80


@dataclass(frozen=True)
class Decision:
    """The changes to make to one request. All empty for an unprotected host.

    `headers` are set, replacing any value the request has. `remove` are
    removed, and never name a header that `headers` sets, so the order Envoy
    applies the two in doesn't matter.
    """

    headers: Mapping[str, str] = field(default_factory=dict)
    remove: frozenset[str] = frozenset()


def decide(
    request_target: str,
    request_headers: Iterable[str],
    protected_hosts: Mapping[str, ProtectedHost],
    xrail_headers: OutboundHeaders,
) -> Decision:
    """Decide how Envoy should change one request from the agent.

    Arguments:

    - `request_target`: where the agent is sending the request, as host and
      optional port, e.g. `mcp.example.com` or `mcp.example.com:9443`. It is
      the request's `Host` header (`:authority` in Envoy).
    - `request_headers`: the names of the headers the agent sent.
    - `protected_hosts`: the configured protected hosts, by host.
    - `xrail_headers`: the x-rail headers to attach, from the ticket holder:
      `x-rail` with the ticket, `x-rail-status` with the reason there is none,
      or none at all when the plugin is off.

    If the host in `request_target` is not protected, the request is left as
    it is: the result is empty.

    If it is protected, the result:

    - sets `xrail_headers`;
    - removes every other `x-rail` or `x-rail-*` header the agent sent, so the
      only x-rail headers that reach the upstream are the ones set here;
    - sets `x-rail-upstream` to `tls` or `plain`, which tells Envoy whether to
      connect with HTTPS or plain HTTP;
    - sets `:authority` to the host and the port to connect to: the protected
      host's configured port if it has one, otherwise the port in
      `request_target`. `:80`, the agent's HTTP default, counts as no port, so
      Envoy uses the default port for HTTPS or HTTP.

    Every other header the agent sent is left unchanged.

    Raises ValueError if `request_target` can't be parsed.
    """
    host, agent_port = _split_target(request_target)
    protected = protected_hosts.get(host)
    if protected is None:
        return Decision()

    port = protected.port
    if port is None and agent_port != _AGENT_DEFAULT_PORT:
        port = agent_port
    shown = f"[{host}]" if ":" in host else host

    headers = {
        **xrail_headers.headers,
        XRAIL_UPSTREAM_HEADER: "tls" if protected.tls else "plain",
        ":authority": f"{shown}:{port}" if port else shown,
    }
    remove = {name.lower() for name in request_headers if _is_xrail(name.lower())}
    return Decision(headers=headers, remove=frozenset(remove - set(headers)))


def _is_xrail(name: str) -> bool:
    return name == XRAIL_HEADER or name.startswith(XRAIL_HEADER + "-")


def _split_target(target: str) -> tuple[str, int | None]:
    """The host, normalized as protected-host entries are, and the port."""
    parts = urlsplit("//" + target)
    return (parts.hostname or "").removesuffix("."), parts.port
