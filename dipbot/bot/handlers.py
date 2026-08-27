"""Owner-only Telegram command handlers.

Every handler is gated on OWNER_USER_ID. Anyone else gets one short refusal and
nothing else - the bot is single-owner by design.
"""
from __future__ import annotations

import html
import logging
import re
import time

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.markdown import hcode

from ..alerts import Alert, AlertSender, fmt_age, fmt_usd, links_keyboard, render_added
from ..db import Database
from ..filters import filters_enabled as filter_switch
from . import guide as guide_ui
from . import settings_ui as ui
from ..feeds.dexscreener import DexScreener, tier_for
from ..feeds.geckoterminal import implied_supply
from ..models import Tier

log = logging.getLogger(__name__)
router = Router()

HELP = """<b>Dipbot</b> — Solana dip alerts

<b>Watchlist</b>
/add &lt;mint | pair | url&gt; — track a coin
<i>(or just paste an address)</i>
/remove &lt;symbol | mint&gt; — stop tracking one
/clear — stop tracking everything
/list — show watchlist

<b>Control</b>
/status — feed health and counters
/settings — show current settings
/filters &lt;on|off&gt; — apply or ignore token filters
/set &lt;setting&gt; &lt;value&gt; — change a setting
/token &lt;symbol&gt; — per-token overrides
/mute &lt;duration&gt; — silence all alerts (e.g. 30m, 2h)
/unmute — resume
/summary — post the daily summary now
/admins — who can control the bot
/guide — full guide
/test — post a sample alert to the channel

Windows: 10s / 1m / 3m / 5m
⚡ realtime (per-trade)  ·  🐢 polled (30s cache, 3m/5m only)
⏳ not bonded yet — tracked once it bonds
"""

DURATION = re.compile(r"^(\d+)\s*([smhd])$", re.I)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def mention(user) -> str:
    """Tag a user so the channel shows who added a coin.

    A username is cleaner when they have one; otherwise a tg:// link still
    resolves to the person, which a bare name would not.
    """
    if getattr(user, "username", None):
        return f"@{user.username}"
    name = html.escape(getattr(user, "full_name", None) or str(user.id))
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def parse_duration(text: str) -> int | None:
    m = DURATION.match(text.strip())
    if not m:
        return None
    return int(m.group(1)) * UNIT_SECONDS[m.group(2).lower()]


class Deps:
    """Wiring handed to handlers by main.py."""

    def __init__(
        self,
        db: Database,
        dex: DexScreener,
        sender: AlertSender,
        owner_id: int,
        feed=None,
        rpc=None,
        detector=None,
        summary=None,
        sanity=None,
        gecko=None,
    ):
        self.db = db
        self.dex = dex
        self.sender = sender
        self.owner_id = owner_id
        self.feed = feed
        self.rpc = rpc
        self.detector = detector
        self.summary = summary
        self.sanity = sanity
        self.gecko = gecko
        self.started_at = time.time()


deps: Deps | None = None


def _deps() -> Deps:
    if deps is None:
        raise RuntimeError("handlers.deps was never initialised")
    return deps


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        HELP, parse_mode="HTML", reply_markup=guide_ui.start_keyboard()
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        HELP, parse_mode="HTML", reply_markup=guide_ui.start_keyboard()
    )


@router.message(Command("guide"))
async def cmd_guide(message: Message) -> None:
    await message.answer(
        guide_ui.menu_text(), parse_mode="HTML", reply_markup=guide_ui.menu_keyboard()
    )


@router.callback_query(F.data.startswith("guide:"))
async def cb_guide(query: CallbackQuery) -> None:
    key = query.data.split(":", 1)[1]

    if key == guide_ui.MENU:
        text, markup = guide_ui.menu_text(), guide_ui.menu_keyboard()
    elif key in guide_ui.PAGES:
        text, markup = guide_ui.PAGES[key][1], guide_ui.page_keyboard()
    else:
        await query.answer("Unknown page.")
        return

    try:
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        # Telegram rejects an edit that would leave the message unchanged.
        pass
    await query.answer()


