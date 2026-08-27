"""Behaviour of the glue in main.py: mute gating and USD repricing.

These reimplement the tick path rather than importing main(), which needs a live
Telegram session. They exist because both rules were broken in ways no unit test
covered: a polled token ignored its own mute, and USD mode did nothing at all.
"""
from __future__ import annotations

import time
from dataclasses import replace

import pytest

from dipbot.detector import Detector
from dipbot.models import PriceSource, PriceTick, Tier

MINT = "Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump"


def tick(price: float, ts: float) -> PriceTick:
    return PriceTick(mint=MINT, price_sol=price, ts=ts, source=PriceSource.POOL_VAULTS)


class Pipeline:
    """The same gating main.on_tick applies, in one testable place."""

    def __init__(self, settings, sol_usd=None):
        self.detector = Detector(settings)
        self.muted_tokens: dict[str, float] = {}
        self.muted_until = 0.0
        self.sol_usd = sol_usd
        self.seen: list[float] = []

    def feed(self, prices, start=1000.0):
        alerts = []
        for i, price in enumerate(prices):
            t = tick(price, start + i)
            if self.muted_until > time.time():
                continue
            if self.muted_tokens.get(t.mint, 0) > time.time():
                continue
            if str(self.detector.settings.get("quote_mode", "SOL")).upper() == "USD" and self.sol_usd:
                t = replace(t, price_sol=t.price_sol * self.sol_usd)
            self.seen.append(t.price_sol)
            alert = self.detector.on_tick(t, tier=Tier.REALTIME)
            if alert:
                alerts.append(alert)
        return alerts


@pytest.fixture
def settings(settings):
    settings["warmup_seconds"] = 0
    settings["confirm_ticks"] = 0
    for w in ("10s", "1m", "3m", "5m"):
        settings[f"min_ticks_{w}"] = 3
    return settings


def test_alert_fires_when_nothing_is_muted(settings):
    assert len(Pipeline(settings).feed([100] * 5 + [80])) == 1


def test_global_mute_blocks_alerts(settings):
    pipe = Pipeline(settings)
    pipe.muted_until = time.time() + 3600
    assert pipe.feed([100] * 5 + [80]) == []


def test_per_token_mute_blocks_polled_token(settings):
    """The bug: polled tokens keep arriving, so mute must be checked here."""
    pipe = Pipeline(settings)
    pipe.muted_tokens[MINT] = time.time() + 3600
    assert pipe.feed([100] * 5 + [80]) == []


def test_expired_per_token_mute_allows_alerts(settings):
    pipe = Pipeline(settings)
    pipe.muted_tokens[MINT] = time.time() - 1
    assert len(pipe.feed([100] * 5 + [80])) == 1


def test_mute_on_another_token_does_not_block(settings):
    pipe = Pipeline(settings)
    pipe.muted_tokens["some-other-mint"] = time.time() + 3600
    assert len(pipe.feed([100] * 5 + [80])) == 1


def test_sol_mode_leaves_prices_untouched(settings):
    settings["quote_mode"] = "SOL"
    pipe = Pipeline(settings, sol_usd=200.0)
    pipe.feed([100, 100, 100])
    assert pipe.seen == [100, 100, 100]


def test_usd_mode_reprices_by_sol(settings):
    """USD mode must actually convert - it previously did nothing."""
    settings["quote_mode"] = "USD"
    pipe = Pipeline(settings, sol_usd=200.0)
    pipe.feed([100, 100, 100])
    assert pipe.seen == [20_000, 20_000, 20_000]


def test_usd_mode_without_a_sol_price_falls_back_safely(settings):
    settings["quote_mode"] = "USD"
    pipe = Pipeline(settings, sol_usd=None)
    pipe.feed([100, 100])
    assert pipe.seen == [100, 100]


def test_relative_drop_is_unchanged_by_quote_mode(settings):
    """A constant SOL price must not alter the measured drawdown."""
    settings["quote_mode"] = "USD"
    alerts = Pipeline(settings, sol_usd=200.0).feed([100] * 5 + [80])
    assert len(alerts) == 1
    assert alerts[0].drawdown == pytest.approx(20.0)


# --- observed market caps ---------------------------------------------------
# The regression: an alert reported a fall from $122.93k on a coin whose real
# high that minute was $56.39k, because the figure was derived from a stale cap
# times a live price ratio rather than observed.


class FakePoller:
    def __init__(self, history):
        self.history = history

    def observed_caps(self, mint, since):
        return [cap for ts, cap in self.history.get(mint, []) if ts >= since]


