"""Detector behaviour.

Each guard here is pinned because Phase 0 measured the failure it prevents:
the window cascade (one dump, four messages), alerts on four ticks, phantom
dips from a bogus high print, and a fresh buffer reading as a collapse.
"""
from __future__ import annotations

import pytest

from dipbot.detector import Detector, _robust_high
from dipbot.models import PriceSource, PriceTick, Tier
from tests.conftest import make_meta

MINT = "Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump"


def tick(price: float, ts: float, mint: str = MINT, source=PriceSource.POOL_VAULTS) -> PriceTick:
    return PriceTick(mint=mint, price_sol=price, ts=ts, source=source, slot=int(ts))


@pytest.fixture
def settings(settings):
    """No warmup or confirmation unless a test asks for them."""
    settings["warmup_seconds"] = 0
    settings["confirm_ticks"] = 0
    settings["min_ticks_10s"] = 3
    settings["min_ticks_1m"] = 3
    settings["min_ticks_3m"] = 3
    settings["min_ticks_5m"] = 3
    return settings


def feed(detector: Detector, prices, start=1000.0, step=1.0, source=PriceSource.POOL_VAULTS, **kw):
    """Push a price series and return every alert produced."""
    alerts = []
    for i, price in enumerate(prices):
        alert = detector.on_tick(tick(price, start + i * step, source=source), **kw)
        if alert:
            alerts.append(alert)
    return alerts


# --- core behaviour ---------------------------------------------------------


def test_flat_prices_never_alert(settings):
    assert feed(Detector(settings), [100.0] * 20) == []


def test_rise_never_alerts(settings):
    assert feed(Detector(settings), [100 + i for i in range(20)]) == []


def test_drop_from_rolling_high_alerts(settings):
    alerts = feed(Detector(settings), [100, 100, 100, 100, 80])
    assert len(alerts) == 1
    assert alerts[0].drawdown == pytest.approx(20.0)
    assert alerts[0].baseline_sol == 100


def test_pump_then_dump_is_measured_from_the_peak(settings):
    """The case that motivates rolling-high: ends above its start, still a dip."""
    alerts = feed(Detector(settings), [100, 120, 140, 140, 112])
    assert len(alerts) == 1
    assert alerts[0].baseline_sol == 140
    assert alerts[0].drawdown == pytest.approx(20.0)


def test_shallow_drop_below_threshold_is_ignored(settings):
    # migrated 10s threshold is 8%
    assert feed(Detector(settings), [100, 100, 100, 97]) == []


# --- the cascade ------------------------------------------------------------


def test_one_dump_produces_one_alert(settings):
    """PKG fired 10s, 3m and 5m at an identical -22.5% from a single tick."""
    alerts = feed(Detector(settings), [100] * 10 + [60] * 10)
    assert len(alerts) == 1


def test_fastest_window_wins(settings):
    alerts = feed(Detector(settings), [100] * 5 + [60])
    assert alerts[0].window == "10s"


def test_slower_window_used_when_fast_one_disabled(settings):
    """A slide spread over a minute still trips a longer window."""
    settings["enabled_windows"] = "1m,3m,5m"
    detector = Detector(settings)
    prices = [100 - i * 0.6 for i in range(40)]  # ~24% over 40s, gentle per tick
    alerts = feed(detector, prices, step=1.0)
    assert alerts
    assert alerts[0].window == "1m"


# --- cooldown and re-arm ----------------------------------------------------


def test_cooldown_blocks_second_alert(settings):
    settings["cooldown_per_token_seconds"] = 600
    alerts = feed(Detector(settings), [100] * 5 + [60] * 5 + [100] * 5 + [50] * 5)
    assert len(alerts) == 1


def test_alert_allowed_after_cooldown_expires(settings):
    settings["cooldown_per_token_seconds"] = 10
    settings["rearm_recovery_pct"] = 0
    detector = Detector(settings)
    alerts = feed(detector, [100] * 5 + [60])
    assert len(alerts) == 1
    # much later, another fall
    alerts += feed(detector, [100] * 5 + [60], start=2000.0)
    assert len(alerts) == 2


def test_rearm_requires_recovery(settings):
    """A coin grinding down must not alert every time the cooldown lapses."""
    settings["cooldown_per_token_seconds"] = 0
    settings["rearm_recovery_pct"] = 50
    detector = Detector(settings)
    alerts = feed(detector, [100] * 5 + [60])
    assert len(alerts) == 1
    # keeps sliding without recovering - no further alerts
    alerts += feed(detector, [55, 50, 45, 40], start=1100.0)
    assert len(alerts) == 1


