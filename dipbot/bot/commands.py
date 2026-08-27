"""The command menu Telegram shows in the ✎ button and `/` autocomplete.

One list, registered with Telegram at startup, so people pick commands instead
of remembering them.

Two scopes: everyone with access sees the everyday commands, and the owner
additionally sees admin management. Scoping is presentation only - the access
middleware is what actually enforces permissions.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)

log = logging.getLogger(__name__)

#: Shown to anyone who opens a private chat with the bot. Telegram caps
#: descriptions at 256 characters and shows them in the order given.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("add", "Track a coin — or just paste the address"),
    ("list", "Show everything being watched"),
    ("remove", "Stop watching a coin"),
    ("clear", "Stop watching every coin — asks first"),
    ("status", "Feed health and counters"),
    ("settings", "View and edit thresholds and filters"),
    ("set", "Change one setting, e.g. /set warmup_seconds 30"),
    ("token", "Settings for a single coin"),
    ("filters", "Apply or ignore the coin filters"),
    ("mute", "Silence alerts for a while, e.g. /mute 30m"),
    ("unmute", "Resume alerts"),
    ("summary", "Post the daily summary now"),
    ("test", "Post a sample alert to the channel"),
    ("admins", "Who can control the bot"),
    ("guide", "How everything works"),
    ("help", "Short command list"),
)

#: Only the owner can manage admins, so only the owner is offered them.
OWNER_COMMANDS: tuple[tuple[str, str], ...] = (
    ("addadmin", "Give someone access — /addadmin <id> [name]"),
    ("removeadmin", "Revoke access — /removeadmin <id>"),
)


def _to_bot_commands(pairs) -> list[BotCommand]:
    return [BotCommand(command=name, description=text) for name, text in pairs]


async def register(bot: Bot, owner_id: int) -> None:
    """Publish the menu. Failure here is cosmetic, so it must not stop startup."""
    try:
        await bot.set_my_commands(
            _to_bot_commands(COMMANDS),
            scope=BotCommandScopeAllPrivateChats(),
        )
        await bot.set_my_commands(
            _to_bot_commands(COMMANDS + OWNER_COMMANDS),
            scope=BotCommandScopeChat(chat_id=owner_id),
        )
        log.info(
            "command menu registered (%d commands, %d owner-only)",
            len(COMMANDS), len(OWNER_COMMANDS),
        )
    except Exception:
        log.warning("could not register the command menu", exc_info=True)
