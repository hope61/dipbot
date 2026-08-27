"""Eligibility rules, including the missing-liquidity case."""
from __future__ import annotations

import time

import pytest

from dipbot.filters import ALLOW, PROXY, SKIP, check_token, filters_enabled
from tests.conftest import make_meta


def test_healthy_token_passes(meta, settings):
    result = check_token(meta, settings)
    assert result.passed
    assert not result.liquidity_unknown


def test_low_liquidity_blocked(settings):
    result = check_token(make_meta(liquidity_usd=500.0), settings)
    assert not result.passed
    assert "liquidity" in result.reason


def test_low_volume_blocked(settings):
    result = check_token(make_meta(volume_h24=100.0), settings)
    assert not result.passed
    assert "volume" in result.reason


def test_result_is_falsy_when_blocked(settings):
    # check_token results are used in boolean context by the detector.
    assert not check_token(make_meta(liquidity_usd=1.0), settings)
    assert check_token(make_meta(), settings)


# --- missing liquidity ------------------------------------------------------
# MEMEDEX on Meteora DBC reported liquidity=null with $141k of real 24h volume.
# Treating null as $0 would silently mute it.


def test_proxy_policy_allows_traded_token(memedex, settings):
    settings["unknown_liquidity"] = PROXY
    result = check_token(memedex, settings)
    assert result.passed
    assert result.liquidity_unknown
    assert "volume ok" in result.reason


def test_proxy_policy_blocks_untraded_token(settings):
    settings["unknown_liquidity"] = PROXY
    token = make_meta(liquidity_usd=None, volume_h24=1_000.0)
    result = check_token(token, settings)
    assert not result.passed
    assert "volume" in result.reason


def test_proxy_policy_blocks_when_volume_also_missing(settings):
    settings["unknown_liquidity"] = PROXY
    token = make_meta(liquidity_usd=None, volume_h24=None)
    result = check_token(token, settings)
    assert not result.passed
    assert "both unknown" in result.reason


def test_skip_policy_blocks_even_when_traded(memedex, settings):
    settings["unknown_liquidity"] = SKIP
    result = check_token(memedex, settings)
    assert not result.passed
    assert result.reason == "liquidity unknown"


def test_allow_policy_passes_when_only_liquidity_is_missing(settings):
    settings["unknown_liquidity"] = ALLOW
    token = make_meta(liquidity_usd=None, volume_h24=None)
    result = check_token(token, settings)
    assert result.passed
    assert result.liquidity_unknown


def test_allow_policy_still_respects_volume_floor(settings):
    """`allow` means ignore the missing liquidity figure, not ignore all filters."""
    settings["unknown_liquidity"] = ALLOW
    token = make_meta(liquidity_usd=None, volume_h24=100.0)
    assert not check_token(token, settings).passed


def test_unknown_liquidity_never_silently_reads_as_zero(memedex, settings):
    """The bug this module exists to prevent."""
    settings["unknown_liquidity"] = PROXY
    settings["min_liquidity_usd"] = 10_000.0
    assert check_token(memedex, settings).passed


# --- age --------------------------------------------------------------------


def test_min_age_blocks_new_token(settings):
    settings["min_token_age_minutes"] = 60
    fresh = make_meta(pair_created_at=int((time.time() - 120) * 1000))
    result = check_token(fresh, settings)
    assert not result.passed
    assert "too new" in result.reason


def test_min_age_allows_old_token(settings):
    settings["min_token_age_minutes"] = 60
    result = check_token(make_meta(), settings)
    assert result.passed


def test_min_age_blocks_unknown_age(settings):
    settings["min_token_age_minutes"] = 60
    result = check_token(make_meta(pair_created_at=None), settings)
    assert not result.passed
    assert "age unknown" in result.reason


def test_age_filter_off_by_default(settings):
    fresh = make_meta(pair_created_at=int((time.time() - 5) * 1000))
    assert check_token(fresh, settings).passed


@pytest.mark.parametrize("policy", [PROXY, SKIP, ALLOW])
def test_every_policy_returns_a_reason(policy, memedex, settings):
    settings["unknown_liquidity"] = policy
    assert check_token(memedex, settings).reason


# --- master switch ----------------------------------------------------------


def test_filters_on_by_default(settings):
    settings.pop("filters_enabled", None)
    assert filters_enabled(settings)


def test_switch_off_passes_a_token_that_would_be_blocked(settings):
    """A $500 pool is exactly what the floor exists to block."""
    thin = make_meta(liquidity_usd=500.0, volume_h24=10.0)
    assert not check_token(thin, settings).passed

    settings["filters_enabled"] = False
    result = check_token(thin, settings)
    assert result.passed
    assert result.reason == "filters off"


def test_switch_off_still_reports_unknown_liquidity(memedex, settings):
    """Alerts still need to say the liquidity figure is missing."""
    settings["filters_enabled"] = False
    assert check_token(memedex, settings).liquidity_unknown


def test_switch_off_ignores_age_limit(settings):
    settings["min_token_age_minutes"] = 600
    fresh = make_meta(pair_created_at=int((time.time() - 60) * 1000))
    assert not check_token(fresh, settings).passed

    settings["filters_enabled"] = False
    assert check_token(fresh, settings).passed


@pytest.mark.parametrize("value, expected", [
    (True, True), (False, False),
    ("true", True), ("false", False),
    ("on", True), ("off", False),
    ("yes", True), ("no", False),
    ("1", True), ("0", False),
])
def test_switch_accepts_stringy_values(settings, value, expected):
    """Settings round-trip through JSON and .env, so strings show up."""
    settings["filters_enabled"] = value
    assert filters_enabled(settings) is expected


# --- per-filter switches ----------------------------------------------------


def test_liquidity_switch_off_ignores_the_floor(settings):
    thin = make_meta(liquidity_usd=500.0)
    assert not check_token(thin, settings).passed

    settings["filter_liquidity_enabled"] = False
    assert check_token(thin, settings).passed


def test_liquidity_switch_off_keeps_volume_filter_working(settings):
    settings["filter_liquidity_enabled"] = False
    quiet = make_meta(liquidity_usd=500.0, volume_h24=10.0)
    result = check_token(quiet, settings)
    assert not result.passed
    assert "volume" in result.reason


def test_volume_switch_off_ignores_the_floor(settings):
    quiet = make_meta(volume_h24=10.0)
    assert not check_token(quiet, settings).passed

    settings["filter_volume_enabled"] = False
    assert check_token(quiet, settings).passed


def test_age_switch_off_ignores_the_limit(settings):
    settings["min_token_age_minutes"] = 600
    fresh = make_meta(pair_created_at=int((time.time() - 60) * 1000))
    assert not check_token(fresh, settings).passed

    settings["filter_age_enabled"] = False
    assert check_token(fresh, settings).passed


def test_liquidity_switch_off_makes_unknown_liquidity_moot(settings):
    """With no liquidity check running, a missing figure cannot fail it."""
    settings["filter_liquidity_enabled"] = False
    settings["unknown_liquidity"] = SKIP
    token = make_meta(liquidity_usd=None, volume_h24=200_000.0)
    result = check_token(token, settings)
    assert result.passed
    assert result.liquidity_unknown


def test_switches_default_to_on(settings):
    for key in ("filter_liquidity_enabled", "filter_volume_enabled", "filter_age_enabled"):
        settings.pop(key, None)
    assert not check_token(make_meta(liquidity_usd=500.0), settings).passed