def test_rearm_clears_after_bounce(settings):
    settings["cooldown_per_token_seconds"] = 0
    settings["rearm_recovery_pct"] = 50
    detector = Detector(settings)
    feed(detector, [100] * 5 + [60])
    # recover past 60 + (100-60)*0.5 = 80, then fall again
    alerts = feed(detector, [85, 100, 100, 100, 100, 60], start=1100.0)
    assert len(alerts) == 1


# --- min ticks --------------------------------------------------------------


def test_window_ignored_without_enough_ticks(settings):
    """A 3m alert fired on exactly 4 ticks in testing."""
    settings["min_ticks_10s"] = 10
    settings["min_ticks_1m"] = 10
    settings["min_ticks_3m"] = 10
    settings["min_ticks_5m"] = 10
    assert feed(Detector(settings), [100, 100, 100, 50]) == []


def test_alert_fires_once_min_ticks_met(settings):
    settings["min_ticks_10s"] = 4
    alerts = feed(Detector(settings), [100, 100, 100, 100, 50])
    assert len(alerts) == 1


# --- warmup -----------------------------------------------------------------


def test_warmup_suppresses_early_alerts(settings):
    """An empty buffer would otherwise read as a total collapse."""
    settings["warmup_seconds"] = 60
    assert feed(Detector(settings), [100, 100, 100, 50]) == []


def test_alert_allowed_after_warmup(settings):
    settings["warmup_seconds"] = 5
    alerts = feed(Detector(settings), [100] * 10 + [50])
    assert len(alerts) == 1


def test_feed_gap_restarts_warmup(settings):
    """After a gap the buffer no longer describes the present."""
    settings["warmup_seconds"] = 30
    detector = Detector(settings)
    feed(detector, [100] * 40)
    # jump far ahead, as a reconnect would
    alerts = feed(detector, [100, 100, 100, 50], start=9000.0)
    assert alerts == []


# --- confirmation -----------------------------------------------------------


def test_confirmation_delays_alert_by_one_tick(settings):
    settings["confirm_ticks"] = 1
    detector = Detector(settings)
    assert feed(detector, [100] * 5 + [60]) == []  # first sighting only
    alerts = feed(detector, [60], start=1006.0)
    assert len(alerts) == 1


def test_single_bad_print_is_not_alerted_when_confirmation_on(settings):
    settings["confirm_ticks"] = 1
    alerts = feed(Detector(settings), [100] * 5 + [60] + [100] * 3)
    assert alerts == []


# --- outlier-resistant baseline --------------------------------------------


def test_robust_high_ignores_lone_spike():
    prices = [100, 100, 101, 99, 100, 100, 500]
    assert _robust_high(prices, 3.0) == 101


def test_robust_high_keeps_genuine_high():
    prices = [100, 102, 104, 106, 108, 110]
    assert _robust_high(prices, 3.0) == 110


def test_robust_high_falls_back_to_max_on_short_series():
    assert _robust_high([100, 500], 3.0) == 500


def test_bogus_spike_does_not_manufacture_a_dip(settings):
    """One bad high print would otherwise inflate the baseline into a dip."""
    alerts = feed(Detector(settings), [100, 100, 100, 100, 100, 1000, 95])
    assert alerts == []


# --- profiles and tiers -----------------------------------------------------


def test_bonding_curve_ticks_never_alert(settings):
    """Pre-bond coins are not tracked; a 50% launch-curve swing is normal."""
    alerts = feed(Detector(settings), [100] * 5 + [50], source=PriceSource.BONDING_CURVE)
    assert alerts == []


def test_bonding_curve_suppression_is_reported(settings):
    detector = Detector(settings)
    feed(detector, [100] * 5 + [50], source=PriceSource.BONDING_CURVE)
    assert detector.stats()["suppressed"].get("not bonded yet")


def test_bonded_coin_alerts_normally(settings):
    alerts = feed(Detector(settings), [100] * 5 + [90])
    assert len(alerts) == 1
    assert alerts[0].drawdown == pytest.approx(10.0)


def test_metadata_saying_bonding_curve_also_silences(settings):
    """A polled coin whose dexId is still `pump` has not bonded either."""
    curve_meta = make_meta(dex_id="pump")
    alerts = feed(Detector(settings), [100] * 5 + [50], meta=curve_meta, tier=Tier.POLLED)
    assert alerts == []


def test_a_coin_that_bonds_starts_alerting(settings):
    """Same token, same detector: silent on the curve, live once bonded."""
    detector = Detector(settings)
    assert feed(detector, [100] * 5 + [50], source=PriceSource.BONDING_CURVE) == []
    alerts = feed(detector, [100] * 5 + [50], start=2000.0, source=PriceSource.POOL_VAULTS)
    assert len(alerts) == 1


