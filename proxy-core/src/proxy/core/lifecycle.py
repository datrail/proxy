"""The holder's life in a process: built from the settings, run, reported.

Building the holder from the settings, running it for the life of the
process, and reporting its state on a health check.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from proxy.core.settings import build_ticket_source, refresh_seconds
from proxy.core.xrail_auth import TicketHolder

# The name the standalone proxy has always logged under, kept so its output is
# unchanged by the move; renaming it is a change of its own.
log = logging.getLogger("fastmcp_proxy")


def build_holder() -> TicketHolder | None:
    """The holder for this process's ticket, or None where the plugin is off.

    Raises `ConfigError` on a configuration that cannot be served, as
    `build_ticket_source` does.
    """
    source = build_ticket_source()
    if source is None:
        return None
    return TicketHolder(source, refresh_seconds=refresh_seconds())


@asynccontextmanager
async def running(holder: TicketHolder | None) -> AsyncIterator[TicketHolder | None]:
    """Run `holder` for the life of the block, and always close it after.

    The block is entered once the first fetch has an outcome (see
    `TicketHolder.start`).

    A None holder is announced rather than run: attaching nothing is a supported
    state and a surprising one to meet in a log, and silence makes it look like
    a proxy that meant to attach and lost its configuration.

    The close is in a `finally`, so the refresh loop never outlives the block:
    `asyncio.run` would otherwise cancel it during interpreter shutdown, which
    surfaces as a traceback on a clean SIGTERM. `aclose` keeps a cancellation
    aimed at the caller, and says why.
    """
    if holder is None:
        log.info(
            "RAIL_PLUGIN_ENABLED is off — nothing is attached to what is forwarded"
        )
        yield None
        return
    try:
        await holder.start()
        yield holder
    finally:
        await holder.aclose()


def health_payload(holder: TicketHolder | None, enabled: bool) -> dict[str, Any]:
    """What a health check reports: up, the plugin flag, and the holder's state.

    Reported whether or not a ticket is held. Failing closed is a designed
    state and not a fault, so nothing here should make a probe restart the
    process. Never the ticket itself: `TicketHolder.status` leaves it out.
    """
    return {
        "status": "ok",
        "plugin_enabled": enabled,
        "ticket": holder.status if holder is not None else None,
    }
