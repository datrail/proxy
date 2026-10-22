"""The pipeline's judging of a body, whichever callout read it."""

import asyncio
import gzip
import threading
import time

import pytest

from envoy_support import BodyJudge
from proxy.core.body import BodyLimits, BodyPolicy, Reason
from proxy.core.judge import Allow, Deny
from proxy.core.request import build_request
from proxy.envoy_grpc import pipeline


def _build_request(headers=()):
    return build_request(
        method="POST",
        scheme="http",
        authority="mcp.example.com",
        path="/mcp",
        headers=headers,
    )


_REQUEST = _build_request()


def _pipeline(judge, **judging) -> pipeline.Pipeline:
    return pipeline.Pipeline(None, {}, pipeline.Judging(judge=judge, **judging))


@pytest.mark.asyncio
async def test_the_judge_sees_the_body_decoded_and_parsed():
    seen = []

    class Recording(BodyJudge):
        def judge_body(self, request, body):
            seen.append(body)
            return Allow()

    request = _build_request(
        [("content-encoding", "gzip"), ("content-type", "application/json")]
    )
    data = gzip.compress(b'{"method": "tools/call"}')
    await _pipeline(Recording()).judge_body(request, lambda: data)

    [view] = seen
    assert view.json == {"method": "tools/call"}


@pytest.mark.asyncio
async def test_a_reason_the_policy_allows_is_forwarded_unjudged(caplog):
    judge = BodyJudge()
    pipe = _pipeline(judge, body_policy=BodyPolicy([Reason.TRUNCATED]))

    verdict = await pipe.judge_body(_REQUEST, lambda: Reason.TRUNCATED)

    assert verdict == Allow()
    assert judge.bodies == []
    assert "(truncated); forwarding it unjudged" in caplog.text


@pytest.mark.asyncio
async def test_a_slow_judgement_is_cut_short_without_stalling_other_calls(caplog):
    """Judged in a worker thread, so the event loop answers other calls
    meanwhile, and refused as `timed-out` once the time budget runs out."""
    finished: list[str] = []

    class Slow(BodyJudge):
        def judge_body(self, request, body):
            time.sleep(0.5)
            return Allow()

    slow = _pipeline(Slow(), body_timeout=0.1)
    quick = _pipeline(BodyJudge())

    async def judge(name, pipe):
        verdict = await pipe.judge_body(_REQUEST, lambda: b"{}")
        finished.append(name)
        return verdict

    started = time.monotonic()
    slow_verdict, _ = await asyncio.gather(judge("slow", slow), judge("quick", quick))
    elapsed = time.monotonic() - started

    assert finished == ["quick", "slow"]
    assert elapsed < 0.4
    assert slow_verdict == Deny()
    assert "(timed-out); refusing it" in caplog.text


@pytest.mark.asyncio
async def test_a_timed_out_body_holds_the_budget_until_its_thread_ends(caplog):
    """A thread can't be stopped, so one whose verdict came too late keeps
    the body's bytes until it ends, and only then gives them back."""
    release = threading.Event()

    class Stuck(BodyJudge):
        def judge_body(self, request, body):
            release.wait(5)
            return Allow()

    pipe = _pipeline(
        Stuck(), body_limits=BodyLimits(max_bytes=4, budget_bytes=4), body_timeout=0.05
    )

    with pipe.open_quota() as quota:
        assert quota.take(4) is None
        verdict = await pipe.judge_body(_REQUEST, lambda: b"1234", quota)
    assert verdict == Deny()
    assert "(timed-out); refusing it" in caplog.text
    with pipe.open_quota() as quota:
        assert quota.take(4) == Reason.OVER_BUDGET

    release.set()
    budget = pipe._budget
    for _ in range(100):
        if budget.held_bytes == 0:
            break
        await asyncio.sleep(0.01)
    assert budget.held_bytes == 0


@pytest.mark.asyncio
async def test_a_body_past_its_deadline_is_never_judged(monkeypatch, caplog):
    """Work whose wait has ended stops at its next step."""
    judge = BodyJudge()
    pipe = _pipeline(judge, body_timeout=0.05)
    # The deadline passes once the view is built, before the judge runs.
    monkeypatch.setattr(pipeline, "is_past", lambda deadline: True)

    verdict = await pipe.judge_body(_REQUEST, lambda: b"{}")

    assert verdict == Deny()
    assert judge.bodies == []
    assert "(timed-out); refusing it" in caplog.text