async def add_coin(message: Message, raw: str, quiet: bool = False) -> bool:
    """Resolve `raw` and start watching it. Returns True if newly added.

    `quiet` suppresses the "couldn't read that" reply, so a pasted address that
    turns out not to be a coin doesn't scold someone for typing in the chat.
    """
    d = _deps()

    address = DexScreener.parse_input(raw)
    if not address:
        if not quiet:
            await message.answer(
                "Couldn't read an address there. Paste a mint, pair address, or DexScreener link."
            )
        return False

    meta = await d.dex.resolve(address)
    if not meta:
        await message.answer(
            "No Solana pair found for that address.\n"
            "If the coin launched seconds ago, DexScreener may not have indexed it yet — "
            "try again shortly."
        )
        return False

    added = await d.db.add_token(meta.mint, meta.pair_address, meta.symbol, tier_for(meta))
    await d.db.upsert_meta(meta)

    if not added:
        await message.answer("Already on the watchlist — details refreshed.")
        return False

    # Seed a real all-time high from the pool's own history. Without it the
    # peak would only ever be "highest since we started watching", which
    # understates for any coin added after its top.
    if d.gecko:
        supply = implied_supply(meta.market_cap, meta.price_usd)
        try:
            peak = await d.gecko.peak_market_cap(meta.pair_address, supply or 0)
        except Exception:
            peak = None
        if peak:
            await d.db.set_ath(meta.mint, peak)

    # Read back what was stored: the peak only exists once the row is written.
    # A coin removed and re-added keeps the peak from its earlier stint.
    meta = await d.db.get_meta(meta.mint) or meta

    # The channel is the shared record of what is being watched and who put it
    # there, so the details go there rather than into a private chat.
    posted = await d.sender.send_text(
        render_added(meta, mention(message.from_user)),
        links_keyboard(meta.mint, meta.pair_address),
    )
    name = meta.name or meta.display_symbol
    if posted:
        await message.answer(f"Added <b>{name}</b> — posted to the channel.", parse_mode="HTML")
    else:
        await message.answer(
            f"Added <b>{name}</b>, but posting to the channel failed. "
            "Check the bot is still an admin there.",
            parse_mode="HTML",
        )
    return True


@router.message(Command("add"))
async def cmd_add(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Usage: <code>/add &lt;mint | pair | dexscreener url&gt;</code>\n"
            "<i>Or just paste the address — no command needed.</i>",
            parse_mode="HTML",
        )
        return
    await message.answer("Resolving…")
    await add_coin(message, parts[1])


@router.message(Command("remove"))
async def cmd_remove(message: Message) -> None:
    d = _deps()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: <code>/remove &lt;symbol | mint&gt;</code>", parse_mode="HTML")
        return
    token = await d.db.find_token(parts[1].strip())
    if not token:
        await message.answer("Not on the watchlist.")
        return
    await d.db.remove_token(token.mint)
    await message.answer(f"Removed <b>${token.symbol or token.mint[:6]}</b>.", parse_mode="HTML")


@router.message(Command("clear"))
async def cmd_clear(message: Message) -> None:
    """Remove every coin at once. Asks first - it cannot be undone."""
    d = _deps()
    tokens = await d.db.list_tokens()
    if not tokens:
        await message.answer("Watchlist is already empty.")
        return

    names = ", ".join(t.symbol or t.mint[:6] for t in tokens[:8])
    if len(tokens) > 8:
        names += f" and {len(tokens) - 8} more"

    await message.answer(
        f"Remove all <b>{len(tokens)}</b> coins?\n\n{names}\n\n"
        "<i>Settings and admins are untouched. Recorded all-time highs are kept, "
        "so re-adding a coin restores its peak.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text=f"🗑 Remove all {len(tokens)}", callback_data="clear:yes"),
                InlineKeyboardButton(text="Cancel", callback_data="clear:no"),
            ]]
        ),
    )


@router.callback_query(F.data == "clear:no")
async def cb_clear_cancel(query: CallbackQuery) -> None:
    try:
        await query.message.edit_text("Cancelled — nothing was removed.")
    except Exception:
        pass
    await query.answer()


