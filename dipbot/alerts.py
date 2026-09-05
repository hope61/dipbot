"""Channel sender: formatting and rate-limited delivery.

Telegram allows roughly 20 messages/min to a channel. When alerts arrive faster
than the configured budget they are coalesced into one digest rather than
dropped - losing a dip alert silently is worse than batching it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.markdown import hcode

from .models import TokenMeta, Tier

log = logging.getLogger(__name__)

#: Trojan trading bot deep link.
#:
#: Trojan's start payload is `r-<code>-<mint>`, and a bare mint does not work -
#: the `r-<code>-` segment is part of the format, not just a referral tag. So
#: "no referral" is not achievable through this link; the choice is whose code
#: sits there. Both the bot and the code are configurable.
TROJAN_BOT = "solana_trojanbot"
TROJAN_REF = ""


def trojan_url(mint: str, bot: str = "", ref: str = "") -> str:
    """Deep link that opens `mint` in Trojan.

    With a code: `?start=r-<code>-<mint>` (the format Trojan expects).
    Without one: `?start=<mint>`, which Trojan was observed to reject - kept
    only so an empty setting degrades to something rather than nothing.
    """
    bot = (bot or TROJAN_BOT).lstrip("@")
    ref = ref or TROJAN_REF
    payload = f"r-{ref}-{mint}" if ref else mint
    return f"https://t.me/{bot}?start={payload}"


def fmt_usd(v: float | None) -> str:
    if v is None:
        return "?"
    if v == 0:
        return "$0"
    for cutoff, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= cutoff:
            return f"${v / cutoff:.2f}{suffix}"
    if abs(v) >= 1:
        return f"${v:,.2f}"
    # Sub-dollar values need the long form, but stripping trailing zeros can
    # leave a dangling point ("$0." for zero, "$1." for 1.00000000).
    return f"${v:.8f}".rstrip("0").rstrip(".")


def fmt_compact(v: float | None) -> str:
    """Money the way the alert format asks for it: 122k, 60k, 1.2M.

    Whole numbers once past 10, one decimal below that so a 4.4k coin does not
    collapse to 4k. No currency symbol - the scale is what gets read.
    """
    if v is None:
        return "?"
    if v == 0:
        return "0"
    for cutoff, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= cutoff:
            scaled = v / cutoff
            text = f"{scaled:.0f}" if abs(scaled) >= 10 else f"{scaled:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    if abs(v) >= 1:
        return f"{v:.0f}"
    return f"{v:.6f}".rstrip("0").rstrip(".")


def fmt_age(minutes: float | None) -> str:
    if minutes is None:
        return "?"
    if minutes < 60:
        return f"{minutes:.0f}m"
    # Integer division, not rounding: 90 minutes is "1h 30m", not "2h 30m".
    if minutes < 1440:
        return f"{int(minutes // 60)}h {int(minutes % 60)}m"
    days, hours = int(minutes // 1440), int((minutes % 1440) // 60)
    return f"{days}d {hours}h" if hours else f"{days}d"


@dataclass
class Alert:
    mint: str
    window: str
    drawdown: float
    price_sol: float
    baseline_sol: float
    tier: Tier
    meta: TokenMeta | None = None
    ts: float = 0.0
    #: Market caps actually observed by the poller, newest and highest within
    #: the alert window. None when we have no observation to quote.
    mcap_now: float | None = None
    mcap_peak: float | None = None


def market_cap_move(alert: Alert) -> tuple[float, float] | None:
    """The observed market cap fall, or None if we did not observe one.

    Deliberately not derived. An earlier version multiplied the current cap by
    the price ratio, which produced figures that never occurred - a coin was
    reported falling from $122.93k when its actual high that minute was $56.39k.
    Both ends must be values the poller really saw.
    """
    if alert.mcap_peak is None or alert.mcap_now is None:
        return None
    if alert.mcap_peak <= alert.mcap_now:
        return None
    return alert.mcap_peak, alert.mcap_now


def display_name(alert: Alert) -> str:
    """The coin's name, which reads better than a one-letter ticker."""
    meta = alert.meta
    if meta and meta.name:
        return meta.name
    if meta and meta.symbol:
        return meta.symbol
    return alert.mint[:6]


