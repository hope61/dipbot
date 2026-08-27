"""Persistence: settings ownership, watchlist, metadata, alert history."""
from __future__ import annotations

import time

import pytest

from dipbot.models import Tier
from tests.conftest import make_meta

pytestmark = pytest.mark.asyncio


async def test_seed_writes_defaults(db):
    assert await db.get("quote_mode") == "SOL"
    assert await db.get("cooldown_per_token_seconds") == 600


async def test_seed_does_not_overwrite_existing(db):
    """Once the bot has run, settings belong to /settings, not .env."""
    await db.set("quote_mode", "USD")
    written = await db.seed_settings({"quote_mode": "SOL"})
    assert written == 0
    assert await db.get("quote_mode") == "USD"


async def test_seed_adds_only_new_keys(db):
    written = await db.seed_settings({"quote_mode": "SOL", "brand_new_key": 42})
    assert written == 1
    assert await db.get("brand_new_key") == 42


async def test_get_returns_default_for_missing(db):
    assert await db.get("nope") is None
    assert await db.get("nope", "fallback") == "fallback"


async def test_settings_roundtrip_types(db):
    await db.set("a_float", 1.5)
    await db.set("a_list", "10s,1m")
    await db.set("a_bool", True)
    assert await db.get("a_float") == 1.5
    assert await db.get("a_list") == "10s,1m"
    assert await db.get("a_bool") is True


# --- watchlist --------------------------------------------------------------


async def test_add_and_list_token(db):
    added = await db.add_token("mint1", "pair1", "CATE", Tier.REALTIME)
    assert added is True
    tokens = await db.list_tokens()
    assert len(tokens) == 1
    assert tokens[0].symbol == "CATE"
    assert tokens[0].tier is Tier.REALTIME


async def test_add_duplicate_returns_false(db):
    await db.add_token("mint1", "pair1", "CATE", Tier.REALTIME)
    assert await db.add_token("mint1", "pair1", "CATE", Tier.REALTIME) is False
    assert len(await db.list_tokens()) == 1


async def test_remove_token(db):
    await db.add_token("mint1", "pair1", "CATE", Tier.REALTIME)
    assert await db.remove_token("mint1") is True
    assert await db.remove_token("mint1") is False
    assert await db.list_tokens() == []


async def test_find_token_by_mint_and_symbol(db):
    await db.add_token("mint1", "pair1", "CATE", Tier.REALTIME)
    assert (await db.find_token("mint1")).symbol == "CATE"
    assert (await db.find_token("CATE")).mint == "mint1"
    assert (await db.find_token("cate")).mint == "mint1"
    assert (await db.find_token("$cate")).mint == "mint1"
    assert await db.find_token("unknown") is None


async def test_mute_token(db):
    await db.add_token("mint1", "pair1", "CATE", Tier.REALTIME)
    token = (await db.list_tokens())[0]
    assert token.is_muted is False

    await db.mute_token("mint1", time.time() + 3600)
    assert (await db.list_tokens())[0].is_muted is True

    await db.mute_token("mint1", time.time() - 1)
    assert (await db.list_tokens())[0].is_muted is False


async def test_enabled_only_filter(db):
    await db.add_token("mint1", "pair1", "A", Tier.REALTIME)
    await db.add_token("mint2", "pair2", "B", Tier.POLLED)
    await db.conn.execute("UPDATE watchlist SET enabled = 0 WHERE mint = 'mint2'")
    await db.conn.commit()
    assert len(await db.list_tokens()) == 2
    assert len(await db.list_tokens(enabled_only=True)) == 1


# --- metadata ---------------------------------------------------------------


async def test_meta_roundtrip(db):
    meta = make_meta()
    await db.upsert_meta(meta)
    loaded = await db.get_meta(meta.mint)
    assert loaded.symbol == "CATE"
    assert loaded.liquidity_usd == pytest.approx(2_300_000.0)
    assert loaded.dex_id == "pumpswap"


async def test_meta_upsert_overwrites(db):
    await db.upsert_meta(make_meta(price_usd=1.0))
    await db.upsert_meta(make_meta(price_usd=2.0))
    loaded = await db.get_meta(make_meta().mint)
    assert loaded.price_usd == pytest.approx(2.0)