def attach_caps(alert, poller, window_seconds):
    """What main.on_tick does after the detector produces an alert."""
    observed = poller.observed_caps(alert.mint, alert.ts - window_seconds)
    alert.mcap_now = observed[-1] if observed else None
    alert.mcap_peak = max(observed) if observed else None
    return alert


def test_caps_come_from_observations_inside_the_window():
    from dipbot.alerts import Alert

    now = 1000.0
    poller = FakePoller({MINT: [(now - 200, 40_000), (now - 50, 100_000), (now - 10, 50_000)]})
    a = Alert(mint=MINT, window="1m", drawdown=50.0, price_sol=1.0, baseline_sol=2.0,
              tier=Tier.REALTIME, ts=now)
    attach_caps(a, poller, 60)

    assert a.mcap_peak == 100_000   # highest seen in the last 60s
    assert a.mcap_now == 50_000     # most recent


def test_observations_outside_the_window_are_ignored():
    from dipbot.alerts import Alert

    now = 1000.0
    poller = FakePoller({MINT: [(now - 500, 900_000), (now - 5, 50_000)]})
    a = Alert(mint=MINT, window="1m", drawdown=50.0, price_sol=1.0, baseline_sol=2.0,
              tier=Tier.REALTIME, ts=now)
    attach_caps(a, poller, 60)

    assert a.mcap_peak == 50_000    # the old 900k is not in the window
    assert a.mcap_now == 50_000


def test_no_observations_leaves_the_caps_unset():
    from dipbot.alerts import Alert
    from dipbot.alerts import render

    a = Alert(mint=MINT, window="1m", drawdown=46.4, price_sol=1.0, baseline_sol=1.87,
              tier=Tier.REALTIME, ts=1000.0)
    attach_caps(a, FakePoller({}), 60)

    assert a.mcap_peak is None
    assert a.mcap_now is None
    assert "→" not in render(a)


# --- quote mode must not corrupt the market cap -----------------------------
# The regression: USD mode multiplies tick prices by the SOL price before the
# detector sees them, but calibration was learned from unscaled SOL prices, so
# every cap came out ~98x too high. A coin worth 171k was published as 27M.


SOL_USD = 98.073
RAW_PRICE = 1.6274e-06
REAL_CAP = 171_394


def calibrate(sanity, price, cap, times=3):
    for _ in range(times):
        sanity.observe_cap("m", price, cap)   # anchoring keeps only the last


def test_sol_mode_cap_is_correct():
    from dipbot.sanity import PriceSanity

    s = PriceSanity()
    calibrate(s, RAW_PRICE, REAL_CAP)
    assert s.market_cap("m", RAW_PRICE) == pytest.approx(REAL_CAP, rel=0.01)


def test_usd_mode_cap_is_correct_when_scales_match():
    """Calibration and detection must use the same scaling."""
    from dipbot.sanity import PriceSanity

    s = PriceSanity()
    calibrate(s, RAW_PRICE * SOL_USD, REAL_CAP)      # calibrated in USD scale
    assert s.market_cap("m", RAW_PRICE * SOL_USD) == pytest.approx(REAL_CAP, rel=0.01)


def test_mismatched_scales_produce_the_original_bug():
    """Documents the failure so the guard below has something to catch."""
    from dipbot.sanity import PriceSanity

    s = PriceSanity()
    calibrate(s, RAW_PRICE, REAL_CAP)                 # calibrated in SOL scale
    wrong = s.market_cap("m", RAW_PRICE * SOL_USD)    # priced in USD scale
    assert wrong / REAL_CAP == pytest.approx(SOL_USD, rel=0.05)


def guard(computed, reported):
    """The check main.on_tick applies before publishing a cap."""
    if not computed or not reported:
        return computed
    drift = computed / reported
    return reported if (drift > 5 or drift < 0.2) else computed


def test_guard_rejects_a_wrongly_scaled_cap():
    assert guard(16_809_124, REAL_CAP) == REAL_CAP


def test_guard_accepts_a_plausible_cap():
    """A real dip moves the cap well within the tolerated band."""
    assert guard(REAL_CAP * 0.6, REAL_CAP) == pytest.approx(REAL_CAP * 0.6)
    assert guard(REAL_CAP * 1.8, REAL_CAP) == pytest.approx(REAL_CAP * 1.8)


def test_guard_passes_through_when_nothing_to_compare():
    assert guard(None, REAL_CAP) is None
    assert guard(REAL_CAP, None) == REAL_CAP
