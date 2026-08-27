"""Clearing the whole watchlist.

Destructive and irreversible, so the interesting cases are what it does *not*
touch and that it asks before acting.
"""
from __future__ import annotations

import pytest

from dipbot.models import Tier

pytestmark = pytest.mark.asyncio


async def add_some(db, n=3):
    for i in range(n):
        await db.add_token(f"mint{i}", f"pair{i}", f"SYM{i}", Tier.REALTIME)


async def test_clear_removes_every_coin(db):
    await add_some(db)
    assert await db.clear_watchlist() == 3
    assert await db.list_tokens() == []


async def test_clear_reports_zero_on_an_empty_watchlist(db):
    assert await db.clear_watchlist() == 0


async def test_clear_leaves_settings_alone(db):
    await add_some(db)
    await db.set("threshold_migrated_1m", 33)

    await db.clear_watchlist()

    assert await db.get("threshold_migrated_1m") == 33
    assert len(await db.all_settings()) >= 19


async def test_clear_leaves_admins_alone(db):
    await add_some(db)
    await db.add_admin(555, "Bob", 111)

    await db.clear_watchlist()

    assert await db.admin_ids() == {555}


async def test_clear_keeps_recorded_peaks(db):
    """So re-adding a coin restores its all-time high instead of starting over."""
    from tests.conftest import make_meta

    meta = make_meta(market_cap=50_000.0)
    await db.add_token(meta.mint, meta.pair_address, meta.symbol, Tier.REALTIME)
    await db.upsert_meta(meta)
    await db.set_ath(meta.mint, 176_373.0)

    await db.clear_watchlist()

    kept = await db.get_meta(meta.mint)
    assert kept is not None
    assert kept.ath_market_cap == pytest.approx(176_373.0)


async def test_re_adding_after_clear_restores_the_peak(db):
    from tests.conftest import make_meta

    meta = make_meta(market_cap=50_000.0)
    await db.add_token(meta.mint, meta.pair_address, meta.symbol, Tier.REALTIME)
    await db.upsert_meta(meta)
    await db.set_ath(meta.mint, 176_373.0)
    await db.clear_watchlist()

    await db.add_token(meta.mint, meta.pair_address, meta.symbol, Tier.REALTIME)
    assert (await db.get_meta(meta.mint)).ath_market_cap == pytest.approx(176_373.0)


async def test_clear_leaves_alert_history(db):
    """The record of what fired is worth keeping for the daily summary."""
    import time

    await add_some(db)
    await db.record_alert("mint0", "1m", 30.0, 1.0, 1.5, time.time())

    await db.clear_watchlist()

    assert await db.alert_count_since(0) == 1


async def test_clearing_twice_is_harmless(db):
    await add_some(db)
    assert await db.clear_watchlist() == 3
    assert await db.clear_watchlist() == 0


async def test_clear_is_offered_only_when_something_is_watched(db):
    """The command short-circuits on an empty list rather than asking."""
    from dipbot.bot import handlers
    from tests.test_access import Recorder

    handlers.deps = handlers.Deps(db=db, dex=None, sender=None, owner_id=1)
    message = Recorder(1, "/clear")
    await handlers.cmd_clear(message)
    assert "already empty" in message.replies[0]


async def test_clear_asks_before_acting(db):
    from dipbot.bot import handlers
    from tests.test_access import Recorder

    await add_some(db)
    handlers.deps = handlers.Deps(db=db, dex=None, sender=None, owner_id=1)
    message = Recorder(1, "/clear")
    await handlers.cmd_clear(message)

    assert "Remove all" in message.replies[0]
    assert len(await db.list_tokens()) == 3      # nothing removed yet