@router.callback_query(F.data == "clear:yes")
async def cb_clear_confirm(query: CallbackQuery) -> None:
    d = _deps()
    removed = await d.db.clear_watchlist()
    log.info("watchlist cleared by %s (%d coins)", query.from_user.id, removed)
    try:
        await query.message.edit_text(
            f"Removed <b>{removed}</b> coins. The watchlist is empty.", parse_mode="HTML"
        )
    except Exception:
        pass
    await query.answer(f"Removed {removed}")


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    d = _deps()
    tokens = await d.db.list_tokens()
    if not tokens:
        await message.answer("Watchlist is empty. Add one with <code>/add &lt;mint&gt;</code>.", parse_mode="HTML")
        return

    lines = [f"<b>Watchlist</b> ({len(tokens)})", ""]
    for t in tokens:
        meta = await d.db.get_meta(t.mint)
        # A pre-bond coin is on the watchlist but deliberately not tracked -
        # say so, rather than letting it look like a silent failure.
        waiting = bool(meta and meta.is_bonding_curve)
        badge = "⏳" if waiting else t.tier.badge
        bits = [f"{badge} <b>${t.symbol or t.mint[:6]}</b>"]
        if waiting:
            bits.append("<i>not bonded — tracking starts when it bonds</i>")
        elif meta:
            bits.append(f"liq {fmt_usd(meta.liquidity_usd)}")
        if t.is_muted:
            bits.append(f"🔇 {int((t.muted_until - time.time()) / 60)}m")
        if not t.enabled:
            bits.append("(disabled)")
        lines.append("  ·  ".join(bits))
        lines.append(f"<code>{t.mint}</code>")
        lines.append("")

    realtime = sum(1 for t in tokens if t.tier is Tier.REALTIME)
    lines.append(f"<i>{realtime} realtime ⚡ · {len(tokens) - realtime} polled 🐢 · ⏳ waiting to bond</i>")
    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    d = _deps()
    tokens = await d.db.list_tokens()
    uptime = time.time() - d.started_at
    alerts_24h = await d.db.alert_count_since(time.time() - 86400)
    realtime = sum(1 for t in tokens if t.tier is Tier.REALTIME)

    lines = [
        "<b>Status</b>",
        "",
        f"<b>Uptime</b>   {uptime / 3600:.1f}h",
        f"<b>Tokens</b>   {len(tokens)}  ({realtime} ⚡ / {len(tokens) - realtime} 🐢)",
        f"<b>Alerts 24h</b> {alerts_24h}",
        "",
    ]

    if d.feed is not None:
        s = d.feed.stats()
        state = "🟢 connected" if s["connected"] else "🔴 down"
        on_public = s["endpoint"] == "primary"
        lines += [
            f"<b>Realtime feed</b>  {state} ({s['endpoint']})",
            f"  {s['tokens']} tokens · {s['accounts']} accounts · {s['reconnects']} reconnects",
            f"  {s['notifications']:,} updates → {s['ticks']:,} ticks",
            f"  {s['megabytes']:.2f} MB streamed",
        ]
        # This is a rate projection, not consumption. Shown as a hypothetical
        # on the public endpoint, because a bare "credits/month" number next to
        # the word "free" reads like a bill and is confusing every time.
        if s["megabytes"] and s["uptime_s"] > 60:
            per_month = s["est_credits"] / s["uptime_s"] * 2_592_000
            share = per_month / 1_000_000 * 100
            if on_public:
                lines.append("  cost: <b>$0 — streaming on the free public RPC</b>")
                lines.append(
                    f"  <i>if it ever failed over to Helius, this traffic rate "
                    f"would be ~{per_month:,.0f} credits/month ({share:.0f}% of the "
                    f"free tier). Nothing is being charged.</i>"
                )
            else:
                lines.append("  cost: ⚠️ <b>BILLED — running on Helius now</b>")
                lines.append(f"  at this rate ≈ {per_month:,.0f} credits/month ({share:.0f}% of 1M)")
        lines.append("")

    lines += [
        f"<b>DexScreener</b>  {d.dex.request_count} reqs · {d.dex.error_count} errors"
        + (f" · {d.dex.empty_retries} empty-retries" if d.dex.empty_retries else ""),
        f"<b>Channel</b>  {d.sender.sent_count} sent · {d.sender.error_count} failed",
    ]
    if d.rpc is not None:
        lines.append(
            f"<b>RPC http</b>  {d.rpc.request_count} reqs · {d.rpc.error_count} errors"
            "  <i>(the only thing that spends Helius credits)</i>"
        )
    if d.sanity is not None:
        bad = d.sanity.stats()["distrusted"]
        if bad:
            lines.append(
                "⚠️ <b>Price mismatch</b> — alerts suppressed for "
                + ", ".join(f"<code>{m[:8]}</code> ({r:.0f}x)" for m, r in bad.items())
            )
    if d.detector is not None:
        st = d.detector.stats()
        suppressed = st["suppressed"]
        lines.append(
            f"<b>Detector</b>  {st['tokens']} tracked · {st['buffered_ticks']:,} ticks buffered"
        )
        if suppressed:
            top = sorted(suppressed.items(), key=lambda kv: -kv[1])[:3]
            lines.append("  held back: " + " · ".join(f"{k} {v:,}" for k, v in top))
    if d.dex.last_error:
        lines.append(f"\n<i>last dex error: {d.dex.last_error}</i>")

    await message.answer("\n".join(lines), parse_mode="HTML")


HELP_LABELS = {
    "thresholds": "📉 Thresholds",
    "alerts": "🔔 Alerts section",
    "filters": "🚫 Filters section",
    "tuning": "🔧 Something's annoying me",
}


