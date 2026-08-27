"""DexScreener client: token resolution and batched metadata polling.

Measured constraints this code is built around (recorded live via spike/):
  - responses are CDN-cached with max-age=30, so polling faster than ~20s
    returns identical bytes and only burns quota
  - the API 403s python's default User-Agent
  - /latest/dex/search is near-useless for pump-ecosystem discovery and is
    cached 60s; /tokens/v1 is the useful endpoint
  - there is no auth of any kind
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from ..models import BONDING_DEX_IDS, TokenMeta, Tier

log = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"
BATCH = 30
SOL_USDC_PAIR = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"

# Constant-product venues we can price from chain state. Concentrated-liquidity
# pools (Raydium CLMM, Orca Whirlpool, Meteora DLMM) price from sqrtPriceX64 or
# an active bin, not vault ratios, so they stay polled until those decoders exist.
REALTIME_DEX_IDS = BONDING_DEX_IDS | {"pumpswap"}

BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class DexScreenerError(RuntimeError):
    pass


class DexScreener:
    def __init__(self, user_agent: str, timeout: float = 25.0):
        self._client = httpx.AsyncClient(headers={"User-Agent": user_agent}, timeout=timeout)
        self.request_count = 0
        self.error_count = 0
        self.empty_retries = 0
        self.last_error: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None, tries: int = 4):
        delay = 1.0
        for attempt in range(tries):
            try:
                r = await self._client.get(BASE + path, params=params)
                self.request_count += 1
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"HTTP {r.status_code}", request=r.request, response=r
                    )
                r.raise_for_status()
                return r.json()
            except Exception as e:
                self.error_count += 1
                self.last_error = f"{type(e).__name__}: {e}"
                if attempt == tries - 1:
                    raise DexScreenerError(self.last_error) from e
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15)

    @staticmethod
    def _pairs_of(data) -> list[dict]:
        """Normalise the two response shapes to a list of pairs.

        `/tokens/v1` answers with a bare list; `/latest/dex/*` wraps it in an
        object whose `pairs` is null rather than empty when there is nothing.
        """
        if isinstance(data, dict):
            return data.get("pairs") or []
        return data or []

    @staticmethod
    def _rank(pair: dict) -> tuple:
        """Sort key for choosing which pair represents a token.

        Liquidity alone is not enough. A migrated pump.fun coin still has its
        bonding-curve pair listed, frozen at the price it migrated at and with
        no liquidity figure at all - and DexScreener's `/tokens/v1` sometimes
        returns *only* that one. Picking it publishes the migration-moment cap
        as the live cap and leaves the detector watching a price that can never
        move again. A real pool always wins, however thin.
        """
        liq = (pair.get("liquidity") or {}).get("usd") or 0
        vol = (pair.get("volume") or {}).get("h24") or 0
        return (pair.get("dexId") not in BONDING_DEX_IDS, liq, vol)

    async def _get_pairs(self, path: str, bucket: float = 0) -> list[dict]:
        """GET an endpoint returning a list of pairs, defeating cached empties.

        DexScreener intermittently answers a perfectly valid request with HTTP
        200 and an empty list, and Cloudflare caches that empty body for its 30s
        TTL - one URL returned [] five times in a row, then returned all 11 pairs
        once the entry expired. Since that TTL outlives our poll interval, a
        poisoned entry would otherwise starve every cycle.

        `bucket` adds a coarse per-cycle cache key so each poll gets its own URL
        (never a stale empty) while still letting the CDN serve retries within a
        cycle. Callers without a cycle fall back to retrying once.
        """
        params = {"_": str(int(time.time() // bucket))} if bucket else None
        data = self._pairs_of(await self._get(path, params=params))
        if data:
            return data

        # The retry is load-bearing, not paranoia: a valid mint was observed
        # returning 0 pairs on first call and full data on the cache-busted one.
        self.empty_retries += 1
        log.debug("empty response for %s, retrying past cache", path[:60])
        data = self._pairs_of(await self._get(path, params={"_": str(int(time.time() * 1000))}))
        if not data:
            # Benign in two common cases: the address is a pair rather than a
            # mint (callers fall back to the pair endpoint), or the token was
            # delisted. Only genuinely surprising for a known-good watchlist mint.
            log.debug("still empty after cache-bust: %s", path[:80])
        return data or []

    @staticmethod
    def _to_meta(pair: dict) -> TokenMeta:
        liq = pair.get("liquidity") or {}
        vol = pair.get("volume") or {}
        txns = (pair.get("txns") or {}).get("m5") or {}

        def num(v):
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        return TokenMeta(
            mint=pair["baseToken"]["address"],
            pair_address=pair["pairAddress"],
            symbol=pair["baseToken"].get("symbol"),
            name=pair["baseToken"].get("name"),
            dex_id=pair.get("dexId"),
            price_usd=num(pair.get("priceUsd")),
            price_native=num(pair.get("priceNative")),
            liquidity_usd=num(liq.get("usd")),
            volume_h24=num(vol.get("h24")),
            volume_m5=num(vol.get("m5")),
            market_cap=num(pair.get("marketCap")),
            pair_created_at=pair.get("pairCreatedAt"),
            txns_m5_buys=txns.get("buys"),
            txns_m5_sells=txns.get("sells"),
            updated_at=time.time(),
        )

    @staticmethod
    def parse_input(text: str) -> str | None:
        """Pull a mint or pair address out of a raw address or DexScreener URL."""
        text = text.strip()
        if m := re.search(r"dexscreener\.com/solana/([1-9A-HJ-NP-Za-km-z]{32,44})", text):
            return m.group(1)
        if m := re.search(r"pump\.fun/(?:coin/)?([1-9A-HJ-NP-Za-km-z]{32,44})", text):
            return m.group(1)
        if BASE58.match(text):
            return text
        return None

    async def resolve(self, address: str) -> TokenMeta | None:
        """Resolve a mint or pair address to the pair that best represents it.

        `/latest/dex/tokens` is tried first because it returns *every* pool for
        the mint. `/tokens/v1` returns only DexScreener's own pick, which for a
        freshly migrated coin can still be the dead bonding-curve pair - it is
        kept purely as a fallback. Finally the input is treated as a pair
        address.
        """
        pairs: list[dict] = []
        for path in (
            f"/latest/dex/tokens/{address}",
            f"/tokens/v1/solana/{address}",
            f"/latest/dex/pairs/solana/{address}",
        ):
            try:
                pairs = await self._get_pairs(path)
            except DexScreenerError:
                pairs = []
            if pairs:
                break

        solana = [p for p in pairs if p.get("chainId") == "solana"]
        if not solana:
            return None
        return self._to_meta(max(solana, key=self._rank))

    async def _batched(self, prefix: str, keys: list[str], bucket: float) -> dict[str, TokenMeta]:
        """Fetch `keys` in chunks of BATCH, keeping the best pair per mint."""
        out: dict[str, TokenMeta] = {}
        best: dict[str, tuple] = {}
        for i in range(0, len(keys), BATCH):
            chunk = keys[i : i + BATCH]
            try:
                pairs = await self._get_pairs(f"{prefix}{','.join(chunk)}", bucket=bucket)
            except DexScreenerError as e:
                log.warning("batch fetch failed for %d addresses: %s", len(chunk), e)
                continue
            for pair in pairs:
                if pair.get("chainId") != "solana":
                    continue
                meta = self._to_meta(pair)
                rank = self._rank(pair)
                if meta.mint not in best or rank > best[meta.mint]:
                    best[meta.mint] = rank
                    out[meta.mint] = meta
            if i + BATCH < len(keys):
                await asyncio.sleep(0.3)
        return out

    async def fetch_many(self, mints: list[str], bucket: float = 20.0) -> dict[str, TokenMeta]:
        """Batch-fetch metadata by mint, keeping the best pair per mint.

        `bucket` should be the caller's poll interval; see _get_pairs.
        """
        return await self._batched("/tokens/v1/solana/", mints, bucket)

    async def fetch_pairs(self, pair_addresses: list[str], bucket: float = 20.0):
        """Batch-fetch metadata for specific pools, by pair address.

        The watchlist already recorded which pool represents each coin, chosen
        from the full pair list at add time. Asking for that pool by name is the
        only way to keep polling it: the by-mint endpoint re-picks a pair on
        every call and can hand back the frozen bonding-curve one instead.
        """
        return await self._batched("/latest/dex/pairs/solana/", pair_addresses, bucket)

    async def sol_usd(self) -> float | None:
        """SOL/USD from a pinned SOL-USDC pair.

        Needed because chain prices are SOL-denominated. Also why QUOTE_MODE
        defaults to SOL: a 5% SOL move would otherwise look like every token
        dipping at once.
        """
        try:
            data = await self._get(f"/latest/dex/pairs/solana/{SOL_USDC_PAIR}")
        except DexScreenerError:
            return None
        pairs = (data or {}).get("pairs") or []
        if not pairs:
            return None
        try:
            return float(pairs[0]["priceUsd"])
        except (KeyError, TypeError, ValueError):
            return None


def tier_for(meta: TokenMeta) -> Tier:
    """Which feed tier a token qualifies for.

    Only constant-product pump.fun venues can be priced from chain state today,
    so everything else is polled and limited to the 3m/5m windows.
    """
    return Tier.REALTIME if meta.dex_id in REALTIME_DEX_IDS else Tier.POLLED
