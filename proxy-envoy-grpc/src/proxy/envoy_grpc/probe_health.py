"""A health probe: ask the server's status method over its unix socket.

    python -m proxy.envoy_grpc.probe_health

Prints the status as JSON and exits 0 when the server answers, or exits 1 when
it doesn't. The server answers whether or not a ticket is held.
"""

from __future__ import annotations

import sys

import grpc

from proxy.envoy_grpc.server import STATUS_METHOD
from proxy.envoy_grpc.settings import get_socket_path

_TIMEOUT_SECONDS = 5.0


def main() -> int:
    try:
        with grpc.insecure_channel(f"unix:{get_socket_path()}") as channel:
            # No serializers: the request and response are raw bytes.
            get = channel.unary_unary(STATUS_METHOD)
            payload = get(b"", timeout=_TIMEOUT_SECONDS)
    except grpc.RpcError as exc:
        print(f"no answer from {get_socket_path()}: {exc.code().name}", file=sys.stderr)
        return 1
    print(payload.decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
