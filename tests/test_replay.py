"""Replay real recorded ticks through the detector.

The synthetic tests in test_detector.py pin individual rules; this pins the
whole thing against market data that actually happened. The fixture is 2,378
ticks from four Solana tokens whose drawdowns ranged 69-97%, captured live.

These assert *ranges* rather than exact counts. The point is to catch a change
that makes the detector fire wildly more or stop firing altogether - not to
freeze one specific alert count, which would break on any deliberate tuning.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from dipbot.detector import Detector
from dipbot.models import PriceSource, PriceTick, Tier

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "ticks.jsonl"


def load_ticks() -> list[PriceTick]:
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
    return [
        PriceTick(
            mint=r["mint"],
            price_sol=r["price_sol"],
            ts=r["ts"],
            source=PriceSource.BONDING_CURVE if r["source"] == "curve" else PriceSource.POOL_VAULTS,
            slot=r.get("slot"),
        )
        for r in rows
    ]


def replay(settings: dict) -> tuple[list, Detector]:
    detector = Detector(settings)
    alerts = [
        alert
        for tick in load_ticks()
        if (alert := detector.on_tick(tick, meta=None, tier=Tier.REALTIME))
    ]
    return alerts, detector


@pytest.fixture(scope="module")
def ticks():
    return load_ticks()


def test_fixture_is_present_and_sane(ticks):
    assert len(ticks) > 2000
    assert len({t.mint for t in ticks}) == 4
    assert all(t.price_sol > 0 for t in ticks)
    # recorded in time order, which the detector's windows depend on
    assert all(a.ts <= b.ts for a, b in zip(ticks, ticks[1:]))


def test_default_settings_produce_a_workable_number_of_alerts(settings):
    """Real market, real settings: enough to be useful, few enough to read."""
    alerts, _ = replay(settings)
    assert 1 <= len(alerts) <= 40


def test_raising_thresholds_reduces_alerts(settings):
    low = dict(settings)
    high = dict(settings)
    for w in ("10s", "1m", "3m", "5m"):
        low[f"threshold_migrated_{w}"] = 5.0
        high[f"threshold_migrated_{w}"] = 50.0

    assert len(replay(low)[0]) > len(replay(high)[0])


def test_every_alert_is_a_real_drawdown(settings):
    alerts, _ = replay(settings)
    for alert in alerts:
        assert alert.baseline_sol > alert.price_sol
        assert 0 < alert.drawdown < 100


def test_cooldown_prevents_repeat_alerts_on_the_same_token(settings):
    """These tokens fell 69-97%; without the cooldown each would fire dozens."""
    settings["cooldown_per_token_seconds"] = 600
    alerts, _ = replay(settings)
    per_token: dict[str, int] = {}
    for alert in alerts:
        per_token[alert.mint] = per_token.get(alert.mint, 0) + 1
    assert max(per_token.values()) <= 6


def test_disabling_cooldown_and_rearm_produces_more_alerts(settings):
    loose = dict(settings)
    loose["cooldown_per_token_seconds"] = 0
    loose["rearm_recovery_pct"] = 0
    assert len(replay(loose)[0]) > len(replay(settings)[0])


def test_guards_actually_suppress_something(settings):
    _, detector = replay(settings)
    suppressed = detector.stats()["suppressed"]
    assert suppressed, "no guard fired on real data - they may be misconfigured"
    assert sum(suppressed.values()) > len(load_ticks()) * 0.1


def test_polled_tier_never_uses_fast_windows_on_real_data(settings):
    detector = Detector(settings)
    alerts = [
        alert
        for tick in load_ticks()
        if (alert := detector.on_tick(tick, meta=None, tier=Tier.POLLED))
    ]
    assert all(a.window in ("3m", "5m") for a in alerts)


def test_replay_is_deterministic(settings):
    """Same input, same settings, same alerts - otherwise tuning is guesswork."""
    first = [(a.mint, a.window, round(a.drawdown, 6)) for a in replay(settings)[0]]
    second = [(a.mint, a.window, round(a.drawdown, 6)) for a in replay(settings)[0]]
    assert first == second
