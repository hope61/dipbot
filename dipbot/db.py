"""SQLite persistence: settings, watchlist, token metadata, alert history.

Price ticks are deliberately NOT stored. They live in memory only - a few hundred
tokens times a few hundred ticks is trivial RAM, and stale history after a restart
is worse than none (it would read as a giant fake dip, hence the warmup window).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import aiosqlite

from .models import Tier, TokenMeta, WatchedToken

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    mint         TEXT PRIMARY KEY,
    pair_address TEXT NOT NULL,
    symbol       TEXT,
    tier         TEXT NOT NULL,
    added_at     REAL NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    muted_until  REAL NOT NULL DEFAULT 0,
    overrides    TEXT
);

CREATE TABLE IF NOT EXISTS token_meta (
    mint            TEXT PRIMARY KEY,
    pair_address    TEXT,
    symbol          TEXT,
    name            TEXT,
    dex_id          TEXT,
    price_usd       REAL,
    price_native    REAL,
    liquidity_usd   REAL,
    volume_h24      REAL,
    volume_m5       REAL,
    market_cap      REAL,
    pair_created_at INTEGER,
    txns_m5_buys    INTEGER,
    txns_m5_sells   INTEGER,
    updated_at      REAL,
    ath_market_cap  REAL
);

-- Chain price sources, cached so a restart doesn't re-resolve every token
-- over HTTP. Invalidated when a token migrates (curve -> pool).
CREATE TABLE IF NOT EXISTS pool_refs (
    mint       TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    program    TEXT NOT NULL,
    accounts   TEXT NOT NULL,
    decimals   INTEGER NOT NULL,
    updated_at REAL NOT NULL
);

-- People allowed to control the bot, besides the owner. The owner is never
-- stored here: they come from OWNER_USER_ID and cannot be removed, so an admin
-- can never lock the owner out.
CREATE TABLE IF NOT EXISTS admins (
    user_id  INTEGER PRIMARY KEY,
    label    TEXT,
    added_by INTEGER,
    added_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mint       TEXT NOT NULL,
    window     TEXT NOT NULL,
    drawdown   REAL NOT NULL,
    price      REAL NOT NULL,
    baseline   REAL NOT NULL,
    ts         REAL NOT NULL,
    message_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_alerts_mint_ts ON alerts (mint, ts);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() was not awaited")
        return self._conn

    async def connect(self, seed: dict | None = None) -> int:
        """Open the database, apply schema, and seed defaults.

        Returns the number of settings actually written (0 on later runs, since
        existing keys are never overwritten).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()
        return await self.seed_settings(seed) if seed else 0

    async def _migrate(self) -> None:
        """Add columns to databases created before they existed.

        CREATE TABLE IF NOT EXISTS silently skips existing tables, so new
        columns need an explicit ALTER or older installs break on first read.
        """
        async with self._conn.execute("PRAGMA table_info(token_meta)") as cur:
            columns = {row["name"] async for row in cur}
        if "ath_market_cap" not in columns:
            await self._conn.execute("ALTER TABLE token_meta ADD COLUMN ath_market_cap REAL")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # --- settings ----------------------------------------------------------

    async def seed_settings(self, seed: dict) -> int:
        """Insert seed values for keys that don't exist yet.

        Existing keys are never overwritten: once the bot has run, settings are
        owned by /settings, not by .env.
        """
        written = 0
        for key, value in seed.items():
            cur = await self.conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value))
            )
            written += cur.rowcount or 0
        await self.conn.commit()
        return written

    async def get(self, key: str, default=None):
        async with self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return json.loads(row["value"]) if row else default

    async def set(self, key: str, value) -> None:
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        await self.conn.commit()

    async def all_settings(self) -> dict:
        async with self.conn.execute("SELECT key, value FROM settings") as cur:
            return {r["key"]: json.loads(r["value"]) async for r in cur}

    # --- watchlist ---------------------------------------------------------

    async def add_token(self, mint: str, pair_address: str, symbol: str | None, tier: Tier) -> bool:
        """Returns False if the token was already on the watchlist."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO watchlist (mint, pair_address, symbol, tier, added_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (mint, pair_address, symbol, tier.value, time.time()),
        )
        await self.conn.commit()
        return bool(cur.rowcount)

    async def remove_token(self, mint: str) -> bool:
        cur = await self.conn.execute("DELETE FROM watchlist WHERE mint = ?", (mint,))
        await self.conn.commit()
        return bool(cur.rowcount)

    async def clear_watchlist(self) -> int:
        """Stop watching everything. Returns how many were removed.

        Metadata rows are kept on purpose, so re-adding a coin restores its
        recorded all-time high instead of starting over.
        """
        cur = await self.conn.execute("DELETE FROM watchlist")
        await self.conn.commit()
        return cur.rowcount or 0

    async def list_tokens(self, enabled_only: bool = False) -> list[WatchedToken]:
        sql = "SELECT * FROM watchlist"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY added_at"
        async with self.conn.execute(sql) as cur:
            return [
                WatchedToken(
                    mint=r["mint"],
                    pair_address=r["pair_address"],
                    symbol=r["symbol"],
                    tier=Tier(r["tier"]),
                    added_at=r["added_at"],
                    enabled=bool(r["enabled"]),
                    muted_until=r["muted_until"],
                    overrides=json.loads(r["overrides"]) if r["overrides"] else None,
                )
                async for r in cur
            ]

    async def find_token(self, needle: str) -> WatchedToken | None:
        """Look up by exact mint, else case-insensitive symbol."""
        for token in await self.list_tokens():
            if token.mint == needle:
                return token
        needle_lower = needle.lower().lstrip("$")
        for token in await self.list_tokens():
            if (token.symbol or "").lower() == needle_lower:
                return token
        return None

    async def set_overrides(self, mint: str, overrides: dict | None) -> None:
        """Per-token setting overrides, or None to fall back to the shared ones."""
        await self.conn.execute(
            "UPDATE watchlist SET overrides = ? WHERE mint = ?",
            (json.dumps(overrides) if overrides else None, mint),
        )
        await self.conn.commit()

    async def all_overrides(self) -> dict[str, dict]:
        async with self.conn.execute(
            "SELECT mint, overrides FROM watchlist WHERE overrides IS NOT NULL"
        ) as cur:
            return {r["mint"]: json.loads(r["overrides"]) async for r in cur}

    async def mute_token(self, mint: str, until_ts: float) -> None:
        await self.conn.execute("UPDATE watchlist SET muted_until = ? WHERE mint = ?", (until_ts, mint))
        await self.conn.commit()

    # --- token metadata ----------------------------------------------------

    async def upsert_meta(self, meta: TokenMeta) -> None:
        await self.conn.execute(
            """
            INSERT INTO token_meta (
                mint, pair_address, symbol, name, dex_id, price_usd, price_native,
                liquidity_usd, volume_h24, volume_m5, market_cap, pair_created_at,
                txns_m5_buys, txns_m5_sells, updated_at, ath_market_cap
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(mint) DO UPDATE SET
                pair_address=excluded.pair_address, symbol=excluded.symbol,
                name=excluded.name, dex_id=excluded.dex_id,
                price_usd=excluded.price_usd, price_native=excluded.price_native,
                liquidity_usd=excluded.liquidity_usd, volume_h24=excluded.volume_h24,
                volume_m5=excluded.volume_m5, market_cap=excluded.market_cap,
                pair_created_at=excluded.pair_created_at,
                txns_m5_buys=excluded.txns_m5_buys, txns_m5_sells=excluded.txns_m5_sells,
                updated_at=excluded.updated_at,
                -- keep the highest market cap ever seen, never lower it
                ath_market_cap=MAX(
                    COALESCE(token_meta.ath_market_cap, 0),
                    COALESCE(excluded.market_cap, 0)
                )
            """,
            (
                meta.mint, meta.pair_address, meta.symbol, meta.name, meta.dex_id,
                meta.price_usd, meta.price_native, meta.liquidity_usd, meta.volume_h24,
                meta.volume_m5, meta.market_cap, meta.pair_created_at,
                meta.txns_m5_buys, meta.txns_m5_sells, meta.updated_at or time.time(),
                meta.market_cap,
            ),
        )
        await self.conn.commit()

    async def set_ath(self, mint: str, ath: float) -> None:
        """Raise the recorded peak. Never lowers it."""
        await self.conn.execute(
            "UPDATE token_meta SET ath_market_cap = MAX(COALESCE(ath_market_cap, 0), ?) "
            "WHERE mint = ?",
            (ath, mint),
        )
        await self.conn.commit()

    async def get_meta(self, mint: str) -> TokenMeta | None:
        async with self.conn.execute("SELECT * FROM token_meta WHERE mint = ?", (mint,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return TokenMeta(**{k: row[k] for k in row.keys()})

    # --- admins ------------------------------------------------------------

    async def add_admin(self, user_id: int, label: str | None, added_by: int) -> bool:
        """Returns False if they were already an admin."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO admins (user_id, label, added_by, added_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, label, added_by, time.time()),
        )
        await self.conn.commit()
        return bool(cur.rowcount)

    async def remove_admin(self, user_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        await self.conn.commit()
        return bool(cur.rowcount)

    async def list_admins(self) -> list[dict]:
        async with self.conn.execute(
            "SELECT user_id, label, added_by, added_at FROM admins ORDER BY added_at"
        ) as cur:
            return [dict(r) async for r in cur]

    async def admin_ids(self) -> set[int]:
        async with self.conn.execute("SELECT user_id FROM admins") as cur:
            return {r["user_id"] async for r in cur}

    # --- pool refs ---------------------------------------------------------

    async def save_pool_ref(self, row: dict) -> None:
        await self.conn.execute(
            "INSERT INTO pool_refs (mint, kind, program, accounts, decimals, updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(mint) DO UPDATE SET kind=excluded.kind, program=excluded.program, "
            "accounts=excluded.accounts, decimals=excluded.decimals, updated_at=excluded.updated_at",
            (
                row["mint"], row["kind"], row["program"],
                json.dumps(row["accounts"]), row["decimals"], time.time(),
            ),
        )
        await self.conn.commit()

    async def get_pool_ref(self, mint: str) -> dict | None:
        async with self.conn.execute("SELECT * FROM pool_refs WHERE mint = ?", (mint,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "mint": row["mint"],
            "kind": row["kind"],
            "program": row["program"],
            "accounts": json.loads(row["accounts"]),
            "decimals": row["decimals"],
        }

    async def delete_pool_ref(self, mint: str) -> None:
        await self.conn.execute("DELETE FROM pool_refs WHERE mint = ?", (mint,))
        await self.conn.commit()

    # --- alerts ------------------------------------------------------------

    async def record_alert(
        self, mint: str, window: str, drawdown: float, price: float, baseline: float,
        ts: float, message_id: int | None = None,
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO alerts (mint, window, drawdown, price, baseline, ts, message_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (mint, window, drawdown, price, baseline, ts, message_id),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def last_alert_ts(self, mint: str) -> float:
        async with self.conn.execute(
            "SELECT MAX(ts) AS ts FROM alerts WHERE mint = ?", (mint,)
        ) as cur:
            row = await cur.fetchone()
        return row["ts"] or 0.0

    async def alert_count_since(self, since_ts: float) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) AS n FROM alerts WHERE ts >= ?", (since_ts,)
        ) as cur:
            row = await cur.fetchone()
        return row["n"]
