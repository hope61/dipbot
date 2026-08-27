"""Cross-checking our chain price against DexScreener.

This is the guard against publishing wrong numbers. It has to be sensitive
enough to catch a broken account mapping (measured 70x on a live coin) and dull
enough not to trip during a genuine crash, when our live price legitimately
diverges from DexScreener's 30-second-old one.
"""
from __future__ import annotations

import pytest

from dipbot.sanity import MIN_SAMPLES, PriceSanity


def feed_ratio(sanity: PriceSanity, mint: str, ratio: float, times: int = MIN_SAMPLES) -> None:
    for _ in range(times):
        sanity.observe(mint, ratio * 1e-6, 1e-6)


def test_unknown_token_is_trusted_by_default():
    """Absence of evidence must not block alerts."""
    assert PriceSanity().is_trusted("never-seen")


def test_agreeing_prices_stay_trusted():
    s = PriceSanity()
    feed_ratio(s, "good", 1.06)   # measured on a healthy PumpSwap pool
    assert s.is_trusted("good")


def test_a_broken_mapping_is_distrusted():
    """The real case: our price was 70x DexScreener's on a live coin."""
    s = PriceSanity()
    feed_ratio(s, "bad", 70.3)
    assert not s.is_trusted("bad")
    assert s.ratio("bad") == pytest.approx(70.3)


def test_inverted_disagreement_is_also_caught():
    s = PriceSanity()
    feed_ratio(s, "bad", 1 / 70)
    assert not s.is_trusted("bad")


def test_one_sample_is_never_enough():
    """A single reading proves nothing - DexScreener lags by up to 30s."""
    s = PriceSanity()
    s.observe("x", 70e-6, 1e-6)
    assert s.is_trusted("x")


def test_a_genuine_crash_does_not_trip_it():
    """During a 50% fall our live price diverges from a stale reference."""
    s = PriceSanity()
    for ratio in (1.05, 1.02, 0.5, 1.04, 1.01, 0.98):
        s.observe("crash", ratio * 1e-6, 1e-6)
    assert s.is_trusted("crash")


def test_recovery_restores_trust():
    """A coin that bonds mid-watch may disagree briefly, then settle."""
    s = PriceSanity()
    feed_ratio(s, "t", 70.0, times=10)
    assert not s.is_trusted("t")

    feed_ratio(s, "t", 1.0, times=10)
    assert s.is_trusted("t")


def test_zero_or_negative_prices_are_ignored():
    s = PriceSanity()
    for _ in range(5):
        s.observe("t", 0.0, 1e-6)
        s.observe("t", 1e-6, 0.0)
        s.observe("t", -1.0, 1e-6)
    assert s.is_trusted("t")
    assert s.ratio("t") is None


def test_tokens_are_judged_independently():
    s = PriceSanity()
    feed_ratio(s, "bad", 70.0)
    feed_ratio(s, "good", 1.0)
    assert not s.is_trusted("bad")
    assert s.is_trusted("good")


def test_forget_clears_state():
    s = PriceSanity()
    feed_ratio(s, "bad", 70.0)
    s.forget("bad")
    assert s.is_trusted("bad")
    assert s.ratio("bad") is None


def test_stats_expose_distrusted_tokens():
    s = PriceSanity()
    feed_ratio(s, "bad", 70.0)
    stats = s.stats()
    assert "bad" in stats["distrusted"]
    assert stats["tracked"] >= 1


def test_threshold_is_configurable():
    strict = PriceSanity(max_disagreement=1.5)
    feed_ratio(strict, "t", 2.0)
    assert not strict.is_trusted("t")

    loose = PriceSanity(max_disagreement=100)
    feed_ratio(loose, "t", 2.0)
    assert loose.is_trusted("t")


def test_history_is_bounded():
    """A long-running bot must not accumulate unbounded samples."""
    s = PriceSanity()
    feed_ratio(s, "t", 1.0, times=500)
    assert len(s._ratios["t"]) <= 10


# --- market cap anchoring ---------------------------------------------------
# A quoted cap is the chart's number moved by our own price change since it was
# taken. That keeps it matching what people check against, while still updating
# per trade rather than per poll.


def test_no_anchor_means_no_cap():
    """Absence of data must not produce a guess."""
    assert PriceSanity().market_cap("m", 1e-6) is None


def test_cap_matches_the_reported_one_at_rest():
    s = PriceSanity()
    s.observe_cap("m", 1.0e-06, 100_000)
    assert s.market_cap("m", 1.0e-06) == pytest.approx(100_000)


def test_cap_tracks_the_live_price_exactly():
    """A 31% fall in price is a 31% fall in cap, by construction."""
    s = PriceSanity()
    s.observe_cap("m", 1.0e-06, 100_000)
    assert s.market_cap("m", 0.69e-06) == pytest.approx(69_000)
    assert s.market_cap("m", 1.20e-06) == pytest.approx(120_000)


def test_a_fresh_anchor_replaces_the_old_one():
    """No averaging with stale pairs - that is what made caps drift high."""
    s = PriceSanity()
    s.observe_cap("m", 1.0e-06, 100_000)
    s.observe_cap("m", 0.5e-06, 50_000)
    assert s.market_cap("m", 0.5e-06) == pytest.approx(50_000)


def test_anchoring_never_compounds_drift():
    """The 187k-on-a-165k-coin regression: repeated polls must not inflate."""
    s = PriceSanity()
    cap_per_price = 100_000_000_000
    for price in (1.0e-06, 8.0e-07, 6.5e-07, 5.5e-07, 5.0e-07):
        s.observe_cap("m", price, price * cap_per_price)
        assert s.market_cap("m", price) == pytest.approx(price * cap_per_price)


def test_anchor_ignores_junk():
    s = PriceSanity()
    s.observe_cap("m", 0.0, 100_000)
    s.observe_cap("m", 1e-6, 0.0)
    assert s.market_cap("m", 1e-6) is None


def test_anchors_are_per_coin():
    s = PriceSanity()
    s.observe_cap("a", 1e-6, 100_000)
    s.observe_cap("b", 1e-6, 900_000)
    assert s.market_cap("a", 1e-6) == pytest.approx(100_000)
    assert s.market_cap("b", 1e-6) == pytest.approx(900_000)


def test_forget_clears_the_anchor():
    s = PriceSanity()
    s.observe_cap("m", 1e-6, 100_000)
    s.forget("m")
    assert s.market_cap("m", 1e-6) is None


def test_stats_count_anchored_coins():
    s = PriceSanity()
    s.observe_cap("m", 1e-6, 100_000)
    assert s.stats()["calibrated"] == 1
