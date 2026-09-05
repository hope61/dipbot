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



# --- schema migration -------------------------------------------------------
# ath_market_cap was dropped when the ATH display was removed. get_meta splats
# SELECT * into TokenMeta, so a leftover column is not harmless - it raises
# TypeError on the first read. Deployed databases must actually lose it.


async def test_migration_drops_the_ath_column_from_an_older_database(tmp_path):
    import sqlite3

    from dipbot.db import Database

    path = tmp_path / "old.sqlite"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE token_meta (mint TEXT PRIMARY KEY, pair_address TEXT, symbol TEXT, "
        "name TEXT, dex_id TEXT, price_usd REAL, price_native REAL, liquidity_usd REAL, "
        "volume_h24 REAL, volume_m5 REAL, market_cap REAL, pair_created_at INTEGER, "
        "txns_m5_buys INTEGER, txns_m5_sells INTEGER, updated_at REAL, "
        "ath_market_cap REAL)"
    )
    legacy.execute(
        "INSERT INTO token_meta (mint, symbol, market_cap, ath_market_cap) "
        "VALUES ('old-mint', 'OLD', 12345, 99999)"
    )
    legacy.commit()
    legacy.close()

    db = Database(path)
    await db.connect()

    columns = [r[1] for r in await (await db.conn.execute("PRAGMA table_info(token_meta)")).fetchall()]
    assert "ath_market_cap" not in columns
    await db.close()


async def test_an_older_database_is_still_readable_after_migrating(tmp_path):
    """The failure this guards: TokenMeta(**row) choking on a stale column."""
    import sqlite3

    from dipbot.db import Database

    path = tmp_path / "readable.sqlite"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE token_meta (mint TEXT PRIMARY KEY, pair_address TEXT, symbol TEXT, "
        "name TEXT, dex_id TEXT, price_usd REAL, price_native REAL, liquidity_usd REAL, "
        "volume_h24 REAL, volume_m5 REAL, market_cap REAL, pair_created_at INTEGER, "
        "txns_m5_buys INTEGER, txns_m5_sells INTEGER, updated_at REAL, "
        "ath_market_cap REAL)"
    )
    legacy.execute(
        "INSERT INTO token_meta (mint, symbol, market_cap, ath_market_cap) "
        "VALUES ('old-mint', 'OLD', 12345, 99999)"
    )
    legacy.commit()
    legacy.close()

    db = Database(path)
    await db.connect()
    existing = await db.get_meta("old-mint")
    assert existing.symbol == "OLD"
    assert existing.market_cap == pytest.approx(12345.0)
    assert not hasattr(existing, "ath_market_cap")
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
    assert "ath_market_cap" not in columns
    assert columns.count("market_cap") == 1
    await db.close()


async def test_metadata_survives_a_coin_being_removed_and_re_added(db):
    """Clearing the watchlist leaves token_meta alone, so a re-add starts from
    what was already known rather than an empty row."""
    from dipbot.models import Tier

    meta = make_meta(market_cap=500_000.0)
    await db.add_token(meta.mint, meta.pair_address, meta.symbol, Tier.REALTIME)
    await db.upsert_meta(meta)

    await db.remove_token(meta.mint)                      # metadata is kept
    await db.add_token(meta.mint, meta.pair_address, meta.symbol, Tier.REALTIME)

    loaded = await db.get_meta(meta.mint)
    assert loaded is not None
    assert loaded.symbol == meta.symbol
    assert loaded.market_cap == pytest.approx(500_000.0)
