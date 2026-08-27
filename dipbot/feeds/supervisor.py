"""Keeps the realtime feed's subscriptions in step with the watchlist.

Runs on a slow loop rather than reacting to commands, so it also recovers from
mid-life changes nobody told it about - a token graduating from bonding curve to
pool being the common one.

Tokens that resolve get tier REALTIME; the rest stay POLLED and are served by
DexScreener at 3m/5m only.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..db import Database
from ..models import Tier
from .pool_resolver import PoolRef, PoolResolver
from .rpc_feed import RpcFeed

log = logging.getLogger(__name__)

SYNC_INTERVAL = 30.0
RERESOLVE_AFTER = 3600.0  # re-check a token we could not price this often
#: Pre-bond coins are re-checked more often than unsupported ones - bonding can
#: happen at any moment, and the check is a couple of RPC reads.
BOND_RECHECK = 300.0


class FeedSupervisor:
    def __init__(self, db: Database, resolver: PoolResolver, feed: RpcFeed):
        self.db = db
        self.resolver = resolver
        self.feed = feed
        self._resolved_at: dict[str, float] = {}
        self._unsupported: dict[str, float] = {}
        #: Coins still on their launch curve. Not watched and never alerted on;
        #: re-checked until they bond.
        self._awaiting_bond: dict[str, float] = {}
        self.sync_count = 0

    @property
    def awaiting_bond(self) -> set[str]:
        return set(self._awaiting_bond)

    async def _load_ref(self, mint: str, pair_address: str) -> PoolRef | None:
        """Cache-first resolution; the cache is what keeps HTTP cost near zero."""
        cached = await self.db.get_pool_ref(mint)
        if cached:
            try:
                return PoolRef.from_row(cached)
            except Exception:
                await self.db.delete_pool_ref(mint)

        ref = await self.resolver.resolve(mint, pair_address)
        # Pre-bond refs are deliberately not cached: the coin is expected to
        # bond, and a cached curve ref would be served forever afterwards,
        # leaving the token stuck and never tracked.
        if ref and not ref.is_curve:
            await self.db.save_pool_ref(ref.to_row())
        return ref

    async def sync_once(self) -> None:
        # Drop stale cache first. A graduated bonding curve would otherwise be
        # re-watched from its cached ref on this very pass, and never move to
        # the pool it now trades on.
        while self.feed.stale:
            mint = self.feed.stale.pop()
            await self.db.delete_pool_ref(mint)
            self._resolved_at.pop(mint, None)
            self._unsupported.pop(mint, None)
            self._awaiting_bond.pop(mint, None)
            log.info("%s price source changed, cache cleared for re-resolve", mint[:8])

        tokens = await self.db.list_tokens(enabled_only=True)
        wanted: set[str] = set()

        for token in tokens:
            # Don't pay to stream a token we would discard anyway.
            if token.is_muted:
                continue
            wanted.add(token.mint)

            if token.mint in self.feed.watched_mints:
                continue

            # Back off on tokens we already know we can't price.
            last_try = self._unsupported.get(token.mint, 0)
            if last_try and time.time() - last_try < RERESOLVE_AFTER:
                continue

            # Same, but sooner, for coins waiting to bond.
            waiting = self._awaiting_bond.get(token.mint, 0)
            if waiting and time.time() - waiting < BOND_RECHECK:
                continue

            try:
                ref = await self._load_ref(token.mint, token.pair_address)
            except Exception as e:
                log.debug("resolve failed for %s: %s", token.mint[:8], e)
                continue

            if not ref:
                self._unsupported[token.mint] = time.time()
                if token.tier is not Tier.POLLED:
                    await self._set_tier(token.mint, Tier.POLLED)
                continue

            # Not bonded yet: don't subscribe and don't alert. Its launch-curve
            # volatility would be indistinguishable from a real dip.
            if ref.is_curve:
                await self.db.delete_pool_ref(token.mint)
                if token.mint not in self._awaiting_bond:
                    log.info(
                        "%s is still on its bonding curve - not tracking until it bonds",
                        token.symbol or token.mint[:8],
                    )
                self._awaiting_bond[token.mint] = time.time()
                if token.tier is not Tier.POLLED:
                    await self._set_tier(token.mint, Tier.POLLED)
                continue

            if self._awaiting_bond.pop(token.mint, None):
                log.info("%s has bonded - tracking now", token.symbol or token.mint[:8])

            self._unsupported.pop(token.mint, None)
            self.feed.watch(ref)
            self._resolved_at[token.mint] = time.time()
            if token.tier is not Tier.REALTIME:
                await self._set_tier(token.mint, Tier.REALTIME)
            log.info(
                "watching %s (%s, %d accounts)",
                token.symbol or token.mint[:8], ref.kind, len(ref.accounts),
            )

        for mint in set(self._awaiting_bond) - wanted:
            self._awaiting_bond.pop(mint, None)

        for mint in self.feed.watched_mints - wanted:
            self.feed.unwatch(mint)
            log.info("stopped watching %s", mint[:8])

        self.sync_count += 1

    async def _set_tier(self, mint: str, tier: Tier) -> None:
        await self.db.conn.execute("UPDATE watchlist SET tier = ? WHERE mint = ?", (tier.value, mint))
        await self.db.conn.commit()

    async def run_forever(self) -> None:
        log.info("feed supervisor started")
        while True:
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("feed sync failed")
            await asyncio.sleep(SYNC_INTERVAL)
