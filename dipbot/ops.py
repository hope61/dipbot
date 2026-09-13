"""Operational safety: credit budget, feed health, daily summary.

These exist because the bot's failure modes are quiet ones. The feed stopping
looks exactly like a calm market, and a Helius allowance running out stops the
stream dead rather than degrading. Both need to announce themselves.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from .db import Database

log = logging.getLogger(__name__)

#: Helius bills WebSocket traffic at 20 credits per megabyte of streamed data.
CREDITS_PER_MB = 20
FREE_TIER_CREDITS = 1_000_000

SECONDS_PER_MONTH = 30 * 86_400


class BudgetGuard:
    """Tracks what the fallback endpoint would cost and stops before the cliff.

    Only fallback traffic is counted - the public endpoint is free. The failure
    this prevents is abrupt: at the credit limit the stream simply stops, with
    no warning and no degraded mode. Better to fall back to polling loudly.
    """

    def __init__(self, db: Database, feed, notify, budget: float = 0.8 * FREE_TIER_CREDITS):
        self.db = db
        self.feed = feed
        self.notify = notify
        self.budget = budget
        self._last_bytes = 0
        self.tripped = False

    async def _month_key(self) -> str:
        return datetime.now().strftime("%Y-%m")

    async def usage(self) -> float:
        stored_month = await self.db.get("credit_month", "")
        current = await self._month_key()
        if stored_month != current:
            # New billing month - the allowance resets, so should we.
            await self.db.set("credit_month", current)
            await self.db.set("credits_used", 0.0)
            self.tripped = False
            return 0.0
        return float(await self.db.get("credits_used", 0.0) or 0.0)

    async def sample(self) -> float:
        """Fold new bytes into the monthly tally. Returns credits used."""
        total = self.feed.bytes_received
        delta = max(0, total - self._last_bytes)
        self._last_bytes = total

        used = await self.usage()
        # Public-endpoint bytes are free; only meter what a provider would bill.
        if delta and self.feed.using_fallback:
            used += delta / 1e6 * CREDITS_PER_MB
            await self.db.set("credits_used", used)
        return used

    async def check(self) -> None:
        used = await self.sample()
        if self.tripped or used < self.budget:
            return
        self.tripped = True
        # Drop the fallback so the feed stays on the free endpoint. Alerts keep
        # working from DexScreener polling even if the primary is also down.
        self.feed.fallback_url = ""
        self.feed.using_fallback = False
        log.warning("credit budget reached (%.0f used), fallback disabled", used)
        await self.notify(
            f"⚠️ <b>RPC credit budget reached</b>\n\n"
            f"{used:,.0f} credits used this month. The paid fallback is now off; "
            f"the feed stays on the public endpoint.\n"
            f"<i>Alerts continue, but if the public endpoint drops there is no backup.</i>"
        )

    async def run_forever(self, interval: float = 300.0) -> None:
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("budget check failed")
            await asyncio.sleep(interval)


async def watching(db: Database | None) -> bool:
    """Is anything on the watchlist? Nothing is worth announcing if not.

    An empty watchlist means the feed being up or down changes nothing for the
    user, so the health and summary posts stay quiet rather than filling an
    idle channel with noise about a bot that has no work to do.
    """
    if db is None:
        return True
    return bool(await db.list_tokens())


class HealthMonitor:
    """Announces the feed going down and coming back.

    Without this, a dead feed is indistinguishable from a quiet market - the
    channel simply goes silent and nothing says why. With an empty watchlist
    there is nothing to miss, so neither message is posted; the down clock
    still runs, so a recovery is only announced if the drop was.
    """

    def __init__(self, feed, notify, down_after: float = 180.0, db: Database | None = None):
        self.feed = feed
        self.notify = notify
        self.down_after = down_after
        self.db = db
        self.announced_down = False
        self.down_since = 0.0

    async def check(self) -> None:
        if self.feed.connected:
            if self.announced_down:
                downtime = time.time() - self.down_since
                self.announced_down = False
                self.down_since = 0.0
                await self.notify(
                    f"✅ <b>Feed recovered</b> after {downtime / 60:.0f}m. "
                    f"Realtime alerts are back."
                )
            return

        if not self.down_since:
            self.down_since = time.time()
            return

        if not self.announced_down and time.time() - self.down_since >= self.down_after:
            if not await watching(self.db):
                return
            self.announced_down = True
            await self.notify(
                "🔴 <b>Realtime feed down</b>\n\n"
                "Reconnecting automatically. Until it returns, only 3m/5m alerts "
                "from polled data will fire — fast dips will be missed."
            )

    async def run_forever(self, interval: float = 30.0) -> None:
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("health check failed")
            await asyncio.sleep(interval)


@dataclass
class SummaryData:
    alerts: int
    tokens: int
    realtime: int
    feed_uptime_h: float
    megabytes: float
    reconnects: int
    dex_errors: int
    top: list[tuple[str, int]]
    credits_used: float


def render_summary(data: SummaryData) -> str:
    lines = [
        "📊 <b>Daily summary</b>",
        "",
        f"<b>Alerts</b>      {data.alerts}",
        f"<b>Tokens</b>      {data.tokens}  ({data.realtime} ⚡ / {data.tokens - data.realtime} 🐢)",
        f"<b>Feed</b>        {data.feed_uptime_h:.1f}h up · {data.reconnects} reconnects",
        f"<b>Streamed</b>    {data.megabytes:.1f} MB",
    ]
    if data.credits_used:
        lines.append(f"<b>Credits</b>     {data.credits_used:,.0f} used this month")
    if data.dex_errors:
        lines.append(f"<b>Dex errors</b>  {data.dex_errors}")
    if data.top:
        lines += ["", "<b>Most alerts</b>"]
        lines += [f"  ${symbol} — {count}" for symbol, count in data.top]
    elif data.alerts == 0:
        lines += ["", "<i>No dips crossed the thresholds today.</i>"]
    return "\n".join(lines)


def seconds_until(hhmm: str, now: datetime | None = None) -> float:
    """Seconds until the next occurrence of a 24h HH:MM time."""
    now = now or datetime.now()
    hour, _, minute = hhmm.partition(":")
    target = now.replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class DailySummary:
    def __init__(self, db: Database, feed, dex, notify, at: str, budget: BudgetGuard | None = None):
        self.db = db
        self.feed = feed
        self.dex = dex
        self.notify = notify
        self.at = at
        self.budget = budget

    async def collect(self) -> SummaryData:
        from .models import Tier

        tokens = await self.db.list_tokens()
        since = time.time() - 86_400
        stats = self.feed.stats()

        async with self.db.conn.execute(
            "SELECT mint, COUNT(*) AS n FROM alerts WHERE ts >= ? "
            "GROUP BY mint ORDER BY n DESC LIMIT 3",
            (since,),
        ) as cur:
            rows = [(r["mint"], r["n"]) async for r in cur]

        symbols = {t.mint: (t.symbol or t.mint[:6]) for t in tokens}
        return SummaryData(
            alerts=await self.db.alert_count_since(since),
            tokens=len(tokens),
            realtime=sum(1 for t in tokens if t.tier is Tier.REALTIME),
            feed_uptime_h=stats["uptime_s"] / 3600,
            megabytes=stats["megabytes"],
            reconnects=stats["reconnects"],
            dex_errors=self.dex.error_count,
            top=[(symbols.get(mint, mint[:6]), n) for mint, n in rows],
            credits_used=await self.budget.usage() if self.budget else 0.0,
        )

    async def post(self) -> None:
        await self.notify(render_summary(await self.collect()))

    async def run_forever(self) -> None:
        if not self.at:
            return
        log.info("daily summary scheduled for %s", self.at)
        while True:
            await asyncio.sleep(seconds_until(self.at))
            try:
                if await watching(self.db):
                    await self.post()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("daily summary failed")
            # Guard against firing twice inside the same minute.
            await asyncio.sleep(60)
