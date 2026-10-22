"""What the extension answers for one request, whichever callout asked.

Each adapter has the server's one `Pipeline` judge a request, from its headers
and, when the judge needs it, its body, then decide on it; and turns the
`Outcome` into its protocol's answer.
"""

import logging
import traceback
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from proxy.core.body import BodyBudget, BodyLimits, BodyPolicy, BodyQuota, Reason
from proxy.core.judge import Allow, AllowAll, Deny, Judge, NeedsBody
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


@dataclass(frozen=True)
class Judging:
    """How requests are judged: the judge, the verdict when judging fails,
    and the bounds on the bodies read."""

    judge: Judge = field(default_factory=AllowAll)
    on_failure: Allow | Deny = field(default_factory=Deny)
    body_limits: BodyLimits = field(default_factory=BodyLimits)


class Pipeline:
    """Judges and decides every request, for every adapter. One per server,
    so the log line about the ticket, and the body budget, are the same
    whichever callout asked.

    A failure while judging, building the request or reading the body
    included, gets `judging.on_failure`, so a request that breaks the adapter is
    never forwarded unjudged unless the setting says so. A failure while
    deciding forwards the request unchanged, as if this extension were down.
    """

    def __init__(
        self,
        holder: TicketHolder | None,
        protected_hosts: Mapping[str, ProtectedHost],
        judging: Judging,
    ) -> None:
        self._holder = holder
        self._protected_hosts = protected_hosts
        self._ticket_headers = TicketHeaders(holder)
        self._judge = judging.judge
        self._on_judge_failure = judging.on_failure
        self._budget = BodyBudget(judging.body_limits)
        self._policy = BodyPolicy()

    def judge_headers(
        self, build_request: Callable[[], Request]
    ) -> tuple[Request | None, Allow | Deny | NeedsBody]:
        """The request, and the judge's verdict on it. The request is None
        when it couldn't be built, and the verdict is then never
        `NeedsBody`."""
        try:
            request = build_request()
            return request, self._judge.judge_headers(request)
        except Exception as exc:  # noqa: BLE001 - answered as the setting says
            return None, self.get_failure_verdict(exc)

    def open_quota(self) -> BodyQuota:
        """A quota for one request's body, under the process-wide budget."""
        return self._budget.open_quota()

    def judge_body(
        self, request: Request, read: Callable[[], bytes | Reason]
    ) -> Allow | Deny:
        """The verdict once `read` has the body, or the policy's for the
        reason it can't."""
        try:
            body = read()
            if isinstance(body, Reason):
                verdict = self._policy.get_verdict(body)
                log.warning(
                    "cannot judge a request's body (%s); %s",
                    body,
                    _describe(verdict),
                )
                return verdict
            return self._judge.judge_body(request, body)
        except Exception as exc:  # noqa: BLE001 - answered as the setting says
            return self.get_failure_verdict(exc)

    def get_failure_verdict(self, exc: Exception) -> Allow | Deny:
        """Log a failure while judging, and give the verdict for it."""
        log_failure("could not judge a request", exc, _describe(self._on_judge_failure))
        return self._on_judge_failure

    def get_outcome(
        self, verdict: Allow | Deny, target: str, header_names: Iterable[str]
    ) -> Outcome:
        """Decide on what the judge allows. `target` and `header_names` are
        what `decide` reads: the request's `:authority`, and the names of
        every header the agent sent."""
        if isinstance(verdict, Deny):
            return Outcome(verdict)
        try:
            return Outcome(verdict, self._decide(target, header_names))
        except Exception as exc:  # noqa: BLE001 - every failure is answered OK
            log_failure("could not decide on a request", exc, "forwarding it unchanged")
            return Outcome(verdict)

    def _decide(self, target: str, header_names: Iterable[str]) -> Decision:
        xrail_headers = outbound_headers(self._holder)
        decision = decide(target, header_names, self._protected_hosts, xrail_headers)
        if decision.headers:
            self._ticket_headers.report(target, xrail_headers)
        return decision


def _describe(verdict: Allow | Deny) -> str:
    return "refusing it" if isinstance(verdict, Deny) else "forwarding it unjudged"


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
