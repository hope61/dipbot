"""Poller: which pool gets polled, and following a coin when it migrates.

The regression these guard against is CRYPTO (7KkXNCoJ...): the watchlist held
its pre-migration `pumpfun` pair, DexScreener's by-mint endpoint kept handing
that same dead pair back, and the coin sat frozen at its migration-moment cap
for its whole life - no price movement, so no dip could ever be detected.
"""
from __future__ import annotations

import pytest

from dipbot.models import PriceTick, Tier
from dipbot.poller import Poller
from tests.conftest import make_meta

MINT = "7KkXNCoJA1uNVahThhuFsBqVqtwm7BsCvWRyrtvUpump"
CURVE = "4TgwdisarGqxoyQ6sNrzWLFrLoj96jmnRpDi5Dd8rdPA"
POOL = "4snXWos2LSE8tkn2fx65hXq8BMmQAPizWdFSXJVUJG67"


def curve_meta(**kw):
    return make_meta(
        mint=MINT, pair_address=CURVE, symbol="CRYPTO", dex_id="pumpfun",
        liquidity_usd=None, volume_h24=40_469.14, volume_m5=0.0,
        market_cap=39_329.45, price_usd=0.00003932, price_native=3.2e-7, **kw
    )


def pool_meta(**kw):
    fields = dict(
        mint=MINT, pair_address=POOL, symbol="CRYPTO", dex_id="pumpswap",
        liquidity_usd=42_743.89, volume_h24=1_000_000.0, volume_m5=8_781.0,
        market_cap=183_627.0, price_usd=0.0001854, price_native=8.9e-7,
    )
    fields.update(kw)
    return make_meta(**fields)


class FakeDex:
    """Records which addresses were asked for, and by which route."""

    def __init__(self, by_pair=None, by_mint=None, resolved=None):
        self.by_pair = by_pair or {}
        self.by_mint = by_mint or {}
        self.resolved = resolved
        self.pair_calls: list[list[str]] = []
        self.mint_calls: list[list[str]] = []
        self.resolve_calls: list[str] = []

    async def fetch_pairs(self, addresses, bucket=20.0):
        self.pair_calls.append(list(addresses))
        return {m.mint: m for a, m in self.by_pair.items() if a in addresses}

    async def fetch_many(self, mints, bucket=20.0):
        self.mint_calls.append(list(mints))
        return {m: self.by_mint[m] for m in mints if m in self.by_mint}

    async def resolve(self, address):
        self.resolve_calls.append(address)
        return self.resolved

    async def sol_usd(self):
        return 200.0


@pytest.mark.asyncio
async def test_polls_the_recorded_pair_not_the_mint(db):
    await db.add_token(MINT, POOL, "CRYPTO", Tier.REALTIME)
    dex = FakeDex(by_pair={POOL: pool_meta()})
    poller = Poller(db, dex, interval=20.0)

    await poller.run_once()

    assert dex.pair_calls == [[POOL]]
    assert dex.mint_calls == []  # nothing was missing, so no by-mint fallback
    assert (await db.get_meta(MINT)).dex_id == "pumpswap"


@pytest.mark.asyncio
async def test_falls_back_to_the_mint_when_the_pair_returns_nothing(db):
    await db.add_token(MINT, POOL, "CRYPTO", Tier.REALTIME)
    dex = FakeDex(by_mint={MINT: pool_meta()})
    poller = Poller(db, dex, interval=20.0)

    await poller.run_once()

    assert dex.mint_calls == [[MINT]]
    assert (await db.get_meta(MINT)).market_cap == pytest.approx(183_627.0)


@pytest.mark.asyncio
async def test_migration_moves_the_watchlist_onto_the_real_pool(db):
    """pump -> pumpswap keeps the same tier, so the pair must move on its own."""
    await db.add_token(MINT, CURVE, "CRYPTO", Tier.REALTIME)
    dex = FakeDex(by_pair={CURVE: curve_meta()}, resolved=pool_meta())
    poller = Poller(db, dex, interval=20.0)

    await poller.run_once()

    assert dex.resolve_calls == [MINT]
    token = (await db.list_tokens())[0]
    assert token.pair_address == POOL
    stored = await db.get_meta(MINT)
    assert stored.dex_id == "pumpswap"
    assert stored.market_cap == pytest.approx(183_627.0)


@pytest.mark.asyncio
async def test_unmigrated_coin_keeps_its_curve(db):
    """Re-resolving must not invent a migration that has not happened."""
    await db.add_token(MINT, CURVE, "CRYPTO", Tier.REALTIME)
    dex = FakeDex(by_pair={CURVE: curve_meta()}, resolved=curve_meta())
    poller = Poller(db, dex, interval=20.0)

    await poller.run_once()

    assert (await db.list_tokens())[0].pair_address == CURVE


@pytest.mark.asyncio
async def test_migration_ticks_the_new_pools_price(db):
    """The detector must receive the live pool price, not the frozen one."""
    ticks: list[PriceTick] = []

    async def on_tick(tick):
        ticks.append(tick)

    await db.add_token(MINT, CURVE, "CRYPTO", Tier.REALTIME)
    dex = FakeDex(by_pair={CURVE: curve_meta()}, resolved=pool_meta())
    poller = Poller(db, dex, interval=20.0, on_tick=on_tick)

    await poller.run_once()

    assert [t.price_sol for t in ticks] == [pytest.approx(8.9e-7)]


@pytest.mark.asyncio
async def test_tier_follows_the_pool_it_migrated_to(db):
    await db.add_token(MINT, CURVE, "CRYPTO", Tier.REALTIME)
    dex = FakeDex(
        by_pair={CURVE: curve_meta()}, resolved=pool_meta(dex_id="meteora")
    )
    poller = Poller(db, dex, interval=20.0)

    await poller.run_once()

    assert (await db.list_tokens())[0].tier is Tier.POLLED
