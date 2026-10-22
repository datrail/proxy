"""Whether a request may go out, judged from what it carries.

A judge answers once the headers are in: allow, deny, or needs the body; and
when it needs the body, again once the body is read. Its rules are the
judge's own; until they exist, `AllowAll` is the judge.

What happens when judging fails, before any verdict, is a setting:
`RAIL_PROXY_ON_JUDGE_FAILURE`, `deny` by default.
"""

import os
from dataclasses import dataclass
from typing import Protocol

from proxy.core.request import Request
from proxy.core.settings import ConfigError

# What a refused agent is answered, whichever interface refuses it.
DENIED_STATUS = 403
DENIED_CONTENT_TYPE = "text/plain"
DENIED_BODY = "request refused by policy\n"

_ON_JUDGE_FAILURE = "RAIL_PROXY_ON_JUDGE_FAILURE"


@dataclass(frozen=True)
class Allow:
    """The request may go out."""


@dataclass(frozen=True)
class Deny:
    """The request is refused, and answered with `DENIED_BODY`."""


@dataclass(frozen=True)
class NeedsBody:
    """A rule that still applies reads the body."""


class Judge(Protocol):
    def judge_headers(self, request: Request) -> Allow | Deny | NeedsBody:
        """The verdict from everything but the body. Raises on a failure,
        which the caller answers as `get_judge_failure_verdict` says."""
        ...

    def judge_body(self, request: Request, body: bytes) -> Allow | Deny:
        """The verdict once the body is read, after `judge_headers` needed
        it. `body` is empty for a request without one."""
        ...


class AllowAll:
    """The judge until rules exist: allows everything, never reads a body."""

    def judge_headers(self, request: Request) -> Allow:
        return Allow()

    def judge_body(self, request: Request, body: bytes) -> Allow:
        return Allow()


def get_judge_failure_verdict() -> Allow | Deny:
    """The verdict for a request that couldn't be judged, from
    `RAIL_PROXY_ON_JUDGE_FAILURE`: `deny` (the default) or `allow`, case
    folded."""
    raw = os.environ.get(_ON_JUDGE_FAILURE, "").strip().lower()
    if raw in ("", "deny"):
        return Deny()
    if raw == "allow":
        return Allow()
    raise ConfigError(f"{_ON_JUDGE_FAILURE}={raw!r} is not deny or allow")
