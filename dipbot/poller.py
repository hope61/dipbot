"""DexScreener metadata poller.

Refreshes watchlist metadata on a fixed interval and emits polled price ticks for
tokens that have no realtime feed. The interval is bounded below by DexScreener's
30s CDN cache: polling faster returns identical bytes.

Phase 1 stores metadata and publishes ticks. The detector that consumes them
arrives in Phase 3.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from .db import Database
from .feeds.dexscreener import DexScreener, tier_for
from collections import deque

from .models import PriceSource, PriceTick

log = logging.getLogger(__name__)

TickHandler = Callable[[PriceTick], Awaitable[None]]


class Poller:
    def __init__(
        self,
        db: Database,
        dex: DexScreener,
        interval: float = 20.0,
        on_tick: TickHandler | None = None,
        on_meta=None,
    ):
        self.db = db
        self.dex = dex
        self.interval = max(interval, 15.0)  # below this the CDN just replays bytes
        self.on_tick = on_tick
        #: Called with each refreshed TokenMeta, for cross-checks.
        self.on_meta = on_meta
        self.last_run: float = 0.0
        self.run_count = 0
        self.sol_usd: float | None = None
        self._sol_refreshed: float = 0.0
        # Consecutive polls in which a watchlist mint returned no data. One miss
        # is routine (DexScreener returns spurious empties); a sustained streak
        # means the token was delisted or the pair moved.
        self.misses: dict[str, int] = {}
        #: Observed market caps per mint: (timestamp, cap). Alerts quote these
        #: rather than deriving a figure, so every number shown actually
        #: happened. Resolution is the poll interval.
        self.cap_history: dict[str, deque] = {}

    async def refresh_sol(self) -> None:
        """SOL/USD is needed to render USD figures from SOL-denominated prices."""
        if time.time() - self._sol_refreshed < 30:
            return
        price = await self.dex.sol_usd()
        if price:
            self.sol_usd = price
            self._sol_refreshed = time.time()

    async def run_once(self) -> int:
        tokens = await self.db.list_tokens(enabled_only=True)
        if not tokens:
            return 0

        await self.refresh_sol()

        # Poll the exact pools the watchlist recorded. Asking by mint instead
        # lets DexScreener re-pick the pair each cycle, and for a migrated
        # pump.fun coin its pick can be the abandoned bonding-curve pair -
        # frozen price, frozen cap, no dip can ever be detected.
        metas = await self.dex.fetch_pairs(
            [t.pair_address for t in tokens if t.pair_address], bucket=self.interval
        )
        missing = [t.mint for t in tokens if t.mint not in metas]
        if missing:
            metas.update(await self.dex.fetch_many(missing, bucket=self.interval))

        for token in tokens:
            meta = metas.get(token.mint)
            # A coin still on its launch curve migrates exactly once. Re-resolve
            # it so the poll follows it onto its real pool the moment it does.
            if meta is not None and meta.is_bonding_curve:
                try:
                    resolved = await self.dex.resolve(token.mint)
                except Exception:
                    resolved = None
                if resolved and not resolved.is_bonding_curve:
                    log.info(
                        "%s migrated %s -> %s", resolved.display_symbol,
                        meta.dex_id, resolved.dex_id,
                    )
                    meta = resolved
            if not meta:
                self.misses[token.mint] = self.misses.get(token.mint, 0) + 1
                if self.misses[token.mint] in (3, 10, 30):
                    log.warning(
                        "%s missing from %d consecutive polls - delisted or pair moved?",
                        token.symbol or token.mint[:8], self.misses[token.mint],
                    )
                continue
            self.misses.pop(token.mint, None)
            await self.db.upsert_meta(meta)

            if self.on_meta:
                await self.on_meta(meta)

            if meta.market_cap:
                history = self.cap_history.setdefault(token.mint, deque(maxlen=120))
                history.append((time.time(), meta.market_cap))

            # A token can migrate from bonding curve to pool mid-life, which
            # changes both its dex_id and which windows it can support. The pair
            # is written back even when the tier is unchanged - pump -> pumpswap
            # is a new pool at the same tier, and polling the old one forever is
            # what froze CRYPTO at its migration-moment cap.
            new_tier = tier_for(meta)
            if new_tier is not token.tier or meta.pair_address != token.pair_address:
                if new_tier is not token.tier:
                    log.info(
                        "%s tier %s -> %s", meta.display_symbol,
                        token.tier.value, new_tier.value,
                    )
                if meta.pair_address != token.pair_address:
                    log.info(
                        "%s pair %s -> %s", meta.display_symbol,
                        (token.pair_address or "")[:8], meta.pair_address[:8],
                    )
                await self.db.conn.execute(
                    "UPDATE watchlist SET tier = ?, pair_address = ? WHERE mint = ?",
                    (new_tier.value, meta.pair_address, token.mint),
                )
                await self.db.conn.commit()

            if self.on_tick and meta.price_native:
                await self.on_tick(
                    PriceTick.now(meta.mint, meta.price_native, PriceSource.DEXSCREENER)
                )

        self.last_run = time.time()
        self.run_count += 1
        return len(metas)

    def observed_caps(self, mint: str, since: float) -> list[float]:
        """Market caps seen for `mint` at or after `since`."""
        return [cap for ts, cap in self.cap_history.get(mint, ()) if ts >= since]

    async def run_forever(self) -> None:
        log.info("poller started, interval %.0fs", self.interval)
        while True:
            started = time.time()
            try:
                n = await self.run_once()
                log.debug("polled %d tokens", n)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll cycle failed")
            await asyncio.sleep(max(1.0, self.interval - (time.time() - started)))
