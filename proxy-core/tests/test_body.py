"""A body read under bounds: the settings, the per-message cap, the
process-wide budget, the policy for a body that can't be judged, and the view
rules see."""

import gzip

import pytest

from proxy.core.body import (
    BodyBudget,
    BodyLimits,
    BodyPolicy,
    BodyView,
    Reason,
    build_view,
    get_body_limits,
    get_body_policy,
    get_body_timeout,
)
from proxy.core.judge import Allow, Deny
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


def test_the_policy_lets_through_the_reasons_it_is_given():
    policy = BodyPolicy([Reason.TRUNCATED])

    assert policy.get_verdict(Reason.TRUNCATED) == Allow()
    assert policy.get_verdict(Reason.TOO_LARGE) == Deny()


def test_the_policy_denies_every_reason_by_default():
    policy = get_body_policy()

    assert all(policy.get_verdict(reason) == Deny() for reason in Reason)


def test_the_reasons_to_let_through_are_a_setting(monkeypatch):
    monkeypatch.setenv(
        "RAIL_PROXY_ALLOW_UNREADABLE_BODY", " Truncated, unsupported-encoding ,"
    )

    policy = get_body_policy()

    allowed = {reason for reason in Reason if policy.get_verdict(reason) == Allow()}
    assert allowed == {Reason.TRUNCATED, Reason.UNSUPPORTED_ENCODING}


def test_a_reason_that_does_not_exist_is_refused(monkeypatch):
    monkeypatch.setenv("RAIL_PROXY_ALLOW_UNREADABLE_BODY", "truncated,too-big")

    with pytest.raises(ConfigError, match="too-big is not a reason"):
        get_body_policy()


def test_the_body_timeout_defaults_under_envoy_s_callout_timeouts():
    assert get_body_timeout() == 0.15


def test_the_body_timeout_is_a_setting(monkeypatch):
    monkeypatch.setenv("RAIL_PROXY_BODY_TIMEOUT_SECONDS", "0.5")

    assert get_body_timeout() == 0.5


_JSON = [("content-type", "application/json; charset=utf-8")]


def _gzip(data: bytes) -> bytes:
    return gzip.compress(data)


def test_a_plain_body_is_seen_as_it_came():
    view = build_view(b"\x00\x01binary", [("content-type", "image/png")], 100)

    assert view == BodyView(b"\x00\x01binary", "image/png")
    assert not view.is_json


@pytest.mark.parametrize(
    "media_type", ["application/json", "application/vnd.api+json", "APPLICATION/JSON"]
)
def test_a_json_body_is_parsed(media_type):
    view = build_view(b'{"method": "tools/call"}', [("content-type", media_type)], 100)

    assert view.is_json
    assert view.json == {"method": "tools/call"}


def test_an_empty_json_body_is_not_parsed():
    view = build_view(b"", _JSON, 100)

    assert view == BodyView(b"", "application/json")


@pytest.mark.parametrize(
    "data",
    [b'{"method": ', b"\xff\xfe\x00", b"[" * 100_000],
    ids=["cut", "bytes", "deep"],
)
def test_json_that_does_not_parse_is_malformed(data):
    assert build_view(data, _JSON, 1_000_000) == Reason.MALFORMED


@pytest.mark.parametrize("coding", ["gzip", "x-gzip", "GZIP"])
def test_a_gzip_body_is_decoded_before_it_is_parsed(coding):
    headers = [*_JSON, ("content-encoding", coding)]

    view = build_view(_gzip(b'{"a": 1}'), headers, 100)

    assert view.json == {"a": 1}


def test_stacked_codings_are_undone_last_first():
    data = _gzip(_gzip(b'{"a": 1}'))

    across_one = build_view(
        data, [*_JSON, ("content-encoding", "gzip, identity, gzip")], 100
    )
    across_two = build_view(
        data, [*_JSON, ("content-encoding", "gzip"), ("content-encoding", "gzip")], 100
    )

    assert across_one.json == across_two.json == {"a": 1}


def test_several_gzip_members_are_decoded_whole():
    view = build_view(
        _gzip(b"first,") + _gzip(b"second"), [("content-encoding", "gzip")], 100
    )

    assert view.data == b"first,second"


def test_a_zip_bomb_stops_at_the_cap():
    bomb = _gzip(b"\x00" * (64 * 1024 * 1024))
    assert len(bomb) < 100_000

    assert (
        build_view(bomb, [("content-encoding", "gzip")], 1024)
        == Reason.DECODED_TOO_LARGE
    )


def test_a_body_decoded_to_exactly_the_cap_is_kept():
    view = build_view(_gzip(b"x" * 1024), [("content-encoding", "gzip")], 1024)

    assert view.data == b"x" * 1024


def test_a_stacked_bomb_stops_at_the_cap():
    bomb = _gzip(_gzip(b"\x00" * (64 * 1024 * 1024)))

    view = build_view(bomb, [("content-encoding", "gzip, gzip")], 1024 * 1024)

    assert view == Reason.DECODED_TOO_LARGE


@pytest.mark.parametrize(
    "data", [_gzip(b"whole")[:-10], b"not gzip at all"], ids=["cut", "not-gzip"]
)
def test_gzip_that_does_not_decode_is_malformed(data):
    assert build_view(data, [("content-encoding", "gzip")], 100) == Reason.MALFORMED


@pytest.mark.parametrize("coding", ["br", "zstd", "deflate", "compress"])
def test_a_coding_with_no_decoder_is_unsupported(coding):
    headers = [("content-encoding", f"gzip, {coding}")]

    assert build_view(b"anything", headers, 100) == Reason.UNSUPPORTED_ENCODING
