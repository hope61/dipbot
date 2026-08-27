"""The Telegram command menu.

Its whole value is being an accurate list, so these mostly check it against the
real handlers rather than against itself.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from dipbot.bot import commands as menu

ROOT = pathlib.Path(__file__).resolve().parent.parent
HANDLERS = ROOT / "dipbot" / "bot" / "handlers.py"


def registered_commands() -> set[str]:
    """Commands the bot actually handles."""
    return set(re.findall(r'Command\("([a-z]+)"\)', HANDLERS.read_text())) | {"start"}


def menu_names() -> set[str]:
    return {name for name, _ in menu.COMMANDS + menu.OWNER_COMMANDS}


# --- the menu matches the bot ----------------------------------------------


def test_every_menu_entry_is_a_real_command():
    """Offering a command that does nothing is worse than not listing it."""
    unknown = menu_names() - registered_commands()
    assert unknown == set(), f"menu offers missing commands: {sorted(unknown)}"


def test_every_command_appears_in_the_menu():
    # /start is implicit in Telegram's UI and needs no menu entry.
    missing = registered_commands() - menu_names() - {"start"}
    assert missing == set(), f"commands missing from the menu: {sorted(missing)}"


def test_owner_commands_are_not_in_the_public_list():
    public = {name for name, _ in menu.COMMANDS}
    assert "addadmin" not in public
    assert "removeadmin" not in public


def test_owner_scope_includes_everything():
    owner_visible = {name for name, _ in menu.COMMANDS + menu.OWNER_COMMANDS}
    assert owner_visible >= {name for name, _ in menu.COMMANDS}
    assert "addadmin" in owner_visible


# --- Telegram's constraints -------------------------------------------------


def test_names_are_valid():
    """Telegram: lowercase letters, digits and underscores, 1-32 chars."""
    for name, _ in menu.COMMANDS + menu.OWNER_COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", name), name


def test_descriptions_fit():
    for name, text in menu.COMMANDS + menu.OWNER_COMMANDS:
        assert 1 <= len(text) <= 256, name


def test_no_duplicate_entries():
    names = [name for name, _ in menu.COMMANDS + menu.OWNER_COMMANDS]
    assert len(names) == len(set(names))


def test_descriptions_are_not_empty_or_placeholder():
    for name, text in menu.COMMANDS + menu.OWNER_COMMANDS:
        assert text.strip()
        assert "TODO" not in text


# --- registration -----------------------------------------------------------


class FakeBot:
    def __init__(self, fail: bool = False):
        self.calls: list[tuple] = []
        self.fail = fail

    async def set_my_commands(self, commands, scope=None):
        if self.fail:
            raise RuntimeError("telegram unavailable")
        self.calls.append((commands, scope))


@pytest.mark.asyncio
async def test_register_publishes_both_scopes():
    bot = FakeBot()
    await menu.register(bot, owner_id=42)
    assert len(bot.calls) == 2

    public, owner = bot.calls
    assert len(public[0]) == len(menu.COMMANDS)
    assert len(owner[0]) == len(menu.COMMANDS) + len(menu.OWNER_COMMANDS)


@pytest.mark.asyncio
async def test_register_targets_the_owner_chat():
    bot = FakeBot()
    await menu.register(bot, owner_id=1919116430)
    _, owner_scope = bot.calls[1][1], bot.calls[1][1]
    assert getattr(owner_scope, "chat_id", None) == 1919116430


@pytest.mark.asyncio
async def test_register_failure_does_not_stop_startup():
    """A cosmetic menu must never prevent the bot from running."""
    await menu.register(FakeBot(fail=True), owner_id=42)
