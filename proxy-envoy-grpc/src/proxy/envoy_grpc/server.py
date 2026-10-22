"""The gRPC server Envoy calls on each request, and the process around it.

It serves ext_authz's `Check` (`ext_authz.py`) and ext_proc's `Process`
(`ext_proc.py`), both always: Envoy names only the cluster, and the method
picks the servicer. The same server answers a status method, for a health
probe on the same unix socket.
"""

import asyncio
import json
import logging
import signal
from collections.abc import Mapping
from pathlib import Path

import grpc
from envoy.service.auth.v3 import external_auth_pb2_grpc
from envoy.service.ext_proc.v3 import external_processor_pb2_grpc

from proxy.core.body import get_body_limits
from proxy.core.judge import get_judge_failure_verdict
from proxy.core.lifecycle import build_holder, health_payload, running
from proxy.core.logs import configure_logging
from proxy.core.settings import ConfigError, plugin_enabled
from proxy.core.xrail_auth import TicketHolder
from proxy.envoy_grpc.ext_authz import ExtAuthz
from proxy.envoy_grpc.ext_proc import ExtProc
from proxy.envoy_grpc.outcome import Judging, Pipeline
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

# Room past the body cap for the rest of a message: headers and framing.
_MESSAGE_HEADROOM_BYTES = 1024 * 1024


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
    judging: Judging | None = None,
) -> grpc.aio.Server:
    """Build, bind and start the server. Raises `ConfigError` if the socket
    path can't be used. `judging` is `Judging`'s defaults unless given."""
    judging = judging or Judging()
    # A body comes in one message from ext_authz, and from ext_proc's buffered
    # modes, so a message may be as large as the cap.
    server = grpc.aio.server(
        options=[
            (
                "grpc.max_receive_message_length",
                judging.body_limits.max_bytes + _MESSAGE_HEADROOM_BYTES,
            )
        ]
    )
    pipeline = Pipeline(holder, protected_hosts, judging)
    external_auth_pb2_grpc.add_AuthorizationServicer_to_server(
        ExtAuthz(pipeline), server
    )
    external_processor_pb2_grpc.add_ExternalProcessorServicer_to_server(
        ExtProc(pipeline), server
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
    log.info("serving Envoy's callouts on %s", socket_path)
    return server


async def _serve(
    holder: TicketHolder | None,
    enabled: bool,
    protected_hosts: Mapping[str, ProtectedHost],
    socket_path: Path,
    judging: Judging,
) -> None:
    """Serve until SIGTERM or SIGINT, then stop."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    server = await _start_server(holder, enabled, protected_hosts, socket_path, judging)
    await stop.wait()
    log.info("stopping")
    await server.stop(_SHUTDOWN_GRACE_SECONDS)


async def _run(
    holder: TicketHolder | None,
    enabled: bool,
    protected_hosts: Mapping[str, ProtectedHost],
    socket_path: Path,
    judging: Judging,
) -> int:
    # Without waiting for the first fetch: the socket is bound at once, and
    # until the fetch lands every protected request gets
    # `x-rail-status: issuer-unreachable`.
    async with running(holder, wait_for_first_fetch=False):
        try:
            await _serve(holder, enabled, protected_hosts, socket_path, judging)
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
        judging = Judging(
            on_failure=get_judge_failure_verdict(), body_limits=get_body_limits()
        )
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    return asyncio.run(_run(holder, enabled, hosts, socket_path, judging))
