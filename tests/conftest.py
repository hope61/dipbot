"""Shared fixtures.

Everything here is offline: no network, no Telegram, no real .env. Tests that
need live API behaviour belong in spike/, not in the suite.
"""
from __future__ import annotations

import time

import pytest
import pytest_asyncio

from dipbot.db import Database
from dipbot.models import TokenMeta, Tier

DEFAULT_SETTINGS = {
    "min_liquidity_usd": 10_000.0,
    "min_volume_24h_usd": 50_000.0,
    "min_token_age_minutes": 0,
    "unknown_liquidity": "proxy",
    "quote_mode": "SOL",
    "enabled_windows": "10s,1m,3m,5m",
    "cooldown_per_token_seconds": 600,
    "rearm_recovery_pct": 50.0,
    "warmup_seconds": 60,
    "confirm_ticks": 1,
    "outlier_median_multiple": 3.0,
    "threshold_migrated_10s": 8.0,
    "threshold_migrated_1m": 15.0,
    "threshold_migrated_3m": 22.0,
    "threshold_migrated_5m": 30.0,
    "min_ticks_10s": 3,
    "min_ticks_1m": 8,
    "min_ticks_3m": 15,
    "min_ticks_5m": 20,
}


@pytest.fixture
def settings() -> dict:
    return dict(DEFAULT_SETTINGS)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.sqlite")
    await database.connect(seed=DEFAULT_SETTINGS)
    yield database
    await database.close()


def make_meta(**overrides) -> TokenMeta:
    """A healthy token that passes every filter unless overridden."""
    base = dict(
        mint="Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump",
        pair_address="HMzvsEEmtzHhvZNw9uwbaG85HCTmFnkbhzUx16cy7ca3",
        symbol="CATE",
        name="Cate",
        dex_id="pumpswap",
        price_usd=0.0679,
        price_native=0.00069,
        liquidity_usd=2_300_000.0,
        volume_h24=23_000_000.0,
        volume_m5=15_000.0,
        market_cap=65_000_000.0,
        pair_created_at=int((time.time() - 3600 * 24) * 1000),
        txns_m5_buys=70,
        txns_m5_sells=79,
        updated_at=time.time(),
    )
    base.update(overrides)
    return TokenMeta(**base)


@pytest.fixture
def meta() -> TokenMeta:
    return make_meta()


@pytest.fixture
def memedex() -> TokenMeta:
    """The real problem case: no liquidity figure, but genuinely traded."""
    return make_meta(
        mint="memeADDB1k6PwmvJsCJVoQnKdK7689JWa1tDVMwrvUo",
        symbol="MEMEDEX",
        name="Memedex",
        dex_id="meteoradbc",
        liquidity_usd=None,
        volume_h24=141_491.87,
        market_cap=185_250.1,
        price_usd=18.52,
    )


__all__ = ["DEFAULT_SETTINGS", "make_meta", "Tier"]