async def test_meta_preserves_null_liquidity(db, memedex):
    """None must survive the round trip; coercing to 0 would mute the token."""
    await db.upsert_meta(memedex)
    loaded = await db.get_meta(memedex.mint)
    assert loaded.liquidity_usd is None
    assert loaded.volume_h24 == pytest.approx(141_491.87)


async def test_get_meta_missing_returns_none(db):
    assert await db.get_meta("nope") is None


# --- alerts -----------------------------------------------------------------


async def test_record_and_count_alerts(db):
    now = time.time()
    await db.record_alert("mint1", "1m", 14.2, 1.0, 1.2, now)
    await db.record_alert("mint1", "3m", 20.0, 1.0, 1.3, now)
    assert await db.alert_count_since(now - 60) == 2
    assert await db.alert_count_since(now + 60) == 0


async def test_last_alert_ts(db):
    assert await db.last_alert_ts("mint1") == 0.0
    now = time.time()
    await db.record_alert("mint1", "1m", 10.0, 1.0, 1.1, now - 100)
    await db.record_alert("mint1", "1m", 10.0, 1.0, 1.1, now)
    assert await db.last_alert_ts("mint1") == pytest.approx(now)


# --- market cap high-water mark ---------------------------------------------
# The peak shown in alerts. It must only ever ratchet upward, and must survive
# the token falling, going quiet, or the bot restarting.


async def test_ath_recorded_on_first_write(db):
    await db.upsert_meta(make_meta(market_cap=100_000.0))
    loaded = await db.get_meta(make_meta().mint)
    assert loaded.ath_market_cap == pytest.approx(100_000.0)


async def test_ath_rises_with_a_new_high(db):
    await db.upsert_meta(make_meta(market_cap=100_000.0))
    await db.upsert_meta(make_meta(market_cap=250_000.0))
    loaded = await db.get_meta(make_meta().mint)
    assert loaded.ath_market_cap == pytest.approx(250_000.0)
    assert loaded.market_cap == pytest.approx(250_000.0)


async def test_ath_survives_the_token_falling(db):
    """The whole point: a coin down 80% must still report where it peaked."""
    await db.upsert_meta(make_meta(market_cap=500_000.0))
    await db.upsert_meta(make_meta(market_cap=100_000.0))
    loaded = await db.get_meta(make_meta().mint)
    assert loaded.ath_market_cap == pytest.approx(500_000.0)
    assert loaded.market_cap == pytest.approx(100_000.0)


async def test_ath_never_ratchets_down_over_many_updates(db):
    for cap in (50_000, 120_000, 90_000, 300_000, 10_000, 240_000):
        await db.upsert_meta(make_meta(market_cap=float(cap)))
    loaded = await db.get_meta(make_meta().mint)
    assert loaded.ath_market_cap == pytest.approx(300_000.0)


async def test_ath_ignores_a_missing_market_cap(db):
    """DexScreener sometimes omits the cap; that must not wipe the peak."""
    await db.upsert_meta(make_meta(market_cap=400_000.0))
    await db.upsert_meta(make_meta(market_cap=None))
    loaded = await db.get_meta(make_meta().mint)
    assert loaded.ath_market_cap == pytest.approx(400_000.0)


async def test_ath_is_per_token(db):
    await db.upsert_meta(make_meta(mint="mint-a", market_cap=100_000.0))
    await db.upsert_meta(make_meta(mint="mint-b", market_cap=900_000.0))
    assert (await db.get_meta("mint-a")).ath_market_cap == pytest.approx(100_000.0)
    assert (await db.get_meta("mint-b")).ath_market_cap == pytest.approx(900_000.0)


async def test_ath_persists_across_reconnect(db, tmp_path):
    from dipbot.db import Database

    await db.upsert_meta(make_meta(market_cap=750_000.0))
    await db.close()

    reopened = Database(db.path)
    await reopened.connect()
    loaded = await reopened.get_meta(make_meta().mint)
    assert loaded.ath_market_cap == pytest.approx(750_000.0)
    await reopened.close()


# --- schema migration -------------------------------------------------------


