"""Building, running and reporting the holder, as every interface does.

Whether startup waits for the first fetch is the one thing interfaces choose,
so each behaviour here is checked in both modes where it applies.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core_support import wound_holder
from proxy.core import lifecycle
from proxy.core.xrail_auth import TicketHolder, Token


class _GatedSource:
    """A source whose fetch answers only once the test opens the gate."""

    def __init__(self) -> None:
        self.calls = 0
        self.gate = asyncio.Event()

    def describe(self) -> str:
        return "https://rc.invalid/v1/tickets (unauthenticated)"

    async def fetch(self) -> Token:
        self.calls += 1
        await self.gate.wait()
        return Token("the-ticket", time.time() + 3600)


async def _until(predicate, timeout: float = 5.0) -> None:
    """Yield to the loop until `predicate()` holds, bounded so a regression
    fails the test rather than hanging it."""

    async def poll():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout)


def test_a_holder_that_has_never_had_an_answer_is_issuer_unreachable():
    """What an interface that does not wait reports until the first fetch
    lands, so it is pinned rather than assumed."""
    holder = TicketHolder(_GatedSource())

    assert holder.snapshot() == (None, "issuer-unreachable")


@pytest.mark.asyncio
async def test_waiting_enters_the_block_once_the_first_fetch_has_landed():
    source = _GatedSource()
    source.gate.set()
    holder = TicketHolder(source)

    async with lifecycle.running(holder, wait_for_first_fetch=True):
        assert source.calls == 1
        assert holder.snapshot() == ("the-ticket", None)

    assert holder._task is None, "the refresh loop outlived the block"


@pytest.mark.asyncio
async def test_not_waiting_enters_at_once_and_the_ticket_follows(caplog):
    source = _GatedSource()
    holder = TicketHolder(source)

    async def serve():
        async with lifecycle.running(holder, wait_for_first_fetch=False):
            # Entered with the fetch not yet answered: Rail Center's state is
            # unknown, which is what `issuer-unreachable` says.
            assert holder.snapshot() == (None, "issuer-unreachable")
            # The first fetch starts without the loop's usual wait.
            await _until(lambda: source.calls == 1)
            assert holder.snapshot() == (None, "issuer-unreachable")

            source.gate.set()
            await _until(lambda: holder.snapshot()[0] is not None)
            assert holder.snapshot() == ("the-ticket", None)

    # Bounded: a start that waited after all would never enter the block, since
    # the gate opens only inside it, and should fail rather than hang.
    with caplog.at_level("INFO"):
        await asyncio.wait_for(serve(), 5)

    assert holder._task is None, "the refresh loop outlived the block"
    # Its outcome is logged as a waited-for fetch's would be.
    assert "ticket acquired (" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("wait", [True, False], ids=["waiting", "not-waiting"])
async def test_the_holder_is_closed_when_the_block_raises(wait):
    source = _GatedSource()
    source.gate.set()
    holder = TicketHolder(source)

    with pytest.raises(RuntimeError, match="the body failed"):
        async with lifecycle.running(holder, wait_for_first_fetch=wait):
            assert holder._task is not None
            raise RuntimeError("the body failed")

    assert holder._task is None, "the refresh loop outlived the block"


@pytest.mark.asyncio
@pytest.mark.parametrize("wait", [True, False], ids=["waiting", "not-waiting"])
async def test_the_holder_is_closed_when_the_block_is_cancelled(wait):
    """A SIGTERM arrives as a cancellation. It must still close the holder,
    and must still reach the caller rather than being swallowed on the way."""
    source = _GatedSource()
    source.gate.set()
    holder = TicketHolder(source)
    entered = asyncio.Event()

    async def serve():
        async with lifecycle.running(holder, wait_for_first_fetch=wait):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(serve())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert holder._task is None, "the refresh loop outlived the block"


@pytest.mark.asyncio
async def test_a_cancellation_during_the_first_fetch_leaves_nothing_running():
    """Waiting on an issuer that never answers, then stopped: the loop was never
    started, and nothing is left behind."""
    source = _GatedSource()
    holder = TicketHolder(source)

    async def serve():
        async with lifecycle.running(holder, wait_for_first_fetch=True):
            raise AssertionError("entered before the first fetch answered")

    task = asyncio.create_task(serve())
    await _until(lambda: source.calls == 1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert holder._task is None


@pytest.mark.asyncio
async def test_no_holder_is_announced_rather_than_silent(caplog):
    with caplog.at_level("INFO"):
        async with lifecycle.running(None, wait_for_first_fetch=True) as held:
            assert held is None

    assert "RAIL_PLUGIN_ENABLED is off" in caplog.text


def test_no_holder_is_built_with_the_plugin_off():
    assert lifecycle.build_holder() is None


def test_the_holder_is_built_with_the_configured_refresh_interval(monkeypatch):
    """A value that is not the default, so the default cannot produce it."""
    monkeypatch.setenv("RAIL_PLUGIN_ENABLED", "true")
    monkeypatch.setenv("RAIL_CENTER_URL", "https://rc.invalid")
    monkeypatch.setenv("RAIL_HOST_ID", "h")
    monkeypatch.setenv("RAIL_SANDBOX_NAME", "s")
    monkeypatch.setenv("RAIL_PROXY_REFRESH_SECONDS", "42")

    holder = lifecycle.build_holder()

    assert isinstance(holder, TicketHolder)
    assert holder.refresh_seconds == 42.0
    assert holder.source.sandbox_name == "s"


def test_the_health_payload_with_no_holder():
    assert lifecycle.health_payload(None, False) == {
        "status": "ok",
        "plugin_enabled": False,
        "ticket": None,
    }


@pytest.mark.parametrize(
    "state",
    [{"ticket": "the-ticket"}, {"reason": "not-found"}, {"reason": "expired"}],
    ids=["held", "not-found", "expired"],
)
def test_the_health_payload_reports_the_holder_and_never_the_ticket(state):
    """`ok` whatever the holder holds: failing closed is a designed state, and
    a probe that restarted the process over it would make an outage a crash
    loop."""
    holder = wound_holder(**state)

    payload = lifecycle.health_payload(holder, True)

    assert payload["status"] == "ok"
    assert payload["plugin_enabled"] is True
    assert payload["ticket"] == holder.status
    assert "the-ticket" not in repr(payload)
