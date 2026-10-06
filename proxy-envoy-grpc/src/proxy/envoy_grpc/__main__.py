"""Run the server: python -m proxy.envoy_grpc

The entry point is this module rather than `server`, so `server` is only ever
imported and its logger is named after it, not `__main__`.
"""

import sys

from proxy.envoy_grpc.server import main

sys.exit(main())