def _help_thresholds(s: dict) -> str:
    windows = ("10s", "1m", "3m", "5m")
    drops = " / ".join(f"{s.get(f'threshold_migrated_{w}'):g}%" for w in windows)
    return (
        "<b>📉 Thresholds</b>\n\n"
        "How big a fall has to be before you get a message.\n\n"
        f"<b>drop</b> — {drops}\n"
        "One number per time window: 10s / 1m / 3m / 5m.\n\n"
        "<b>Measured from the peak</b>, not the start. A coin that pumps then "
        "dumps still alerts.\n\n"
        "<b>Coins that haven't bonded</b>\n"
        "Not alerted on at all. We measured pre-bond coins swinging 49%, 71% "
        "and 93% in 3 minutes with nothing wrong, so no threshold is meaningful "
        "there. Tracking starts the moment a coin bonds.\n\n"
        "Bigger → fewer alerts. Smaller → more."
    )


def _help_alerts(s: dict) -> str:
    windows = ("10s", "1m", "3m", "5m")
    return (
        "<b>🔔 Alerts section</b>\n\n"
        f"<b>windows</b> — {str(s.get('enabled_windows')).replace(',', ' · ')}\n"
        "How far back to compare. 10s catches sudden dumps, 5m catches slow "
        "bleeds. All run at once.\n"
        "🐢 coins skip 10s and 1m — their prices only refresh every 30s, so it "
        "would be guesswork.\n\n"
        f"<b>quote</b> — {s.get('quote_mode')}\n"
        "Measure the fall in SOL or dollars. Keep it on SOL: if SOL itself "
        "drops 5%, every coin looks 5% down in dollars even if it never moved.\n\n"
        f"<b>cooldown</b> — {s.get('cooldown_per_token_seconds'):g}s\n"
        "After a coin alerts, stay quiet about it this long. Without it one "
        "crash fires 4 messages (10s, 1m, 3m, 5m all at once).\n\n"
        f"<b>re-arm</b> — {s.get('rearm_recovery_pct'):g}%\n"
        "After alerting, the coin must bounce back up this much of the fall "
        "before it can alert again. Stops a dying coin alerting forever.\n\n"
        f"<b>warmup</b> — {s.get('warmup_seconds'):g}s\n"
        "Ignore a coin for this long after the bot starts. It has no price "
        "history yet, so the first price would look like a collapse.\n\n"
        f"<b>confirm</b> — {s.get('confirm_ticks'):g} tick\n"
        "Wait for this many more prices to check the fall is real. 0 = fastest "
        "alerts, more false ones.\n\n"
        f"<b>min ticks</b> — {' · '.join(str(s.get(f'min_ticks_{w}')) for w in windows)}\n"
        "How many prices must exist before that window is trusted. 3 prices "
        "over 3 minutes isn't a trend."
    )


def _help_filters(s: dict) -> str:
    age = int(s.get("min_token_age_minutes") or 0)
    return (
        "<b>🚫 Filters section</b>\n\n"
        "Which coins are worth alerting on at all.\n\n"
        f"<b>liquidity</b> — {fmt_usd(s.get('min_liquidity_usd'))}\n"
        "How much money sits in the pool. In a tiny pool one $50 trade moves "
        "the price 40% — that's not a dip, that's an empty pool.\n\n"
        f"<b>if unknown</b> — {s.get('unknown_liquidity', 'proxy')}\n"
        "Some pools report no liquidity number at all (MEMEDEX did). What to do "
        "then:\n"
        "<b>proxy</b> — judge it on volume instead (default)\n"
        "<b>skip</b> — never alert without a number\n"
        "<b>allow</b> — alert anyway\n"
        "Treating a missing number as $0 would silently mute real coins.\n\n"
        f"<b>volume 24h</b> — {fmt_usd(s.get('min_volume_24h_usd'))}\n"
        "How much got traded in the last day. Skips coins nobody trades.\n\n"
        f"<b>token age</b> — {str(age) + ' min' if age else 'off'}\n"
        "Ignore coins younger than this. The first minutes of a launch are the "
        "messiest data there is. Raise it if the channel is too busy.\n\n"

        f"<b>outlier</b> — {s.get('outlier_median_multiple'):g}x\n"
        "Throw away a single weird price if the next one disagrees with it. "
        "Catches bots and glitches."
    )


def _help_tuning(s: dict) -> str:
    return (
        "<b>🔧 Quick fixes</b>\n\n"
        "Too many alerts\n→ raise thresholds, then cooldown\n\n"
        "One crash, several messages\n→ raise cooldown\n\n"
        "Same coin alerting as it dies\n→ raise re-arm\n\n"
        "Alerts about garbage coins\n→ raise liquidity + volume 24h\n\n"
        "Alerts about 2-minute-old coins\n→ raise token age\n\n"
        "Alerts too slow\n→ confirm to 0\n\n"
        "Alerts on almost no data\n→ raise min ticks\n\n"
        "Everything alerts when the market drops\n→ set quote to SOL\n\n"
        "Missing dips you wanted\n→ lower thresholds"
    )


