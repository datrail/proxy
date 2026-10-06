"""Run the proxy: python -m proxy.standalone

The entry point is this module rather than `server`, so `server` is only ever
imported and its logger is named after it, not `__main__`.
"""

import asyncio
import sys

from proxy.standalone.server import main

sys.exit(asyncio.run(main()))
