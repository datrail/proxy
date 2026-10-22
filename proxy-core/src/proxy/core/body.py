"""The body of an HTTP message, read under bounds, for a rule that needs it.

An interface reads a body only when the judge needs it. Each message's body is
held up to `RAIL_PROXY_MAX_BODY_BYTES`, and the bodies held at once up to
`RAIL_PROXY_BODY_BUDGET_BYTES`, so no burst makes the process hold more.

Rules never see the raw bytes: `build_view` decodes them by
`Content-Encoding`, last coding first, under the same cap, and parses them by
`Content-Type`. A body that can't be read or built into a view gets a
`Reason`, which `BodyPolicy` turns into a verdict.
"""

import json
import os
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self

from proxy.core.judge import Allow, Deny
from proxy.core.settings import ConfigError, seconds_setting

_MAX_BODY_BYTES = "RAIL_PROXY_MAX_BODY_BYTES"
_BODY_BUDGET_BYTES = "RAIL_PROXY_BODY_BUDGET_BYTES"
_DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024
# Bodies at the cap that may be held at once, by default.
_DEFAULT_BUDGET_IN_CAPS = 4
_ALLOW_UNREADABLE = "RAIL_PROXY_ALLOW_UNREADABLE_BODY"
_BODY_TIMEOUT_SECONDS = "RAIL_PROXY_BODY_TIMEOUT_SECONDS"
# Under Envoy's default callout timeouts (200 ms for ext_proc's
# `message_timeout` and ext_authz's `timeout`).
_DEFAULT_BODY_TIMEOUT_SECONDS = 0.15


class Reason(StrEnum):
    """Why a body can't be judged as it is."""

    NOT_SENT = "not-sent"
    TRUNCATED = "truncated"
    TOO_LARGE = "too-large"
    OVER_BUDGET = "over-budget"
    UNSUPPORTED_ENCODING = "unsupported-encoding"
    DECODED_TOO_LARGE = "decoded-too-large"
    MALFORMED = "malformed"
    TIMED_OUT = "timed-out"


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
    """The verdict for a body that can't be judged as it is: deny, unless
    its reason is one `allowed` lets through unjudged."""

    def __init__(self, allowed: Iterable[Reason] = ()) -> None:
        self._allowed = frozenset(allowed)

    def get_verdict(self, reason: Reason) -> Allow | Deny:
        return Allow() if reason in self._allowed else Deny()


def get_body_policy() -> BodyPolicy:
    """From `RAIL_PROXY_ALLOW_UNREADABLE_BODY`: the comma-separated reasons
    whose bodies are let through unjudged. None by default."""
    raw = os.environ.get(_ALLOW_UNREADABLE, "")
    names = {name.strip().lower() for name in raw.split(",") if name.strip()}
    unknown = names - {reason.value for reason in Reason}
    if unknown:
        raise ConfigError(
            f"{_ALLOW_UNREADABLE}: {', '.join(sorted(unknown))} is not a reason; "
            f"one of {', '.join(reason.value for reason in Reason)}"
        )
    return BodyPolicy(Reason(name) for name in names)


def get_body_timeout() -> float:
    """From `RAIL_PROXY_BODY_TIMEOUT_SECONDS`: how long building a body's
    view and judging it may take before the body is `timed-out`. 0.15 s by
    default, under Envoy's default callout timeouts."""
    return seconds_setting(_BODY_TIMEOUT_SECONDS, _DEFAULT_BODY_TIMEOUT_SECONDS)


@dataclass(frozen=True)
class BodyView:
    """What a rule sees of a body: its bytes, decoded, and its media type
    (`application/json`, or empty when none was sent). `json` is the parsed
    value when `is_json`; a binary type is never parsed."""

    data: bytes
    media_type: str
    is_json: bool = False
    json: Any = field(default=None, compare=False)


def build_view(
    data: bytes, headers: Iterable[tuple[str, str]], max_bytes: int
) -> BodyView | Reason:
    """Decode `data` by the message's `Content-Encoding`, last coding first,
    never past `max_bytes`, then parse it by its `Content-Type`. Slow on a
    large body: run it off the event loop."""
    headers = list(headers)
    for coding in reversed(_get_codings(headers)):
        decoder = _DECODERS.get(coding)
        if decoder is None:
            return Reason.UNSUPPORTED_ENCODING
        decoded = decoder(data, max_bytes)
        if isinstance(decoded, Reason):
            return decoded
        data = decoded
    media_type = _get_media_type(headers)
    if not data or not _is_json(media_type):
        return BodyView(data, media_type)
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        return Reason.MALFORMED
    return BodyView(data, media_type, is_json=True, json=value)


def _get_codings(headers: list[tuple[str, str]]) -> list[str]:
    """Every coding, in the order applied, across repeated headers."""
    return [
        coding.strip().lower()
        for name, value in headers
        if name.lower() == "content-encoding"
        for coding in value.split(",")
        if coding.strip()
    ]


def _get_media_type(headers: list[tuple[str, str]]) -> str:
    value = next((v for n, v in headers if n.lower() == "content-type"), "")
    return value.split(";")[0].strip().lower()


def _is_json(media_type: str) -> bool:
    return media_type == "application/json" or media_type.endswith("+json")


def _decode_identity(data: bytes, max_bytes: int) -> bytes:
    return data


def _decode_gzip(data: bytes, max_bytes: int) -> bytes | Reason:
    """Every gzip member in `data`, stopped as soon as the output passes
    `max_bytes`, so a bomb never expands past it."""
    out = bytearray()
    while data:
        decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        try:
            out += decompressor.decompress(data, max_bytes + 1 - len(out))
        except zlib.error:
            return Reason.MALFORMED
        if len(out) > max_bytes:
            return Reason.DECODED_TOO_LARGE
        if not decompressor.eof:
            # The input ended inside a member.
            return Reason.MALFORMED
        data = decompressor.unused_data
    return bytes(out)


_DECODERS: dict[str, Callable[[bytes, int], bytes | Reason]] = {
    "identity": _decode_identity,
    "gzip": _decode_gzip,
    "x-gzip": _decode_gzip,
}