HELP_PAGES = {
    "thresholds": _help_thresholds,
    "alerts": _help_alerts,
    "filters": _help_filters,
    "tuning": _help_tuning,
}


def help_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"help:{key}")]
        for key, label in HELP_LABELS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Back to settings", callback_data="help:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def help_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="help:menu")]]
    )


def settings_keyboard(filters_on: bool = True) -> InlineKeyboardMarkup:
    toggle = "🚫 Turn filters off" if filters_on else "✅ Turn filters on"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle, callback_data="filters:toggle")],
            [InlineKeyboardButton(text="✏️ Edit settings", callback_data="s:m")],
            [InlineKeyboardButton(text="❓ What do these mean?", callback_data="help:menu")],
        ]
    )


async def render_settings(db: Database) -> str:
    s = await db.all_settings()
    windows = ("10s", "1m", "3m", "5m")

    def num(key: str) -> str:
        """Trim trailing .0 so the columns stay narrow."""
        return f"{s.get(key, 0):g}"

    def row(label: str, value: str) -> str:
        return f"{label:<12}{value}"

    def money(v) -> str:
        """Compact money for settings: $10k, not $10.00k."""
        v = float(v or 0)
        for cutoff, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
            if v >= cutoff:
                return f"${v / cutoff:g}{suffix}"
        return f"${v:g}"

    age = int(s.get("min_token_age_minutes") or 0)

    # Monospace blocks keep the columns aligned; the explanations live behind
    # the ❓ button rather than crowding every line here.
    thresholds = "\n".join(
        [
            f"{'':<6}" + "".join(f"{w:>7}" for w in windows),
            f"{'drop':<6}" + "".join(f"{num(f'threshold_migrated_{w}') + '%':>7}" for w in windows),
        ]
    )
    alerts = "\n".join(
        [
            row("windows", " · ".join(str(s.get("enabled_windows", "")).split(","))),
            row("quote", str(s.get("quote_mode"))),
            row("cooldown", f"{num('cooldown_per_token_seconds')}s"),
            row("re-arm", f"{num('rearm_recovery_pct')}%"),
            row("warmup", f"{num('warmup_seconds')}s"),
            row("confirm", f"{num('confirm_ticks')} tick"),
            row("min ticks", " · ".join(num(f"min_ticks_{w}") for w in windows)),
        ]
    )
    filters_on = filter_switch(s)
    filters = "\n".join(
        [
            row("enabled", "yes" if filters_on else "NO - all tokens pass"),
            row("liquidity", money(s.get("min_liquidity_usd"))),
            row("if unknown", str(s.get("unknown_liquidity", "proxy"))),
            row("volume 24h", money(s.get("min_volume_24h_usd"))),
            row("token age", f"{age} min" if age else "off"),
            row("outlier", f"{num('outlier_median_multiple')}x median"),
        ]
    )

    return (
        "<b>⚙️ Settings</b>\n\n"
        "<b>Thresholds</b>  <i>drop from peak</i>\n"
        f"<pre>{thresholds}</pre>\n\n"
        "<b>Alerts</b>\n"
        f"<pre>{alerts}</pre>\n\n"
        "<b>Filters</b>\n"
        f"<pre>{filters}</pre>"
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    d = _deps()
    text = await render_settings(d.db)
    on = filter_switch(await d.db.all_settings())
    await message.answer(text, parse_mode="HTML", reply_markup=settings_keyboard(on))


@router.message(Command("filters"))
async def cmd_filters(message: Message) -> None:
    """Turn the token filters on or off without touching individual values."""
    d = _deps()
    parts = (message.text or "").split(maxsplit=1)
    settings = await d.db.all_settings()
    current = filter_switch(settings)

    if len(parts) < 2:
        state = "on" if current else "off"
        await message.answer(
            f"Filters are <b>{state}</b>.\n\n"
            "<code>/filters off</code> — alert on every watched token\n"
            "<code>/filters on</code> — apply liquidity, volume and age limits",
            parse_mode="HTML",
        )
        return

    arg = parts[1].strip().lower()
    if arg not in ("on", "off"):
        await message.answer("Usage: <code>/filters on</code> or <code>/filters off</code>", parse_mode="HTML")
        return

    await d.db.set("filters_enabled", arg == "on")
    if arg == "off":
        await message.answer(
            "Filters <b>off</b>. Every watched token can now alert, including "
            "thin pools that print fake wicks.",
            parse_mode="HTML",
        )
    else:
        await message.answer("Filters <b>on</b>.", parse_mode="HTML")


@router.callback_query(F.data == "filters:toggle")
async def cb_filters_toggle(query: CallbackQuery) -> None:
    d = _deps()
    settings = await d.db.all_settings()
    new_state = not filter_switch(settings)
    await d.db.set("filters_enabled", new_state)

    text = await render_settings(d.db)
    try:
        await query.message.edit_text(
            text, parse_mode="HTML", reply_markup=settings_keyboard(new_state)
        )
    except Exception:
        pass
    await query.answer("Filters on" if new_state else "Filters off — every token can alert")


@router.callback_query(F.data.startswith("help:"))
async def cb_help(query: CallbackQuery) -> None:
    topic = query.data.split(":", 1)[1]

    if topic == "settings":
        d = _deps()
        text = await render_settings(d.db)
        markup = settings_keyboard(filter_switch(await d.db.all_settings()))
    elif topic == "menu":
        text = (
            "<b>❓ What does each setting do?</b>\n\n"
            "Each page explains every line in that section of /settings, "
            "with what it's currently set to.\n\n"
            "Pick a section:"
        )
        markup = help_menu_keyboard()
    elif topic in HELP_PAGES:
        text = HELP_PAGES[topic](await _deps().db.all_settings())
        markup = help_back_keyboard()
    else:
        await query.answer("Unknown topic.")
        return

    try:
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        # Telegram rejects an edit that would not change the message.
        pass
    await query.answer()


@router.message(Command("mute"))
async def cmd_mute(message: Message) -> None:
    d = _deps()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: <code>/mute 30m</code> (s/m/h/d)", parse_mode="HTML")
        return
    seconds = parse_duration(parts[1])
    if not seconds:
        await message.answer("Couldn't read that duration. Try <code>30m</code>, <code>2h</code>, <code>1d</code>.", parse_mode="HTML")
        return
    await d.db.set("muted_until", time.time() + seconds)
    await message.answer(f"Muted for {parts[1].strip()}.")


@router.message(Command("unmute"))
async def cmd_unmute(message: Message) -> None:
    await _deps().db.set("muted_until", 0)
    await message.answer("Unmuted.")


@router.message(Command("test"))
async def cmd_test(message: Message) -> None:
    """Post a sample alert to the channel to prove the wiring end to end."""
    d = _deps()
    tokens = await d.db.list_tokens()
    meta = await d.db.get_meta(tokens[0].mint) if tokens else None
    tier = tokens[0].tier if tokens else Tier.REALTIME
    mint = tokens[0].mint if tokens else "So11111111111111111111111111111111111111112"

    alert = Alert(
        mint=mint, window="1m", drawdown=14.2,
        price_sol=(meta.price_native or 1e-8) if meta else 1e-8,
        baseline_sol=((meta.price_native or 1e-8) * 1.142) if meta else 1.142e-8,
        tier=tier, meta=meta, ts=time.time(),
    )
    msg_id = await d.sender.send_alert(alert)
    await message.answer(
        f"Sample alert posted to the channel (message {msg_id})."
        if msg_id else
        "Send failed — check that the bot is still an admin with Post Messages."
    )


@router.callback_query(F.data.startswith("mute:"))
async def cb_mute(query: CallbackQuery) -> None:
    d = _deps()
    if query.from_user.id != d.owner_id:
        await query.answer("Not allowed.", show_alert=True)
        return
    _, mint, seconds = query.data.split(":")
    await d.db.mute_token(mint, time.time() + int(seconds))
    await query.answer(f"Muted for {int(seconds) // 60}m.")


@router.callback_query(F.data.startswith("remove:"))
async def cb_remove(query: CallbackQuery) -> None:
    d = _deps()
    if query.from_user.id != d.owner_id:
        await query.answer("Not allowed.", show_alert=True)
        return
    mint = query.data.split(":", 1)[1]
    removed = await d.db.remove_token(mint)
    await query.answer("Removed from watchlist." if removed else "Wasn't on the watchlist.")


# --- Phase 4: inline settings editing ---------------------------------------


async def _show_group(query: CallbackQuery, group: str) -> None:
    d = _deps()
    values = await d.db.all_settings()
    await _edit(query, ui.group_text(group), ui.group_keyboard(group, values))


async def _show_editor(query: CallbackQuery, key: str) -> None:
    d = _deps()
    setting = ui.BY_KEY[key]
    values = await d.db.all_settings()
    value = values.get(key)
    await _edit(query, ui.editor_text(setting, value, values), ui.editor_keyboard(setting, value))


async def _edit(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        # Telegram rejects an edit that would leave the message unchanged.
        pass


@router.callback_query(F.data == "s:m")
async def cb_settings_menu(query: CallbackQuery) -> None:
    await _edit(query, ui.menu_text(), ui.groups_keyboard())
    await query.answer()


@router.callback_query(F.data.startswith("s:g:"))
async def cb_settings_group(query: CallbackQuery) -> None:
    await _show_group(query, query.data.split(":", 2)[2])
    await query.answer()


@router.callback_query(F.data.startswith("s:e:"))
async def cb_settings_edit(query: CallbackQuery) -> None:
    key = query.data.split(":", 2)[2]
    if key not in ui.BY_KEY:
        await query.answer("Unknown setting.")
        return
    await _show_editor(query, key)
    await query.answer()


@router.callback_query(F.data.startswith("s:a:"))
async def cb_settings_adjust(query: CallbackQuery) -> None:
    _, _, key, steps = query.data.split(":", 3)
    setting = ui.BY_KEY.get(key)
    if not setting:
        await query.answer("Unknown setting.")
        return
    d = _deps()
    current = await d.db.get(key)
    new_value = ui.apply_step(setting, current, float(steps))
    await d.db.set(key, new_value)
    await _show_editor(query, key)
    await query.answer(f"{setting.label}: {setting.format(new_value)}")


@router.callback_query(F.data.startswith("s:v:"))
async def cb_settings_value(query: CallbackQuery) -> None:
    _, _, key, raw = query.data.split(":", 3)
    setting = ui.BY_KEY.get(key)
    if not setting:
        await query.answer("Unknown setting.")
        return
    d = _deps()
    if setting.kind == ui.BOOL:
        value = raw == "1"
    elif setting.kind == ui.CHOICE:
        value = raw
    else:
        value = setting.clamp(float(raw))
    await d.db.set(key, value)
    await _show_editor(query, key)
    await query.answer(f"{setting.label}: {setting.format(value)}")


@router.message(Command("set"))
async def cmd_set(message: Message) -> None:
    """Exact values, for when tapping +/- is too slow."""
    d = _deps()
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "Usage: <code>/set &lt;setting&gt; &lt;value&gt;</code>\n"
            "e.g. <code>/set threshold_migrated_1m 12</code>\n\n"
            "See <code>/settings</code> → Edit for the list.",
            parse_mode="HTML",
        )
        return

    key, raw = parts[1], " ".join(parts[2:])
    setting = ui.BY_KEY.get(key)
    if not setting:
        close = [k for k in ui.BY_KEY if key in k]
        hint = ("\n\nDid you mean: " + ", ".join(f"<code>{k}</code>" for k in close[:5])) if close else ""
        await message.answer(f"No setting called <code>{key}</code>.{hint}", parse_mode="HTML")
        return

    try:
        value = ui.coerce(setting, raw)
    except ValueError as e:
        await message.answer(f"Couldn't use {raw!r}: {e}")
        return

    before = await d.db.get(key)
    await d.db.set(key, value)
    await message.answer(
        f"<b>{setting.label}</b>: {setting.format(before)} → <b>{setting.format(value)}</b>",
        parse_mode="HTML",
    )


@router.message(Command("token"))
async def cmd_token(message: Message) -> None:
    """Per-token overrides, for a coin that behaves unlike its profile."""
    d = _deps()
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "<code>/token CATE</code> — show its overrides\n"
            "<code>/token CATE threshold_migrated_1m 25</code> — set one\n"
            "<code>/token CATE clear</code> — back to the shared settings",
            parse_mode="HTML",
        )
        return

    token = await d.db.find_token(parts[1])
    if not token:
        await message.answer("Not on the watchlist.")
        return

    name = token.symbol or token.mint[:8]
    overrides = dict(token.overrides or {})

    if len(parts) == 2:
        if not overrides:
            lines = [f"<b>${name}</b> uses the shared settings.", ""]
        else:
            lines = [f"<b>${name}</b> overrides:", ""]
            for key, value in overrides.items():
                setting = ui.BY_KEY.get(key)
                shown = setting.format(value) if setting else value
                lines.append(f"  {setting.label if setting else key}: <b>{shown}</b>")
            lines.append("")
        lines.append("<i>Set one: <code>/token %s threshold_migrated_1m 25</code></i>" % name)
        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    if parts[2].lower() in ("clear", "reset", "none"):
        await d.db.set_overrides(token.mint, None)
        await message.answer(f"<b>${name}</b> back to the shared settings.", parse_mode="HTML")
        return

    if len(parts) < 4:
        await message.answer("Usage: <code>/token CATE &lt;setting&gt; &lt;value&gt;</code>", parse_mode="HTML")
        return

    key, raw = parts[2], " ".join(parts[3:])
    setting = ui.BY_KEY.get(key)
    if not setting:
        await message.answer(f"No setting called <code>{key}</code>.", parse_mode="HTML")
        return
    try:
        value = ui.coerce(setting, raw)
    except ValueError as e:
        await message.answer(f"Couldn't use {raw!r}: {e}")
        return

    overrides[key] = value
    await d.db.set_overrides(token.mint, overrides)
    await message.answer(
        f"<b>${name}</b> · {setting.label} → <b>{setting.format(value)}</b>\n"
        f"<i>Only this token; everything else unchanged.</i>",
        parse_mode="HTML",
    )


