"""Entrypoint: wires config, database, feeds and the Telegram bot together.

Phase 1 scope - watchlist management, DexScreener polling, and channel delivery.
The realtime RPC feed (Phase 2) and the dip detector (Phase 3) plug into the
same `PriceTick` seam without changing anything here.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from dataclasses import replace

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from . import config as config_mod
from . import alerts as alerts_mod
from .alerts import AlertSender
from .bot import commands as bot_commands
from .bot import handlers
from .bot.middleware import AccessMiddleware
from .db import Database
from .detector import WINDOW_SECONDS as detector_windows, Detector
from .feeds.dexscreener import DexScreener
from .feeds.pool_resolver import PoolResolver
from .feeds.rpc_feed import RpcFeed
from .feeds.solana_rpc import SolanaRpc
from .feeds.supervisor import FeedSupervisor
from .models import PriceTick, Tier
from .ops import BudgetGuard, DailySummary, HealthMonitor
from .poller import Poller
from .sanity import PriceSanity

log = logging.getLogger("dipbot")

#: How often the diagnostics task reports feed and detector state.
DIAGNOSTIC_INTERVAL = 60.0


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def run() -> None:
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        print("Copy .env.example to .env and fill in the REQUIRED block.", file=sys.stderr)
        raise SystemExit(2)

    setup_logging(cfg.log_level)
    log.info("starting dipbot")
    log.info("rpc http  %s", cfg.masked_rpc_http)
    log.info("rpc ws    %s", cfg.rpc_ws)

    db = Database(cfg.database_path)
    seeded = await db.connect(seed=cfg.seed)
    log.info("database  %s (%d settings seeded)", cfg.database_path, seeded)

    dex = DexScreener(user_agent=cfg.user_agent)
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode="HTML"))

    me = await bot.get_me()
    log.info("bot       @%s", me.username)

    # Populates the ✎ menu button and `/` autocomplete in Telegram.
    await bot_commands.register(bot, cfg.owner_id)

    # Trojan's deep link needs an `r-<code>-` prefix; see alerts.trojan_url.
    alerts_mod.TROJAN_BOT = cfg.trojan_bot
    alerts_mod.TROJAN_REF = cfg.trojan_ref
    log.info(
        "trojan    @%s%s", cfg.trojan_bot,
        f" (ref {cfg.trojan_ref})" if cfg.trojan_ref else " (no ref - link may not resolve)",
    )

    sender = AlertSender(bot, cfg.channel_id, cfg.max_alerts_per_minute)

    sanity = PriceSanity()
    detector = Detector(await db.all_settings())
    detector.overrides = await db.all_overrides()
    _watched = await db.list_tokens()
    tiers: dict[str, Tier] = {t.mint: t.tier for t in _watched}

    muted_tokens: dict[str, float] = {}

    def quoted(price: float) -> float:
        """Apply the quote mode to a raw SOL price.

        USD mode reprices in dollars so a SOL-wide move shows up as every coin
        dipping at once; SOL mode isolates the coin's own move. Detection and
        market-cap calibration must both use this, or the two end up on
        different scales - which once made every cap ~98x too high.
        """
        if str(detector.settings.get("quote_mode", "SOL")).upper() == "USD" and poller.sol_usd:
            return price * poller.sol_usd
        return price

    #: Ticks actually delivered to the detector, per mint. A coin the detector
    #: never hears from cannot alert, and that looks identical from outside to
    #: a coin whose dip missed the threshold.
    tick_counts: dict[str, int] = {}

    async def on_tick(tick: PriceTick) -> None:
        tick_counts[tick.mint] = tick_counts.get(tick.mint, 0) + 1
        if await db.get("muted_until", 0) > time.time():
            return
        # Realtime tokens are unsubscribed when muted, but polled ones keep
        # arriving - so the per-token mute has to be enforced here too.
        if muted_tokens.get(tick.mint, 0) > time.time():
            return

        tick = replace(tick, price_sol=quoted(tick.price_sol))

        meta = await db.get_meta(tick.mint)
        alert = detector.on_tick(tick, meta=meta, tier=tiers.get(tick.mint, Tier.REALTIME))
        if alert is None:
            return

        # Never publish a number we cannot corroborate. If our chain price has
        # been persistently disagreeing with DexScreener, we are reading the
        # wrong account and every figure in the alert would be wrong.
        if not sanity.is_trusted(alert.mint):
            log.warning(
                "suppressed %s alert: price disagrees with DexScreener by %.1fx",
                meta.display_symbol if meta else alert.mint[:8], sanity.ratio(alert.mint) or 0,
            )
            return

        # Price both ends of the fall from the live feed, scaled to the market
        # cap the user sees on a chart. Instant, and internally consistent -
        # the cap change equals the headline percentage by construction.
        alert.mcap_now = sanity.market_cap(alert.mint, alert.price_sol)
        alert.mcap_peak = sanity.market_cap(alert.mint, alert.baseline_sol)

        # Last line of defence. The computed cap must land near the last cap we
        # actually saw; a large gap means the calibration is on the wrong scale
        # and every figure would be wrong. This exact fault once published a
        # 27M cap for a coin worth 171k.
        if alert.mcap_now and meta and meta.market_cap:
            drift = alert.mcap_now / meta.market_cap
            if drift > 5 or drift < 0.2:
                log.error(
                    "%s computed cap %.0f is %.1fx the reported %.0f - dropping "
                    "the market cap figures rather than publishing them",
                    meta.display_symbol, alert.mcap_now, drift, meta.market_cap,
                )
                alert.mcap_now = meta.market_cap
                alert.mcap_peak = None
                sanity.forget(alert.mint)

        if alert.mcap_now is None and meta:
            # Not calibrated yet: fall back to the last polled cap alone.
            alert.mcap_now = meta.market_cap

        message_id = await sender.send_alert(alert)
        await db.record_alert(
            alert.mint, alert.window, alert.drawdown, alert.price_sol,
            alert.baseline_sol, alert.ts, message_id,
        )
        log.info(
            "ALERT %s %s -%.1f%%", (meta.display_symbol if meta else tick.mint[:8]),
            alert.window, alert.drawdown,
        )

    async def diagnostics() -> None:
        """Log why alerts are or are not happening.

        Without this, "the bot never alerts" is indistinguishable from "no coin
        dipped far enough", and both look like silence in the log.
        """
        last: dict[str, int] = {}
        while True:
            await asyncio.sleep(DIAGNOSTIC_INTERVAL)
            try:
                f = feed.stats()
                log.info(
                    "feed connected=%s accounts=%d tokens=%d ticks=%d notif=%d reconnects=%d",
                    f["connected"], f["accounts"], f["tokens"], f["ticks"],
                    f["notifications"], f["reconnects"],
                )
                d = detector.stats()
                log.info(
                    "detector tokens=%d buffered=%d suppressed=%s",
                    d["tokens"], d["buffered_ticks"], d["suppressed"] or "{}",
                )
                # Per-token tick rate over the interval. A watched coin sitting
                # at ~3 ticks a minute is on polled data only, whatever its tier
                # says, and the fast windows can never reach their tick floor.
                rates = []
                for token in await db.list_tokens():
                    total = tick_counts.get(token.mint, 0)
                    delta = total - last.get(token.mint, 0)
                    last[token.mint] = total
                    rates.append(f"{token.symbol or token.mint[:6]}={delta}")
                log.info("ticks/%.0fs %s", DIAGNOSTIC_INTERVAL, " ".join(rates))
            except Exception:
                log.exception("diagnostics failed")

    async def refresh_detector() -> None:
        """Pick up settings edits and tier changes without a restart."""
        while True:
            await asyncio.sleep(30)
            try:
                detector.settings = await db.all_settings()
                detector.overrides = await db.all_overrides()
                watched = await db.list_tokens()
                tiers.clear()
                tiers.update({t.mint: t.tier for t in watched})
                muted_tokens.clear()
                muted_tokens.update({t.mint: t.muted_until for t in watched if t.muted_until})
                current = {t.mint for t in watched}
                for mint in set(sanity.stats()["distrusted"]) - current:
                    sanity.forget(mint)
                await sender.flush_digest()
            except Exception:
                log.exception("detector refresh failed")

    async def cross_check(meta) -> None:
        """Compare our chain price with DexScreener's on every poll."""
        chain = feed.last_price.get(meta.mint)
        if not chain:
            return
        if meta.price_native:
            sanity.observe(meta.mint, chain, float(meta.price_native))
        if meta.market_cap:
            # Anchor the reported cap to our price right now, so a quoted cap
            # is the chart's number moved by our own price change since.
            sanity.observe_cap(meta.mint, quoted(chain), meta.market_cap)

    poller = Poller(db, dex, interval=cfg.poll_seconds, on_tick=on_tick, on_meta=cross_check)

    rpc = SolanaRpc(cfg.rpc_http)
    feed = RpcFeed(
        ws_url=cfg.rpc_ws,
        fallback_url=cfg.rpc_ws_fallback,
        on_tick=on_tick,
        encoding=cfg.rpc_encoding,
    )
    supervisor = FeedSupervisor(db, PoolResolver(rpc), feed)

    async def notify(text: str) -> None:
        await sender.send_text(text)

    budget = BudgetGuard(db, feed, notify, budget=cfg.credit_budget)
    health = HealthMonitor(feed, notify, db=db)
    summary = DailySummary(db, feed, dex, notify, cfg.daily_summary_at, budget)

    handlers.deps = handlers.Deps(
        db=db, dex=dex, sender=sender, owner_id=cfg.owner_id,
        feed=feed, rpc=rpc, detector=detector, summary=summary, sanity=sanity,
    )

    dp = Dispatcher()
    gate = AccessMiddleware(cfg.owner_id, db)
    dp.message.outer_middleware(gate)
    dp.callback_query.outer_middleware(gate)
    dp.include_router(handlers.router)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - windows
            pass

    tasks = [
        asyncio.create_task(poller.run_forever(), name="poller"),
        asyncio.create_task(feed.run_forever(), name="rpc-feed"),
        asyncio.create_task(supervisor.run_forever(), name="feed-supervisor"),
        asyncio.create_task(refresh_detector(), name="detector-refresh"),
        asyncio.create_task(diagnostics(), name="diagnostics"),
        asyncio.create_task(budget.run_forever(), name="budget-guard"),
        asyncio.create_task(health.run_forever(), name="health-monitor"),
        asyncio.create_task(summary.run_forever(), name="daily-summary"),
        asyncio.create_task(dp.start_polling(bot, handle_signals=False), name="telegram"),
    ]

    log.info("running - send /start to @%s", me.username)
    await stop.wait()

    log.info("shutting down")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await dex.close()
    await rpc.close()
    await bot.session.close()
    await db.close()
    log.info("stopped")


def cli() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