async def test_migration_adds_the_column_to_an_older_database(tmp_path):
    """CREATE TABLE IF NOT EXISTS skips existing tables, so installs made
    before ath_market_cap existed need an explicit ALTER."""
    import sqlite3

    from dipbot.db import Database

    path = tmp_path / "old.sqlite"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE token_meta (mint TEXT PRIMARY KEY, pair_address TEXT, symbol TEXT, "
        "name TEXT, dex_id TEXT, price_usd REAL, price_native REAL, liquidity_usd REAL, "
        "volume_h24 REAL, volume_m5 REAL, market_cap REAL, pair_created_at INTEGER, "
        "txns_m5_buys INTEGER, txns_m5_sells INTEGER, updated_at REAL)"
    )
    legacy.execute(
        "INSERT INTO token_meta (mint, symbol, market_cap) VALUES ('old-mint', 'OLD', 12345)"
    )
    legacy.commit()
    legacy.close()

    db = Database(path)
    await db.connect()

    columns = [r[1] for r in await (await db.conn.execute("PRAGMA table_info(token_meta)")).fetchall()]
    assert "ath_market_cap" in columns

    existing = await db.get_meta("old-mint")
    assert existing.symbol == "OLD"
    assert existing.ath_market_cap is None  # unknown for rows written before tracking
    await db.close()


async def test_migration_is_idempotent(tmp_path):
    from dipbot.db import Database

    path = tmp_path / "twice.sqlite"
    for _ in range(3):
        db = Database(path)
        await db.connect()
        await db.close()

    db = Database(path)
    await db.connect()
    columns = [r[1] for r in await (await db.conn.execute("PRAGMA table_info(token_meta)")).fetchall()]
    assert columns.count("ath_market_cap") == 1
    await db.close()


async def test_migrated_database_starts_tracking_on_next_poll(tmp_path):
    """An old row with no peak should pick one up from the next update."""
    from dipbot.db import Database

    db = Database(tmp_path / "m.sqlite")
    await db.connect()
    await db.conn.execute(
        "INSERT INTO token_meta (mint, symbol, market_cap) VALUES ('m1', 'X', 500)"
    )
    await db.conn.commit()
    assert (await db.get_meta("m1")).ath_market_cap is None

    await db.upsert_meta(make_meta(mint="m1", symbol="X", market_cap=800.0))
    assert (await db.get_meta("m1")).ath_market_cap == pytest.approx(800.0)
    await db.close()


async def test_ath_is_available_immediately_after_first_write(db):
    """The added-coin message reads it straight back, so it must exist at once."""
    await db.upsert_meta(make_meta(market_cap=171_394.0, ath_market_cap=None))
    loaded = await db.get_meta(make_meta().mint)
    assert loaded.ath_market_cap == pytest.approx(171_394.0)


async def test_ath_survives_a_coin_being_removed_and_re_added(db):
    """Peaks belong to the coin, not to the current watch session."""
    from dipbot.models import Tier

    meta = make_meta(market_cap=500_000.0)
    await db.add_token(meta.mint, meta.pair_address, meta.symbol, Tier.REALTIME)
    await db.upsert_meta(meta)

    await db.remove_token(meta.mint)                      # metadata is kept
    await db.add_token(meta.mint, meta.pair_address, meta.symbol, Tier.REALTIME)
    await db.upsert_meta(make_meta(market_cap=100_000.0))  # re-added much lower

    loaded = await db.get_meta(meta.mint)
    assert loaded.ath_market_cap == pytest.approx(500_000.0)
    assert loaded.market_cap == pytest.approx(100_000.0)


async def test_set_ath_raises_the_peak(db):
    await db.upsert_meta(make_meta(market_cap=50_000.0))
    await db.set_ath(make_meta().mint, 176_373.0)
    assert (await db.get_meta(make_meta().mint)).ath_market_cap == pytest.approx(176_373.0)


async def test_set_ath_never_lowers_it(db):
    """A real peak from history must not be undone by a later quiet poll."""
    await db.upsert_meta(make_meta(market_cap=50_000.0))
    await db.set_ath(make_meta().mint, 176_373.0)
    await db.set_ath(make_meta().mint, 1_000.0)
    await db.upsert_meta(make_meta(market_cap=60_000.0))
    assert (await db.get_meta(make_meta().mint)).ath_market_cap == pytest.approx(176_373.0)
