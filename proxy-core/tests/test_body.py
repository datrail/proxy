"""A body read under bounds: the settings, the per-message cap, the
process-wide budget, and the policy for a body that can't be judged."""

import pytest

from proxy.core.body import (
    BodyBudget,
    BodyLimits,
    BodyPolicy,
    Reason,
    get_body_limits,
)
from proxy.core.judge import Deny
from proxy.core.settings import ConfigError

_MIB = 1024 * 1024


def test_the_limits_default_to_8_mib_and_4_bodies_at_once():
    assert get_body_limits() == BodyLimits(max_bytes=8 * _MIB, budget_bytes=32 * _MIB)


def test_the_budget_defaults_to_4_caps_of_the_cap_set(monkeypatch):
    monkeypatch.setenv("RAIL_PROXY_MAX_BODY_BYTES", "1000")

    assert get_body_limits() == BodyLimits(max_bytes=1000, budget_bytes=4000)


def test_both_limits_can_be_set(monkeypatch):
    monkeypatch.setenv("RAIL_PROXY_MAX_BODY_BYTES", " 1000 ")
    monkeypatch.setenv("RAIL_PROXY_BODY_BUDGET_BYTES", "1000")

    assert get_body_limits() == BodyLimits(max_bytes=1000, budget_bytes=1000)


@pytest.mark.parametrize(
    "name", ["RAIL_PROXY_MAX_BODY_BYTES", "RAIL_PROXY_BODY_BUDGET_BYTES"]
)
@pytest.mark.parametrize("value", ["0", "-1", "8MiB", "1.5", "1e6"])
def test_a_limit_that_is_not_a_positive_number_of_bytes_is_refused(
    monkeypatch, name, value
):
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError, match=name):
        get_body_limits()


def test_a_budget_under_the_cap_is_refused(monkeypatch):
    monkeypatch.setenv("RAIL_PROXY_MAX_BODY_BYTES", "1000")
    monkeypatch.setenv("RAIL_PROXY_BODY_BUDGET_BYTES", "999")

    with pytest.raises(ConfigError, match="no body at the cap could be read"):
        get_body_limits()


def test_a_body_past_the_cap_is_too_large_and_holds_nothing_more():
    budget = BodyBudget(BodyLimits(max_bytes=10, budget_bytes=100))

    with budget.open_quota() as quota:
        assert quota.take(6) is None
        assert quota.take(4) is None
        assert quota.take(1) == Reason.TOO_LARGE
        assert budget.held_bytes == 10


def test_bodies_past_the_budget_are_over_budget():
    budget = BodyBudget(BodyLimits(max_bytes=10, budget_bytes=15))

    with budget.open_quota() as first, budget.open_quota() as second:
        assert first.take(10) is None
        assert second.take(5) is None
        assert second.take(1) == Reason.OVER_BUDGET


def test_a_closed_quota_gives_its_bytes_back():
    budget = BodyBudget(BodyLimits(max_bytes=10, budget_bytes=10))

    with budget.open_quota() as quota:
        quota.take(10)
    with budget.open_quota() as quota:
        assert quota.take(10) is None
    assert budget.held_bytes == 0


@pytest.mark.parametrize("reason", list(Reason))
def test_the_policy_denies_every_reason(reason):
    assert BodyPolicy().get_verdict(reason) == Deny()