@router.message(Command("summary"))
async def cmd_summary(message: Message) -> None:
    """Post the daily summary now, rather than waiting for its scheduled time."""
    d = _deps()
    if d.summary is None:
        await message.answer("Summary isn't wired up.")
        return
    await d.summary.post()
    await message.answer("Summary posted to the channel.")


# --- access control ---------------------------------------------------------


def _describe_admin(entry: dict, owner_id: int) -> str:
    who = entry.get("label") or "unnamed"
    return f"  <code>{entry['user_id']}</code> — {who}"


@router.message(Command("admins"))
async def cmd_admins(message: Message) -> None:
    """Anyone with access can see who else has it - no secret admins."""
    d = _deps()
    admins = await d.db.list_admins()
    lines = [
        "<b>Who can control this bot</b>",
        "",
        f"  <code>{d.owner_id}</code> — owner <i>(cannot be removed)</i>",
    ]
    lines += [_describe_admin(a, d.owner_id) for a in admins]
    if not admins:
        lines.append("  <i>no other admins</i>")
    lines += [
        "",
        "<i>Owner only: <code>/addadmin &lt;user id&gt; [name]</code> · "
        "<code>/removeadmin &lt;user id&gt;</code></i>",
        "<i>Admins get full control except managing other admins.</i>",
    ]
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("addadmin"))
async def cmd_addadmin(message: Message, is_owner: bool = False) -> None:
    d = _deps()
    if not is_owner:
        await message.answer("Only the owner can add admins.")
        return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer(
            "Usage: <code>/addadmin &lt;user id&gt; [name]</code>\n\n"
            "They can get their id from @userinfobot. A numeric id is required — "
            "Telegram does not let bots look people up by @username.",
            parse_mode="HTML",
        )
        return

    raw = parts[1].strip().lstrip("@")
    if not raw.lstrip("-").isdigit():
        await message.answer(
            f"<code>{raw}</code> isn't a numeric user id. Ask them to send "
            "<code>/start</code> to @userinfobot and paste the number.",
            parse_mode="HTML",
        )
        return

    user_id = int(raw)
    label = parts[2].strip() if len(parts) > 2 else None

    if user_id == d.owner_id:
        await message.answer("That's you — the owner already has full access.")
        return

    added = await d.db.add_admin(user_id, label, message.from_user.id)
    if not added:
        await message.answer(f"<code>{user_id}</code> is already an admin.", parse_mode="HTML")
        return

    log.info("owner %s added admin %s (%s)", message.from_user.id, user_id, label)
    await message.answer(
        f"Added <code>{user_id}</code>{f' — {label}' if label else ''}.\n\n"
        "<i>They have full control of the watchlist and settings, but cannot "
        "add or remove admins. They need to message the bot themselves for it "
        "to reach them.</i>",
        parse_mode="HTML",
    )


