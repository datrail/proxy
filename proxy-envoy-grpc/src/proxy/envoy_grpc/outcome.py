"""What the extension answers for one request, whichever callout asked.

Each adapter has `get_outcome` judge and decide the request, and turns the
`Outcome` into its protocol's answer.
"""

import logging
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field

from proxy.core.judge import Allow, Deny, NeedsBody
from proxy.envoy_grpc.decision import Decision

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    """Refused, or let through with `decision`'s changes. A refused request
    has no changes."""

    verdict: Allow | Deny
    decision: Decision = field(default_factory=Decision)


def get_outcome(
    judge: Callable[[], Allow | Deny | NeedsBody],
    decide: Callable[[], Decision],
    on_judge_failure: Allow | Deny,
) -> Outcome:
    """Judge, then decide on what the judge allows.

    A failure while judging, building the request included, gets
    `on_judge_failure`, so a request that breaks the adapter is never forwarded
    unjudged unless the setting says so. A failure while deciding forwards the
    request unchanged, as if this extension were down.
    """
    refused = isinstance(on_judge_failure, Deny)
    on_failure = "refusing it" if refused else "forwarding it unjudged"
    try:
        verdict = judge()
    except Exception as exc:  # noqa: BLE001 - answered as the setting says
        _log_failure("could not judge a request", exc, on_failure)
        verdict = on_judge_failure
    if isinstance(verdict, NeedsBody):
        # Nothing reads a body yet.
        log.error("the judge needs the body, which is not read; %s", on_failure)
        verdict = on_judge_failure

    if isinstance(verdict, Deny):
        return Outcome(verdict)
    try:
        return Outcome(verdict, decide())
    except Exception as exc:  # noqa: BLE001 - every failure is answered OK
        _log_failure("could not decide on a request", exc, "forwarding it unchanged")
        return Outcome(verdict)


def _log_failure(what: str, exc: Exception, action: str) -> None:
    # The traceback but not the exception's message, which could quote a
    # header value.
    log.error(
        "%s (%s); %s\n%s",
        what,
        type(exc).__name__,
        action,
        "".join(traceback.format_tb(exc.__traceback__)).rstrip(),
    )
