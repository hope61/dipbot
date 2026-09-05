"""Shared value types.

`PriceTick` is the seam the whole detector is written against. Phase 1 only
produces polled ticks from DexScreener; Phase 2 adds per-trade ticks from RPC
without anything downstream changing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum


#: DexScreener names the pump.fun bonding curve inconsistently: `pump` on the
#: pair endpoint, `pumpfun` on the token endpoints. Both mean "pre-migration
#: launch curve", so both have to be recognised - a missed one reads as a real
#: pool and its frozen post-migration snapshot gets published as live data.
BONDING_DEX_IDS = frozenset({"pump", "pumpfun"})


class Tier(str, Enum):
    """How fresh a token's price data can possibly be.

    REALTIME tokens are priced from chain state on every trade. POLLED tokens
    come from DexScreener, whose CDN caches for 30s - they must never be offered
    the 10s or 1m windows, because the data cannot support them.
    """

    REALTIME = "realtime"
    POLLED = "polled"

    @property
    def badge(self) -> str:
        return "⚡" if self is Tier.REALTIME else "🐢"

    @property
    def allowed_windows(self) -> tuple[str, ...]:
        return ("10s", "1m", "3m", "5m") if self is Tier.REALTIME else ("3m", "5m")


class PriceSource(str, Enum):
    BONDING_CURVE = "bonding_curve"  # single account, atomic
    POOL_VAULTS = "pool_vaults"  # two accounts, needs slot grouping
    DEXSCREENER = "dexscreener"  # polled, up to 30s stale


@dataclass(frozen=True)
class PriceTick:
    mint: str
    price_sol: float
    ts: float
    source: PriceSource
    slot: int | None = None

    @classmethod
    def now(cls, mint: str, price_sol: float, source: PriceSource, slot: int | None = None):
        return cls(mint=mint, price_sol=price_sol, ts=time.time(), source=source, slot=slot)


@dataclass
class TokenMeta:
    """DexScreener's view of a token. Refreshed by the poller."""

    mint: str
    pair_address: str
    symbol: str | None = None
    name: str | None = None
    dex_id: str | None = None
    price_usd: float | None = None
    price_native: float | None = None
    liquidity_usd: float | None = None
    volume_h24: float | None = None
    volume_m5: float | None = None
    market_cap: float | None = None
    pair_created_at: int | None = None  # ms epoch
    txns_m5_buys: int | None = None
    txns_m5_sells: int | None = None
    updated_at: float = 0.0

    @property
    def age_minutes(self) -> float | None:
        if not self.pair_created_at:
            return None
        return (time.time() - self.pair_created_at / 1000) / 60

    @property
    def display_symbol(self) -> str:
        return self.symbol or self.mint[:6]

    @property
    def is_bonding_curve(self) -> bool:
        """Pre-migration pump.fun tokens still trade on their bonding curve."""
        return self.dex_id in BONDING_DEX_IDS


@dataclass
class WatchedToken:
    mint: str
    pair_address: str
    symbol: str | None
    tier: Tier
    added_at: float
    enabled: bool = True
    muted_until: float = 0.0
    overrides: dict | None = None

    @property
    def is_muted(self) -> bool:
        return self.muted_until > time.time()
