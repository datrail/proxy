"""The judge's default, and the setting for a request it can't judge."""

import pytest

from proxy.core.judge import Allow, AllowAll, Deny, get_judge_failure_verdict
from proxy.core.request import build_request
from proxy.core.settings import ConfigError


def test_allow_all_allows_every_request():
    request = build_request("DELETE", "http", "any.example.com", "/", [])

    assert AllowAll().judge_headers(request) == Allow()


def test_a_judge_failure_is_denied_by_default():
    assert get_judge_failure_verdict() == Deny()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("deny", Deny()), ("allow", Allow()), (" Allow ", Allow()), ("", Deny())],
)
def test_the_judge_failure_setting(monkeypatch, value, expected):
    monkeypatch.setenv("RAIL_PROXY_ON_JUDGE_FAILURE", value)

    assert get_judge_failure_verdict() == expected


@pytest.mark.parametrize("value", ["refuse", "true", "deny,allow"])
def test_another_judge_failure_value_is_refused(monkeypatch, value):
    monkeypatch.setenv("RAIL_PROXY_ON_JUDGE_FAILURE", value)

    with pytest.raises(ConfigError, match="RAIL_PROXY_ON_JUDGE_FAILURE"):
        get_judge_failure_verdict()
