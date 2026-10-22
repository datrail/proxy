"""What the extension answers for one request, whichever callout asked.

Each adapter hands a request to the server's one `Pipeline`, which judges and
decides it, and turns the `Outcome` into its protocol's answer.
"""

import logging
import traceback
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from proxy.core.judge import Allow, Deny, Judge, NeedsBody
from proxy.core.request import Request
from proxy.core.xrail_auth import TicketHeaders, TicketHolder, outbound_headers
from proxy.envoy_grpc.decision import Decision, decide
from proxy.envoy_grpc.settings import ProtectedHost

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    """Refused, or let through with `decision`'s changes. A refused request
    has no changes."""

    verdict: Allow | Deny
    decision: Decision = field(default_factory=Decision)


class Pipeline:
    """Judges and decides every request, for every adapter. One per server,
    so the log line about the ticket is the same whichever callout asked."""

    def __init__(
        self,
        holder: TicketHolder | None,
        protected_hosts: Mapping[str, ProtectedHost],
        judge: Judge,
        on_judge_failure: Allow | Deny,
    ) -> None:
        self._holder = holder
        self._protected_hosts = protected_hosts
        self._ticket_headers = TicketHeaders(holder)
        self._judge = judge
        self._on_judge_failure = on_judge_failure

    def get_outcome(
        self,
        build_request: Callable[[], Request],
        target: str,
        header_names: Iterable[str],
    ) -> Outcome:
        """`target` and `header_names` are what `decide` reads: the request's
        `:authority`, and the names of every header the agent sent."""
        return get_outcome(
            lambda: self._judge.judge_headers(build_request()),
            lambda: self._decide(target, header_names),
            self._on_judge_failure,
        )

    def _decide(self, target: str, header_names: Iterable[str]) -> Decision:
        xrail_headers = outbound_headers(self._holder)
        decision = decide(target, header_names, self._protected_hosts, xrail_headers)
        if decision.headers:
            self._ticket_headers.report(target, xrail_headers)
        return decision


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
        log_failure("could not judge a request", exc, on_failure)
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
        log_failure("could not decide on a request", exc, "forwarding it unchanged")
        return Outcome(verdict)


def log_failure(what: str, exc: Exception, action: str) -> None:
    """Log `exc` with its traceback but not its message, which could quote a
    header value."""
    log.error(
        "%s (%s); %s\n%s",
        what,
        type(exc).__name__,
        action,
        "".join(traceback.format_tb(exc.__traceback__)).rstrip(),
    )
