"""Environment-backed configuration.

Only credentials and infrastructure live here. Thresholds and filters are seeded
from .env on first run and then owned by the database, editable from /settings -
see db.Settings. Editing those values in .env after first run does nothing, which
is called out in .env.example.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = ("10s", "1m", "3m", "5m")
WINDOW_SECONDS = {"10s": 10, "1m": 60, "3m": 180, "5m": 300}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _req(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise ConfigError(f"{name} is required but missing from .env")
    return val


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from e


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from e


@dataclass(frozen=True)
class Config:
    bot_token: str
    channel_id: int
    owner_id: int

    rpc_http: str
    rpc_ws: str
    rpc_ws_fallback: str
    rpc_encoding: str

    poll_seconds: int
    user_agent: str

    database_path: Path
    log_level: str
    max_alerts_per_minute: int
    daily_summary_at: str
    credit_budget: float
    trojan_bot: str
    trojan_ref: str

    # seeds handed to the database on first run only
    seed: dict = field(default_factory=dict)

    @property
    def masked_rpc_http(self) -> str:
        """RPC URL safe to log - Helius embeds the key as a query param."""
        return self.rpc_http.split("?")[0] + ("?api-key=***" if "api-key=" in self.rpc_http else "")


def load(env_file: Path | None = None) -> Config:
    load_dotenv(env_file or ROOT / ".env")

    channel_raw = _req("TELEGRAM_CHANNEL_ID")
    try:
        channel_id = int(channel_raw)
    except ValueError as e:
        raise ConfigError(
            f"TELEGRAM_CHANNEL_ID must be the numeric id (e.g. -1001234567890), got {channel_raw!r}"
        ) from e

    owner_raw = _req("OWNER_USER_ID")
    if not owner_raw.isdigit():
        raise ConfigError(f"OWNER_USER_ID must be numeric, got {owner_raw!r}")

    db_path = Path(os.getenv("DATABASE_PATH", "./data/dipbot.sqlite").strip())
    if not db_path.is_absolute():
        db_path = ROOT / db_path

    seed = {
        "quote_mode": os.getenv("QUOTE_MODE", "SOL").strip().upper(),
        "enabled_windows": os.getenv("ENABLED_WINDOWS", "10s,1m,3m,5m").strip(),
        "cooldown_per_token_seconds": _int("COOLDOWN_PER_TOKEN_SECONDS", 600),
        "rearm_recovery_pct": _float("REARM_RECOVERY_PCT", 50),
        "warmup_seconds": _int("WARMUP_SECONDS", 60),
        "confirm_ticks": _int("CONFIRM_TICKS", 1),
        "min_liquidity_usd": _float("MIN_LIQUIDITY_USD", 10_000),
        "min_volume_24h_usd": _float("MIN_VOLUME_24H_USD", 50_000),
        "min_token_age_minutes": _int("MIN_TOKEN_AGE_MINUTES", 0),
        "outlier_median_multiple": _float("OUTLIER_MEDIAN_MULTIPLE", 3),
        "unknown_liquidity": os.getenv("UNKNOWN_LIQUIDITY", "proxy").strip().lower(),
        "filters_enabled": _bool("FILTERS_ENABLED", True),
        "filter_liquidity_enabled": _bool("FILTER_LIQUIDITY_ENABLED", True),
        "filter_volume_enabled": _bool("FILTER_VOLUME_ENABLED", True),
        "filter_age_enabled": _bool("FILTER_AGE_ENABLED", True),
    }
    for w in WINDOWS:
        key = w.upper()
        # Only bonded coins are alerted on, so there is a single profile. The
        # `migrated` key name is kept so existing databases keep their values.
        seed[f"threshold_migrated_{w}"] = _float(f"THRESHOLD_MIGRATED_{key}", 0)
        seed[f"min_ticks_{w}"] = _int(f"MIN_TICKS_{key}", 4)

    if seed["quote_mode"] not in ("SOL", "USD"):
        raise ConfigError(f"QUOTE_MODE must be SOL or USD, got {seed['quote_mode']!r}")

    if seed["unknown_liquidity"] not in ("proxy", "skip", "allow"):
        raise ConfigError(
            f"UNKNOWN_LIQUIDITY must be proxy, skip or allow, got {seed['unknown_liquidity']!r}"
        )

    bad = [w for w in seed["enabled_windows"].split(",") if w.strip() and w.strip() not in WINDOWS]
    if bad:
        raise ConfigError(f"ENABLED_WINDOWS contains unknown windows: {bad}. Valid: {list(WINDOWS)}")

    return Config(
        bot_token=_req("TELEGRAM_BOT_TOKEN"),
        channel_id=channel_id,
        owner_id=int(owner_raw),
        rpc_http=os.getenv("SOLANA_RPC_HTTP", "https://api.mainnet-beta.solana.com").strip(),
        rpc_ws=os.getenv("SOLANA_RPC_WS", "wss://api.mainnet-beta.solana.com").strip(),
        rpc_ws_fallback=os.getenv("SOLANA_RPC_WS_FALLBACK", "").strip(),
        # zstd measured 518 B -> 431 B per notification. Helius bills by data
        # volume, so this is a straight ~17% discount when on the fallback.
        rpc_encoding=os.getenv("SOLANA_RPC_ENCODING", "base64+zstd").strip(),
        poll_seconds=_int("DEXSCREENER_POLL_SECONDS", 20),
        # DexScreener 403s python's default UA, so a browser-ish string is required.
        user_agent=os.getenv(
            "DEXSCREENER_USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        ).strip(),
        database_path=db_path,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        max_alerts_per_minute=_int("MAX_ALERTS_PER_MINUTE", 15),
        daily_summary_at=os.getenv("DAILY_SUMMARY_AT", "").strip(),
        # Stop using the paid fallback at this many credits. Default is 80% of
        # the Helius free tier, leaving headroom rather than hitting the wall.
        credit_budget=_float("CREDIT_BUDGET", 800_000),
        trojan_bot=os.getenv("TROJAN_BOT", "solana_trojanbot").strip().lstrip("@"),
        trojan_ref=os.getenv("TROJAN_REF", "").strip(),
        seed=seed,
    )