@router.message(Command("removeadmin"))
async def cmd_removeadmin(message: Message, is_owner: bool = False) -> None:
    d = _deps()
    if not is_owner:
        await message.answer("Only the owner can remove admins.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: <code>/removeadmin &lt;user id&gt;</code>", parse_mode="HTML")
        return

    raw = parts[1].strip().lstrip("@")
    if not raw.lstrip("-").isdigit():
        await message.answer("That isn't a numeric user id.")
        return

    user_id = int(raw)
    if user_id == d.owner_id:
        await message.answer("The owner can't be removed.")
        return

    removed = await d.db.remove_admin(user_id)
    if removed:
        log.info("owner %s removed admin %s", message.from_user.id, user_id)
        await message.answer(
            f"Removed <code>{user_id}</code>. Access ends immediately.", parse_mode="HTML"
        )
    else:
        await message.answer(f"<code>{user_id}</code> wasn't an admin.", parse_mode="HTML")


# --- pasting an address is the same as /add ---------------------------------
# Registered last so every command matches first. Anything that doesn't look
# like an address is ignored silently: the bot is a tool, not a chat partner,
# and replying to stray text would be noise.


@router.message(F.text & ~F.text.startswith("/"))
async def catch_pasted_address(message: Message) -> None:
    text = (message.text or "").strip()
    if not text or not DexScreener.parse_input(text.split()[0] if text.split() else text):
        return
    await add_coin(message, text.split()[0], quiet=True)