def render(alert: Alert) -> str:
    meta = alert.meta
    lines = [f"{display_name(alert)} -{alert.drawdown:.0f}% in {alert.window}", ""]

    # Both ends come from the live price feed, scaled to the market cap shown
    # on a chart. An earlier version derived the first from a stale cap times a
    # live ratio and reported a fall from 122.93k on a coin whose real high
    # that minute was 56.39k.
    move = market_cap_move(alert)
    current = alert.mcap_now if alert.mcap_now is not None else (meta.market_cap if meta else None)
    if move:
        peak, now = move
        lines.append(f"MC: {fmt_compact(peak)} -> {fmt_compact(now)}")
    elif current:
        lines.append(f"MC: {fmt_compact(current)}")

    if meta:
        lines.append(f"Age: {fmt_age(meta.age_minutes)}")
        lines.append(f"Vol 5m: {fmt_compact(meta.volume_m5)}")
        lines.append(f"Vol 24h: {fmt_compact(meta.volume_h24)}")
    else:
        lines.append("no market data yet")

    lines += ["", hcode(alert.mint)]
    return "\n".join(lines)


def render_added(meta: TokenMeta, added_by: str) -> str:
    """The message posted to the channel when someone adds a coin.

    Same field layout as an alert so the channel reads consistently - only the
    header differs, since nothing has dropped yet.
    """
    name = meta.name or meta.symbol or meta.mint[:6]
    lines = [f"{name} added by {added_by}", ""]

    if meta.market_cap:
        lines.append(f"MC: {fmt_compact(meta.market_cap)}")
    lines.append(f"Age: {fmt_age(meta.age_minutes)}")
    lines.append(f"Vol 5m: {fmt_compact(meta.volume_m5)}")
    lines.append(f"Vol 24h: {fmt_compact(meta.volume_h24)}")

    lines += ["", hcode(meta.mint)]
    return "\n".join(lines)


def links_keyboard(mint: str, pair_address: str) -> InlineKeyboardMarkup:
    """The link row, shared by alerts and add announcements."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Chart", url=f"https://dexscreener.com/solana/{pair_address}"),
                InlineKeyboardButton(text="pump.fun", url=f"https://pump.fun/coin/{mint}"),
                InlineKeyboardButton(text="Trojan", url=trojan_url(mint)),
            ]
        ]
    )


def keyboard(alert: Alert) -> InlineKeyboardMarkup:
    pair = alert.meta.pair_address if alert.meta else alert.mint
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Chart", url=f"https://dexscreener.com/solana/{pair}"),
                InlineKeyboardButton(text="pump.fun", url=f"https://pump.fun/coin/{alert.mint}"),
                InlineKeyboardButton(text="Trojan", url=trojan_url(alert.mint)),
            ],
            [
                InlineKeyboardButton(text="🔇 Mute 1h", callback_data=f"mute:{alert.mint}:3600"),
                InlineKeyboardButton(text="🗑 Remove", callback_data=f"remove:{alert.mint}"),
            ],
        ]
    )


class AlertSender:
    """Token-bucket sender. Coalesces overflow instead of dropping it."""

    def __init__(self, bot: Bot, channel_id: int, max_per_minute: int = 15):
        self.bot = bot
        self.channel_id = channel_id
        self.max_per_minute = max_per_minute
        self._sent_times: list[float] = []
        self._pending: list[Alert] = []
        self._lock = asyncio.Lock()
        self.sent_count = 0
        self.error_count = 0

    def _budget_left(self) -> int:
        cutoff = time.time() - 60
        self._sent_times = [t for t in self._sent_times if t > cutoff]
        return self.max_per_minute - len(self._sent_times)

    async def _send(self, text: str, markup: InlineKeyboardMarkup | None = None) -> int | None:
        try:
            msg = await self.bot.send_message(
                self.channel_id, text, reply_markup=markup,
                parse_mode="HTML", disable_web_page_preview=True,
            )
            self._sent_times.append(time.time())
            self.sent_count += 1
            return msg.message_id
        except TelegramRetryAfter as e:
            log.warning("flood control, retrying in %ss", e.retry_after)
            await asyncio.sleep(e.retry_after)
            return await self._send(text, markup)
        except Exception as e:
            self.error_count += 1
            log.error("send failed: %s", e)
            return None

    async def send_alert(self, alert: Alert) -> int | None:
        async with self._lock:
            if self._budget_left() > 0:
                return await self._send(render(alert), keyboard(alert))
            self._pending.append(alert)
            log.info("rate limit reached, queued %s for digest", alert.mint[:8])
            return None

    async def flush_digest(self) -> int | None:
        """Emit queued overflow as a single message."""
        async with self._lock:
            if not self._pending or self._budget_left() <= 0:
                return None
            batch, self._pending = self._pending, []
            lines = [f"🔻 <b>{len(batch)} more dips</b> (rate limited)", ""]
            for a in batch:
                sym = a.meta.display_symbol if a.meta else a.mint[:6]
                lines.append(f"${sym} −{a.drawdown:.1f}% ({a.window}) {a.tier.badge}")
            return await self._send("\n".join(lines))

    async def send_text(self, text: str, markup: InlineKeyboardMarkup | None = None) -> int | None:
        async with self._lock:
            return await self._send(text, markup)
