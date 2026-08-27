"""All-time high, from GeckoTerminal's OHLCV history.

DexScreener publishes no historical peak - the figure on their website is not
in the API. Without a history source the best we could do was "highest since
the bot started watching", which understates for any coin added after its top.

GeckoTerminal serves free candles for the same pools. Reading the highest high
gives a genuine peak for the pool's whole life.

The API is unauthenticated and rate limited around 30 calls/minute, and each
peak costs two calls - so callers space their lookups out rather than fetching
a whole watchlist back to back.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.geckoterminal.com/api/v2"

#: Coarse first: an hourly candle's high still captures an intra-hour spike,
#: and 1000 of them covers 41 days - longer than these coins tend to live.
#: The 5-minute pass catches pools too young to have hourly candles yet.
TIMEFRAMES = (("hour", 1), ("minute", 5))

#: A rate-limited lookup is retried rather than abandoned - see peak_price_usd.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF = 5.0


class GeckoTerminal:
    def __init__(self, timeout: float = 20.0):
        self._client = httpx.AsyncClient(
            timeout=timeout, headers={"Accept": "application/json"}
        )
        self.request_count = 0
        self.error_count = 0
        self.rate_limited = 0
        self.last_error: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def peak_price_usd(self, pair_address: str) -> float | None:
        """Highest price the pool has ever traded at, in USD.

        Returns None if the pool is unknown to GeckoTerminal, which is normal
        for one created moments ago.
        """
        best: float | None = None

        for timeframe, aggregate in TIMEFRAMES:
            candles = None
            # A 429 is worth waiting out rather than abandoning: giving up here
            # leaves the coin with no peak at all, which reads as "its ATH is
            # its current price".
            for attempt in range(RATE_LIMIT_RETRIES):
                try:
                    r = await self._client.get(
                        f"{BASE}/networks/solana/pools/{pair_address}/ohlcv/{timeframe}",
                        params={
                            "aggregate": aggregate,
                            "limit": 1000,
                            "currency": "usd",
                            "token": "base",
                        },
                    )
                    self.request_count += 1
                    if r.status_code == 404:
                        return None
                    if r.status_code == 429 and attempt < RATE_LIMIT_RETRIES - 1:
                        self.rate_limited += 1
                        await asyncio.sleep(RATE_LIMIT_BACKOFF * (attempt + 1))
                        continue
                    r.raise_for_status()
                    candles = r.json()["data"]["attributes"]["ohlcv_list"]
                    break
                except Exception as e:
                    self.error_count += 1
                    self.last_error = f"{type(e).__name__}: {e}"
                    log.debug(
                        "geckoterminal %s failed for %s: %s", timeframe, pair_address[:8], e
                    )
                    break
            if candles is None:
                continue

            # [timestamp, open, high, low, close, volume]
            highs = [c[2] for c in candles if c and c[2]]
            if highs:
                best = max(best or 0.0, max(highs))

            await asyncio.sleep(0.5)  # stay well inside the rate limit

        return best

    async def peak_market_cap(self, pair_address: str, supply: float) -> float | None:
        """The pool's all-time high expressed as a market cap."""
        if supply <= 0:
            return None
        peak = await self.peak_price_usd(pair_address)
        return peak * supply if peak else None


def implied_supply(market_cap: float | None, price_usd: float | None) -> float | None:
    """Circulating supply, from two figures in the same DexScreener snapshot.

    Same-snapshot division cancels any staleness: whatever moment the pair was
    priced at, cap and price came from it together.
    """
    if not market_cap or not price_usd:
        return None
    return market_cap / price_usd