def test_polled_tier_cannot_use_fast_windows(settings):
    """30s-stale data must never produce a 10s or 1m alert."""
    alerts = feed(Detector(settings), [100] * 5 + [60], tier=Tier.POLLED)
    assert all(a.window in ("3m", "5m") for a in alerts)


def test_disabled_window_is_skipped(settings):
    settings["enabled_windows"] = "3m,5m"
    alerts = feed(Detector(settings), [100] * 5 + [60])
    assert all(a.window in ("3m", "5m") for a in alerts)


# --- filters ----------------------------------------------------------------


def test_filtered_token_never_alerts(settings):
    thin = make_meta(liquidity_usd=100.0)
    alerts = feed(Detector(settings), [100] * 5 + [50], meta=thin)
    assert alerts == []


def test_unknown_liquidity_token_still_alerts(settings, memedex):
    """MEMEDEX-style coin: no liquidity figure, real volume, must still work."""
    alerts = feed(Detector(settings), [100] * 5 + [50], meta=memedex)
    assert len(alerts) == 1


# --- isolation and reporting ------------------------------------------------


def test_tokens_do_not_share_state(settings):
    detector = Detector(settings)
    other = "JDbm8VXgqaoVsPNhBzRt8wkuJtWdmC75Xfa1Z9a4pump"
    for i, price in enumerate([100] * 5 + [60]):
        detector.on_tick(tick(price, 1000 + i))
        detector.on_tick(tick(100, 1000 + i, mint=other))
    assert len(detector.states) == 2
    assert detector.states[other].last_alert_at == 0


def test_stats_report_suppression_reasons(settings):
    settings["warmup_seconds"] = 60
    detector = Detector(settings)
    feed(detector, [100, 100, 100, 50])
    assert detector.stats()["suppressed"].get("warmup")
    assert detector.stats()["tokens"] == 1


# --- per-token overrides ----------------------------------------------------


QUIET = {f"threshold_migrated_{w}": 50.0 for w in ("10s", "1m", "3m", "5m")}


def test_override_raises_threshold_for_one_token(settings):
    """A coin that behaves unlike its profile can be tuned alone."""
    detector = Detector(settings)
    detector.overrides[MINT] = dict(QUIET)
    assert feed(detector, [100] * 5 + [80]) == []


def test_override_applies_per_window(settings):
    """Overriding only 10s leaves the slower windows on shared settings."""
    detector = Detector(settings)
    detector.overrides[MINT] = {"threshold_migrated_10s": 50.0}
    alerts = feed(detector, [100] * 5 + [80])
    assert len(alerts) == 1
    assert alerts[0].window == "1m"


def test_override_lowers_threshold_for_one_token(settings):
    detector = Detector(settings)
    detector.overrides[MINT] = {"threshold_migrated_10s": 2.0}
    alerts = feed(detector, [100] * 5 + [97])
    assert len(alerts) == 1


def test_override_does_not_affect_other_tokens(settings):
    other = "JDbm8VXgqaoVsPNhBzRt8wkuJtWdmC75Xfa1Z9a4pump"
    detector = Detector(settings)
    detector.overrides[MINT] = dict(QUIET)

    for i, price in enumerate([100] * 5 + [80]):
        detector.on_tick(tick(price, 1000 + i))
        detector.on_tick(tick(price, 1000 + i, mint=other))

    assert detector.states[MINT].last_alert_at == 0
    assert detector.states[other].last_alert_at > 0


def test_effective_merges_over_shared_settings(settings):
    detector = Detector(settings)
    detector.overrides[MINT] = {"threshold_migrated_10s": 42.0}
    merged = detector.effective(MINT)
    assert merged["threshold_migrated_10s"] == 42.0
    assert merged["cooldown_per_token_seconds"] == settings["cooldown_per_token_seconds"]


def test_effective_returns_shared_settings_when_no_override(settings):
    detector = Detector(settings)
    assert detector.effective(MINT) is settings


def test_override_can_disable_a_window(settings):
    detector = Detector(settings)
    detector.overrides[MINT] = {"enabled_windows": "5m"}
    alerts = feed(detector, [100] * 5 + [80])
    assert alerts == []


def test_override_cooldown_is_respected(settings):
    detector = Detector(settings)
    detector.overrides[MINT] = {"cooldown_per_token_seconds": 0, "rearm_recovery_pct": 0}
    alerts = feed(detector, [100] * 5 + [80] + [100] * 5 + [80], step=1.0)
    assert len(alerts) >= 2


def test_stats_report_override_count(settings):
    detector = Detector(settings)
    detector.overrides[MINT] = {"threshold_migrated_1m": 5.0}
    assert detector.stats()["overrides"] == 1
