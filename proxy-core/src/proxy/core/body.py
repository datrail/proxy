"""The body of an HTTP message, read under bounds, for a rule that needs it.

An interface reads a body only when the judge needs it, and hands the judge
the bytes, or a `Reason` it can't. Each message's body is held up to
`RAIL_PROXY_MAX_BODY_BYTES`, and the bodies held at once up to
`RAIL_PROXY_BODY_BUDGET_BYTES`, so no burst makes the process hold more.
`BodyPolicy` turns a reason into a verdict.
"""

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from proxy.core.judge import Allow, Deny
from proxy.core.settings import ConfigError

_MAX_BODY_BYTES = "RAIL_PROXY_MAX_BODY_BYTES"
_BODY_BUDGET_BYTES = "RAIL_PROXY_BODY_BUDGET_BYTES"
_DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024
# Bodies at the cap that may be held at once, by default.
_DEFAULT_BUDGET_IN_CAPS = 4


class Reason(StrEnum):
    """Why a body can't be judged as it is."""

    NOT_SENT = "not-sent"
    TRUNCATED = "truncated"
    TOO_LARGE = "too-large"
    OVER_BUDGET = "over-budget"


@dataclass(frozen=True)
class BodyLimits:
    max_bytes: int = _DEFAULT_MAX_BODY_BYTES
    budget_bytes: int = _DEFAULT_MAX_BODY_BYTES * _DEFAULT_BUDGET_IN_CAPS


def get_body_limits() -> BodyLimits:
    """From `RAIL_PROXY_MAX_BODY_BYTES` (8 MiB by default) and
    `RAIL_PROXY_BODY_BUDGET_BYTES` (4 caps by default), in bytes."""
    max_bytes = _get_bytes_setting(_MAX_BODY_BYTES) or _DEFAULT_MAX_BODY_BYTES
    budget_bytes = _get_bytes_setting(_BODY_BUDGET_BYTES) or (
        max_bytes * _DEFAULT_BUDGET_IN_CAPS
    )
    if budget_bytes < max_bytes:
        raise ConfigError(
            f"{_BODY_BUDGET_BYTES}={budget_bytes} is less than "
            f"{_MAX_BODY_BYTES}={max_bytes}, so no body at the cap could be read"
        )
    return BodyLimits(max_bytes=max_bytes, budget_bytes=budget_bytes)


def _get_bytes_setting(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    if not raw.isdigit() or int(raw) == 0:
        raise ConfigError(f"{name}={raw!r} is not a positive number of bytes")
    return int(raw)


class BodyBudget:
    """The body bytes the process holds at once, across every request."""

    def __init__(self, limits: BodyLimits) -> None:
        self._limits = limits
        self._held = 0

    @property
    def held_bytes(self) -> int:
        return self._held

    def open_quota(self) -> "BodyQuota":
        """A quota for one message's body. Close it once the body is judged."""
        return BodyQuota(self, self._limits.max_bytes)

    def _reserve(self, size: int) -> bool:
        if self._held + size > self._limits.budget_bytes:
            return False
        self._held += size
        return True

    def _release(self, size: int) -> None:
        self._held -= size


class BodyQuota:
    """What one message's body may hold: up to the cap, while the budget
    lasts. Counts the bytes; the interface keeps them."""

    def __init__(self, budget: BodyBudget, max_bytes: int) -> None:
        self._budget = budget
        self._max_bytes = max_bytes
        self._taken = 0

    def take(self, size: int) -> Reason | None:
        """Count `size` more bytes, or say why they can't be held."""
        if self._taken + size > self._max_bytes:
            return Reason.TOO_LARGE
        if not self._budget._reserve(size):
            return Reason.OVER_BUDGET
        self._taken += size
        return None

    def close(self) -> None:
        self._budget._release(self._taken)
        self._taken = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class BodyPolicy:
    """The verdict for a body that can't be judged as it is: deny, for every
    reason."""

    def get_verdict(self, reason: Reason) -> Allow | Deny:
        return Deny()
