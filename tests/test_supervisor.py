"""Feed supervisor: subscription lifecycle, tier flips, resolution caching.

These cover claims made elsewhere in the design - that muted tokens are not
streamed (a cost-control promise), that cached refs avoid re-resolving on every
restart, and that a token graduating mid-life is re-resolved rather than stuck.
"""
from __future__ import annotations

import time

import pytest

from dipbot.feeds.pool_resolver import CURVE, POOL, PoolRef
from dipbot.feeds.rpc_feed import RpcFeed
from dipbot.feeds.supervisor import FeedSupervisor
from dipbot.models import Tier

MINT_A = "FmsQNEebvJLMcfiKuVuvGRgAQJzpJfyjJ8PHUGnzpump"
MINT_B = "DvFzcExFCWj9GnVS1ByXrsPezniP7Be6QienbXJ8pump"
PAIR = "HMzvsEEmtzHhvZNw9uwbaG85HCTmFnkbhzUx16cy7ca3"

pytestmark = pytest.mark.asyncio


def make_ref(mint: str, kind: str = POOL) -> PoolRef:
    accounts = ("acct1",) if kind == CURVE else ("acct1", "acct2")
    return PoolRef(mint=mint, kind=kind, program="prog", accounts=accounts, decimals=6)


class FakeResolver:
    def __init__(self, refs: dict[str, PoolRef | None]):
        self.refs = refs
        self.calls: list[str] = []

    async def resolve(self, mint, pair_address=None):
        self.calls.append(mint)
        return self.refs.get(mint)


async def build(db, refs):
    async def on_tick(_):
        pass

    feed = RpcFeed("wss://example", on_tick)
    resolver = FakeResolver(refs)
    return FeedSupervisor(db, resolver, feed), feed, resolver


async def test_resolvable_token_is_watched_and_marked_realtime(db):
    await db.add_token(MINT_A, PAIR, "CATE", Tier.POLLED)
    sup, feed, _ = await build(db, {MINT_A: make_ref(MINT_A)})

    await sup.sync_once()

    assert feed.watched_mints == {MINT_A}
    assert feed.account_count == 2
    assert (await db.list_tokens())[0].tier is Tier.REALTIME


async def test_unresolvable_token_stays_polled(db):
    await db.add_token(MINT_A, PAIR, "MEMEDEX", Tier.REALTIME)
    sup, feed, _ = await build(db, {MINT_A: None})

    await sup.sync_once()

    assert feed.watched_mints == set()
    assert (await db.list_tokens())[0].tier is Tier.POLLED


async def test_muted_token_is_not_streamed(db):
    """Cost control: we don't pay for bytes we would discard."""
    await db.add_token(MINT_A, PAIR, "CATE", Tier.REALTIME)
    await db.mute_token(MINT_A, time.time() + 3600)
    sup, feed, resolver = await build(db, {MINT_A: make_ref(MINT_A)})

    await sup.sync_once()

    assert feed.watched_mints == set()
    assert resolver.calls == []


async def test_unmuting_resumes_streaming(db):
    await db.add_token(MINT_A, PAIR, "CATE", Tier.REALTIME)
    await db.mute_token(MINT_A, time.time() + 3600)
    sup, feed, _ = await build(db, {MINT_A: make_ref(MINT_A)})
    await sup.sync_once()
    assert feed.watched_mints == set()

    await db.mute_token(MINT_A, 0)
    await sup.sync_once()
    assert feed.watched_mints == {MINT_A}


async def test_removed_token_is_unwatched(db):
    await db.add_token(MINT_A, PAIR, "CATE", Tier.REALTIME)
    sup, feed, _ = await build(db, {MINT_A: make_ref(MINT_A)})
    await sup.sync_once()
    assert feed.watched_mints == {MINT_A}

    await db.remove_token(MINT_A)
    await sup.sync_once()
    assert feed.watched_mints == set()
    assert feed.account_count == 0


async def test_resolution_is_cached_in_db(db):
    """Without the cache, every restart re-resolves every token over HTTP."""
    await db.add_token(MINT_A, PAIR, "CATE", Tier.POLLED)
    sup, _, resolver = await build(db, {MINT_A: make_ref(MINT_A)})

    await sup.sync_once()
    assert resolver.calls == [MINT_A]

    cached = await db.get_pool_ref(MINT_A)
    assert cached is not None
    assert cached["accounts"] == ["acct1", "acct2"]


async def test_cached_ref_avoids_second_resolve(db):
    await db.add_token(MINT_A, PAIR, "CATE", Tier.POLLED)
    sup, feed, resolver = await build(db, {MINT_A: make_ref(MINT_A)})
    await sup.sync_once()

    # simulate a restart: fresh feed and supervisor, same database
    sup2, feed2, resolver2 = await build(db, {MINT_A: make_ref(MINT_A)})
    await sup2.sync_once()

    assert feed2.watched_mints == {MINT_A}
    assert resolver2.calls == []  # served entirely from cache


# --- pre-bond coins are not tracked ----------------------------------------


