"""In-bot user guide.

Written for someone who did not build this. Short pages, concrete examples, and
the honest limits stated rather than hidden - the most common support question
is "why am I not getting alerts", and it usually has a real answer.
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

MENU = "menu"

PAGES: dict[str, tuple[str, str]] = {
    "what": (
        "What it does",
        "<b>What this bot does</b>\n\n"
        "You give it a list of Solana coins. It watches their price and posts a "
        "message to the channel when one drops sharply.\n\n"
        "It watches four time spans at once:\n"
        "<b>10s</b> — sudden dump\n"
        "<b>1m</b> · <b>3m</b> — fast fall\n"
        "<b>5m</b> — slow bleed\n\n"
        "A drop counts from the coin's <b>highest point</b> in that span, not "
        "where it started. So a coin that pumps then crashes still alerts — "
        "that crash is the thing worth knowing about.\n\n"
        "It does not buy, sell, or hold anything. It only watches and tells you.",
    ),
    "adding": (
        "Adding coins",
        "<b>Adding coins to watch</b>\n\n"
        "<b>Just paste the address.</b> No command needed — the bot picks it "
        "up and starts watching.\n\n"
        "<code>/add &lt;address&gt;</code> still works too.\n\n"
        "Either accepts any of these:\n"
        "· the coin's mint address\n"
        "· the pair address\n"
        "· a DexScreener link\n"
        "· a pump.fun link\n\n"
        "<code>/list</code> shows everything being watched.\n"
        "<code>/remove CATE</code> stops watching one.\n"
        "<code>/clear</code> stops watching everything — it asks first.\n\n"
        "<b>If /add says it can't find the coin</b>, it usually just launched — "
        "DexScreener takes a few minutes to index brand-new coins. Wait and "
        "try again.\n\n"
        "There's no limit on how many you add, but each busy coin adds traffic. "
        "A dozen is comfortable.",
    ),
    "badges": (
        "⚡ and 🐢 explained",
        "<b>Why some coins are ⚡ and others 🐢</b>\n\n"
        "<b>⚡ realtime</b>\n"
        "Price read straight from the blockchain, updated on every single trade. "
        "Gets all four windows including 10s.\n"
        "Works for bonded pump.fun coins trading on PumpSwap.\n\n"
        "<b>🐢 polled</b>\n"
        "Price comes from DexScreener, which only refreshes every 30 seconds. "
        "Only gets the 3m and 5m windows.\n\n"
        "A 10-second alert on 30-second-old data would be guesswork, so the bot "
        "refuses to offer it rather than pretending.\n\n"
        "Coins on Raydium, Orca and Meteora are 🐢. A pump.fun coin that "
        "graduates stays ⚡ — the bot notices and switches automatically.",
    ),
    "alert": (
        "Reading an alert",
        "<b>Reading an alert</b>\n\n"
        "<code>Gta Buckz -60% in 1m</code>\n"
        "Coin, how far it fell, over what span.\n\n"
        "<code>MC: 122k -> 60k</code>\n"
        "Market cap at the window's high, and now. Both read from the "
        "blockchain at the moment of the alert, so they match the fall "
        "exactly.\n\n"
        "<code>ATH: 300k</code>\n"
        "The highest market cap <b>since this bot started watching it</b>. Not "
        "a true all-time high — nothing gives us the coin's full history, so a "
        "coin added after its top will show a lower peak than reality.\n\n"
        "<code>Age · Vol 5m · Vol 24h</code>\n"
        "From DexScreener, refreshed every 20 seconds.\n\n"
        "<b>Buttons</b>\n"
        "Chart · pump.fun · Trojan to trade it\n"
        "🔇 Mute 1h · 🗑 Remove\n\n"
        "<b>If MC shows one number and no arrow</b>, the coin was added too "
        "recently to price both ends yet. The percentage is still correct.",
    ),
    "quiet": (
        "Why no alerts?",
        "<b>Not getting alerts?</b>\n\n"
        "Usually one of these, in order of likelihood:\n\n"
        "<b>1. Nothing actually dropped.</b>\n"
        "A calm coin moving 2% won't trip an 8% threshold. That's correct.\n\n"
        "<b>2. The coin hasn't bonded yet.</b>\n"
        "Coins still on their launch curve are never alerted on. "
        "<code>/list</code> shows them as waiting. They start being tracked "
        "automatically once they bond.\n\n"
        "<b>3. A filter is blocking it.</b>\n"
        "<code>/settings</code> → Filters. Thin coins get blocked by the "
        "liquidity floor. Turn that filter off if you want them.\n\n"
        "<b>4. Thresholds are too high.</b>\n"
        "<code>/settings</code> → Thresholds, or <code>/set "
        "threshold_migrated_1m 10</code>\n\n"
        "<b>5. You're muted.</b>\n"
        "<code>/unmute</code>\n\n"
        "<b>6. The feed is down.</b>\n"
        "<code>/status</code> shows it. The bot also posts to the channel when "
        "the feed drops and when it recovers.\n\n"
        "<code>/status</code> has a <i>held back</i> line showing exactly what "
        "was suppressed and why.",
    ),
    "tuning": (
        "Tuning alerts",
        "<b>Making it louder or quieter</b>\n\n"
        "<code>/settings</code> → ✏️ Edit settings\n"
        "Or set one directly:\n"
        "<code>/set threshold_migrated_1m 12</code>\n\n"
        "<b>Too many alerts</b> → raise thresholds, then cooldown\n"
        "<b>Too few</b> → lower thresholds\n"
        "<b>Same crash repeating</b> → raise cooldown\n"
        "<b>Junk coins alerting</b> → raise min liquidity\n\n"
        "<b>Coins that haven't bonded</b>\n"
        "Coins still on their launch curve are not alerted on at all. They swing "
        "50–90% in minutes with nothing actually wrong, so any threshold would "
        "either spam you or be useless. Tracking starts the moment a coin bonds.\n\n"
        "<b>One coin misbehaving?</b>\n"
        "<code>/token CATE threshold_migrated_1m 25</code> changes it for that "
        "coin only.\n\n"
        "Changes apply within 30 seconds. No restart.",
    ),
    "admins": (
        "Sharing access",
        "<b>Letting other people control the bot</b>\n\n"
        "<code>/admins</code> — see who has access\n"
        "<code>/addadmin 123456789 Bob</code> — owner only\n"
        "<code>/removeadmin 123456789</code> — owner only\n\n"
        "They get their numeric id by messaging @userinfobot. Telegram doesn't "
        "let bots look people up by username, so the number is required.\n\n"
        "<b>Admins can</b> add and remove coins, change every setting, mute.\n"
        "<b>Admins cannot</b> add or remove other admins.\n\n"
        "The owner can never be removed, so nobody can lock you out.\n\n"
        "New admins must message the bot themselves first — Telegram won't let "
        "a bot start a conversation.",
    ),
    "commands": (
        "All commands",
        "<b>Every command</b>\n\n"
        "<b>Coins</b>\n"
        "<code>/add</code> · <code>/remove</code> · <code>/clear</code> · "
        "<code>/list</code>\n\n"
        "<b>Settings</b>\n"
        "<code>/settings</code> — view and edit\n"
        "<code>/set &lt;name&gt; &lt;value&gt;</code> — change one exactly\n"
        "<code>/token &lt;coin&gt;</code> — settings for one coin only\n"
        "<code>/filters on|off</code> — ignore all filters\n\n"
        "<b>Alerts</b>\n"
        "<code>/mute 30m</code> · <code>/unmute</code>\n"
        "<code>/test</code> — post a sample alert\n"
        "<code>/summary</code> — post the daily summary now\n\n"
        "<b>Info</b>\n"
        "<code>/status</code> — feed health and counters\n"
        "<code>/admins</code> — who has access\n"
        "<code>/help</code> — the short list",
    ),
}


def menu_text() -> str:
    return (
        "<b>📖 Guide</b>\n\n"
        "How this bot works, in plain English.\n\n"
        "<i>New here? Start with “What it does”.</i>\n"
        "<i>Expecting alerts and seeing none? Try “Why no alerts?”.</i>"
    )


def menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"guide:{key}")]
        for key, (title, _) in PAGES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def page_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to guide", callback_data="guide:menu")]]
    )


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📖 Open the guide", callback_data="guide:menu")]]
    )
