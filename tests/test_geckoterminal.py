"""All-time high from GeckoTerminal.

DexScreener has no ATH field, so without this the peak is only "highest since
the bot started watching" - which understates for any coin added after its top.
The HTTP layer is faked; the live behaviour was verified against two real coins.
"""
from __future__ import annotations

import pytest

from dipbot.feeds.geckoterminal import GeckoTerminal, implied_supply


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "error"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def candles(*highs):
    # [timestamp, open, high, low, close, volume]
    return {"data": {"attributes": {"ohlcv_list": [[0, 0, h, 0, 0, 0] for h in highs]}}}


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def get(self, url, params=None):
        self.calls += 1
        return self._responses.pop(0) if self._responses else FakeResponse(candles(), 200)

    async def aclose(self):
        pass


def build(responses) -> GeckoTerminal:
    gecko = GeckoTerminal()
    gecko._client = FakeClient(responses)
    return gecko


# --- supply -----------------------------------------------------------------


def test_implied_supply_divides_same_snapshot_figures():
    assert implied_supply(171_394.0, 0.0001753) == pytest.approx(977_718_197, rel=0.01)


def test_implied_supply_needs_both_figures():
    assert implied_supply(None, 0.1) is None
    assert implied_supply(100.0, None) is None
    assert implied_supply(100.0, 0.0) is None


# --- peak price -------------------------------------------------------------


@pytest.mark.asyncio
async def test_peak_is_the_highest_high_seen():
    gecko = build([FakeResponse(candles(1.0, 5.0, 3.0)), FakeResponse(candles(2.0))])
    assert await gecko.peak_price_usd("pool") == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_both_timeframes_are_considered():
    """A pool too young for hourly candles still has 5-minute ones."""
    gecko = build([FakeResponse(candles()), FakeResponse(candles(7.0))])
    assert await gecko.peak_price_usd("pool") == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_unknown_pool_returns_none():
    """Normal for a pool created moments ago - not an error."""
    gecko = build([FakeResponse(None, 404)])
    assert await gecko.peak_price_usd("pool") is None


@pytest.mark.asyncio
async def test_a_failing_timeframe_does_not_lose_the_other():
    gecko = build([FakeResponse(None, 500), FakeResponse(candles(4.0))])
    assert await gecko.peak_price_usd("pool") == pytest.approx(4.0)
    assert gecko.error_count == 1


@pytest.mark.asyncio
async def test_no_candles_returns_none():
    gecko = build([FakeResponse(candles()), FakeResponse(candles())])
    assert await gecko.peak_price_usd("pool") is None


@pytest.mark.asyncio
async def test_zero_highs_are_ignored():
    gecko = build([FakeResponse(candles(0, 0)), FakeResponse(candles(0))])
    assert await gecko.peak_price_usd("pool") is None


# --- peak market cap --------------------------------------------------------


@pytest.mark.asyncio
async def test_peak_market_cap_multiplies_by_supply():
    gecko = build([FakeResponse(candles(0.00017634855)), FakeResponse(candles())])
    peak = await gecko.peak_market_cap("pool", 1_000_137_457)
    assert peak == pytest.approx(176_373, rel=0.01)   # measured on a live coin


@pytest.mark.asyncio
async def test_peak_market_cap_needs_a_supply():
    gecko = build([FakeResponse(candles(1.0)), FakeResponse(candles())])
    assert await gecko.peak_market_cap("pool", 0) is None


@pytest.mark.asyncio
async def test_peak_market_cap_is_none_without_history():
    gecko = build([FakeResponse(None, 404)])
    assert await gecko.peak_market_cap("pool", 1_000_000) is None


# --- rate limiting ----------------------------------------------------------
#
# Regression: the whole watchlist was fetched back to back, GeckoTerminal
# rate limited most of it, and every 429 was swallowed - so three coins ended up
# with an ATH exactly equal to their market cap.


@pytest.mark.asyncio
async def test_rate_limited_lookup_is_retried(monkeypatch):
    monkeypatch.setattr("dipbot.feeds.geckoterminal.RATE_LIMIT_BACKOFF", 0.0)
    gecko = build([
        FakeResponse(None, 429),
        FakeResponse(candles(0.001, 0.004), 200),  # hourly, after the wait
        FakeResponse(candles(0.002), 200),  # 5-minute pass
    ])

    assert await gecko.peak_price_usd("pool") == pytest.approx(0.004)
    assert gecko.rate_limited == 1
    await gecko.close()


@pytest.mark.asyncio
async def test_persistent_rate_limiting_gives_up_and_reports(monkeypatch):
    monkeypatch.setattr("dipbot.feeds.geckoterminal.RATE_LIMIT_BACKOFF", 0.0)
    gecko = build([FakeResponse(None, 429)] * 6)

    assert await gecko.peak_price_usd("pool") is None
    assert "429" in (gecko.last_error or "")
    await gecko.close()


@pytest.mark.asyncio
async def test_unknown_pool_is_not_retried():
    """404 means GeckoTerminal has never seen the pool - waiting cannot help."""
    gecko = build([FakeResponse(None, 404), FakeResponse(candles(9.9), 200)])

    assert await gecko.peak_price_usd("pool") is None
    assert gecko._client.calls == 1
    await gecko.close()