async def test_coin_on_its_bonding_curve_is_not_watched(db):
    """Launch-curve volatility is indistinguishable from a real dip."""
    await db.add_token(MINT_A, PAIR, "FRESH", Tier.REALTIME)
    sup, feed, _ = await build(db, {MINT_A: make_ref(MINT_A, kind=CURVE)})

    await sup.sync_once()

    assert feed.watched_mints == set()
    assert feed.account_count == 0
    assert sup.awaiting_bond == {MINT_A}
    assert (await db.list_tokens())[0].tier is Tier.POLLED


async def test_coin_starts_being_tracked_once_it_bonds(db):
    await db.add_token(MINT_A, PAIR, "FRESH", Tier.REALTIME)
    sup, feed, resolver = await build(db, {MINT_A: make_ref(MINT_A, kind=CURVE)})
    await sup.sync_once()
    assert feed.watched_mints == set()

    # it bonds: resolution now yields a pool
    resolver.refs[MINT_A] = make_ref(MINT_A, kind=POOL)
    sup._awaiting_bond[MINT_A] = 0  # skip the re-check backoff
    await sup.sync_once()

    assert feed.watched_mints == {MINT_A}
    assert sup.awaiting_bond == set()
    assert (await db.list_tokens())[0].tier is Tier.REALTIME


async def test_pre_bond_ref_is_not_cached(db):
    """A cached curve ref would be served forever and the coin never tracked."""
    await db.add_token(MINT_A, PAIR, "FRESH", Tier.REALTIME)
    sup, _, _ = await build(db, {MINT_A: make_ref(MINT_A, kind=CURVE)})

    await sup.sync_once()

    assert await db.get_pool_ref(MINT_A) is None


async def test_pre_bond_coin_is_not_rechecked_every_sync(db):
    """Re-resolving costs RPC calls; back off between checks."""
    await db.add_token(MINT_A, PAIR, "FRESH", Tier.REALTIME)
    sup, _, resolver = await build(db, {MINT_A: make_ref(MINT_A, kind=CURVE)})

    await sup.sync_once()
    await sup.sync_once()
    await sup.sync_once()

    assert resolver.calls == [MINT_A]


async def test_removing_a_pre_bond_coin_forgets_it(db):
    await db.add_token(MINT_A, PAIR, "FRESH", Tier.REALTIME)
    sup, _, _ = await build(db, {MINT_A: make_ref(MINT_A, kind=CURVE)})
    await sup.sync_once()
    assert sup.awaiting_bond == {MINT_A}

    await db.remove_token(MINT_A)
    await sup.sync_once()
    assert sup.awaiting_bond == set()


async def test_stale_mint_is_drained(db):
    await db.add_token(MINT_A, PAIR, "FRESH", Tier.REALTIME)
    sup, feed, _ = await build(db, {MINT_A: make_ref(MINT_A, kind=POOL)})
    await sup.sync_once()

    feed.stale.add(MINT_A)
    feed.unwatch(MINT_A)
    await sup.sync_once()

    assert feed.stale == set()


async def test_unsupported_token_is_not_retried_every_sync(db):
    await db.add_token(MINT_A, PAIR, "MEMEDEX", Tier.POLLED)
    sup, _, resolver = await build(db, {MINT_A: None})

    await sup.sync_once()
    await sup.sync_once()
    await sup.sync_once()

    assert resolver.calls == [MINT_A]  # backed off, not hammered


async def test_multiple_tokens_tracked_independently(db):
    await db.add_token(MINT_A, PAIR, "A", Tier.POLLED)
    await db.add_token(MINT_B, PAIR, "B", Tier.POLLED)
    sup, feed, _ = await build(db, {MINT_A: make_ref(MINT_A), MINT_B: None})

    await sup.sync_once()

    assert feed.watched_mints == {MINT_A}
    tiers = {t.symbol: t.tier for t in await db.list_tokens()}
    assert tiers == {"A": Tier.REALTIME, "B": Tier.POLLED}


async def test_disabled_token_is_not_streamed(db):
    await db.add_token(MINT_A, PAIR, "CATE", Tier.REALTIME)
    await db.conn.execute("UPDATE watchlist SET enabled = 0 WHERE mint = ?", (MINT_A,))
    await db.conn.commit()
    sup, feed, _ = await build(db, {MINT_A: make_ref(MINT_A)})

    await sup.sync_once()

    assert feed.watched_mints == set()


# --- pool ref persistence ---------------------------------------------------


async def test_pool_ref_save_get_delete(db):
    ref = make_ref(MINT_A)
    await db.save_pool_ref(ref.to_row())
    loaded = await db.get_pool_ref(MINT_A)
    assert PoolRef.from_row(loaded) == ref

    await db.delete_pool_ref(MINT_A)
    assert await db.get_pool_ref(MINT_A) is None


async def test_pool_ref_upsert_overwrites(db):
    await db.save_pool_ref(make_ref(MINT_A, kind=CURVE).to_row())
    await db.save_pool_ref(make_ref(MINT_A, kind=POOL).to_row())
    loaded = await db.get_pool_ref(MINT_A)
    assert loaded["kind"] == POOL
    assert len(loaded["accounts"]) == 2


async def test_get_missing_pool_ref_returns_none(db):
    assert await db.get_pool_ref("nope") is None
