"""The gRPC server Envoy calls on each request, and the process around it.

Envoy's ext_authz filter sends every request it forwards to `Check`, which
answers with the headers to set and remove. It always answers OK: a request is
never refused here, only changed.

The same server answers a status method, for a health probe on the same unix
socket.
"""

import asyncio
import json
import logging
import signal
import traceback
from collections.abc import Mapping
from pathlib import Path

import grpc
from envoy.config.core.v3.base_pb2 import HeaderValue, HeaderValueOption
from envoy.service.auth.v3 import external_auth_pb2, external_auth_pb2_grpc
from google.rpc import code_pb2, status_pb2

from proxy.core.lifecycle import build_holder, health_payload, running
from proxy.core.logs import configure_logging
from proxy.core.settings import ConfigError, plugin_enabled
from proxy.core.xrail_auth import TicketHeaders, TicketHolder, outbound_headers
from proxy.envoy_grpc.decision import Decision, decide
from proxy.envoy_grpc.settings import (
    ProtectedHost,
    get_protected_hosts,
    get_socket_path,
)

log = logging.getLogger(__name__)

# The status method's service and name. Answered with `health_payload` as JSON
# bytes, so there is no .proto for it.
_STATUS_SERVICE = "rail.proxy.v1.Status"
STATUS_METHOD = f"/{_STATUS_SERVICE}/Get"

# How long in-flight checks get to finish once SIGTERM arrives.
_SHUTDOWN_GRACE_SECONDS = 5.0


class _ExtAuthz(external_auth_pb2_grpc.AuthorizationServicer):
    """Answers Envoy's `Check` with the decision for the request."""

    def __init__(
        self,
        holder: TicketHolder | None,
        protected_hosts: Mapping[str, ProtectedHost],
    ) -> None:
        self._holder = holder
        self._protected_hosts = protected_hosts
        self._ticket_headers = TicketHeaders(holder)

    async def Check(self, request, context):
        try:
            return _ok(self._decide(request))
        except Exception as exc:  # noqa: BLE001 - every failure is answered OK
            # Answered OK with no changes, so Envoy forwards the request as it
            # would if this server were down. Logged with the traceback but not
            # the exception's message, which could quote a header value.
            log.error(
                "could not decide on a request (%s); forwarding it unchanged\n%s",
                type(exc).__name__,
                "".join(traceback.format_tb(exc.__traceback__)).rstrip(),
            )
            return _ok(Decision())

    def _decide(self, request) -> Decision:
        http = request.attributes.request.http
        names = list(http.headers) or [h.key for h in http.header_map.headers]
        xrail_headers = outbound_headers(self._holder)
        decision = decide(http.host, names, self._protected_hosts, xrail_headers)
        if decision.headers:
            self._ticket_headers.report(http.host, xrail_headers)
        return decision


def _ok(decision: Decision) -> external_auth_pb2.CheckResponse:
    return external_auth_pb2.CheckResponse(
        status=status_pb2.Status(code=code_pb2.OK),
        ok_response=external_auth_pb2.OkHttpResponse(
            headers=[
                HeaderValueOption(
                    header=HeaderValue(key=name, value=value),
                    append_action=HeaderValueOption.OVERWRITE_IF_EXISTS_OR_ADD,
                )
                for name, value in decision.headers.items()
            ],
            headers_to_remove=sorted(decision.remove),
        ),
    )


def _status_handler(holder: TicketHolder | None, enabled: bool):
    async def get(_request: bytes, _context) -> bytes:
        return json.dumps(health_payload(holder, enabled)).encode()

    # No serializers: the request and response are passed as raw bytes.
    return grpc.method_handlers_generic_handler(
        _STATUS_SERVICE, {"Get": grpc.unary_unary_rpc_method_handler(get)}
    )


def _remove_stale_socket(path: Path) -> None:
    """A socket left by a previous run would stop the bind. Anything else at
    the path is not ours to delete."""
    if path.is_socket():
        path.unlink()
    elif path.exists():
        raise ConfigError(f"RAIL_PROXY_EXT_SOCKET: {path} exists and is not a socket")


async def _start_server(
    holder: TicketHolder | None,
    enabled: bool,
    protected_hosts: Mapping[str, ProtectedHost],
    socket_path: Path,
) -> grpc.aio.Server:
    """Build, bind and start the server. Raises `ConfigError` if the socket
    path can't be used."""
    server = grpc.aio.server()
    external_auth_pb2_grpc.add_AuthorizationServicer_to_server(
        _ExtAuthz(holder, protected_hosts), server
    )
    server.add_generic_rpc_handlers((_status_handler(holder, enabled),))
    try:
        _remove_stale_socket(socket_path)
        server.add_insecure_port(f"unix:{socket_path}")
        # Connecting needs write permission on the socket, so Envoy can connect
        # if it runs as this user or in its group, and nothing else can.
        socket_path.chmod(0o660)
    except RuntimeError as exc:
        raise ConfigError(f"RAIL_PROXY_EXT_SOCKET: cannot bind {socket_path}") from exc
    except OSError as exc:
        # E.g. a directory it can't search, or a stale socket it can't remove.
        raise ConfigError(
            f"RAIL_PROXY_EXT_SOCKET: cannot use {socket_path}: {exc.strerror}"
        ) from exc
    await server.start()
    log.info("serving Envoy's checks on %s", socket_path)
    return server


async def _serve(
    holder: TicketHolder | None,
    enabled: bool,
    protected_hosts: Mapping[str, ProtectedHost],
    socket_path: Path,
) -> None:
    """Serve until SIGTERM or SIGINT, then stop."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    server = await _start_server(holder, enabled, protected_hosts, socket_path)
    await stop.wait()
    log.info("stopping")
    await server.stop(_SHUTDOWN_GRACE_SECONDS)


async def _run(
    holder: TicketHolder | None,
    enabled: bool,
    protected_hosts: Mapping[str, ProtectedHost],
    socket_path: Path,
) -> int:
    # Without waiting for the first fetch: the socket is bound at once, and
    # until the fetch lands every protected request gets
    # `x-rail-status: issuer-unreachable`.
    async with running(holder, wait_for_first_fetch=False):
        try:
            await _serve(holder, enabled, protected_hosts, socket_path)
        except ConfigError as exc:
            log.error("%s", exc)
            return 2
    return 0


def main() -> int:
    configure_logging()
    try:
        enabled = plugin_enabled()
        hosts = get_protected_hosts(plugin_on=enabled)
        holder = build_holder()
        socket_path = get_socket_path()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    return asyncio.run(_run(holder, enabled, hosts, socket_path))
