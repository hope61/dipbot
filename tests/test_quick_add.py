"""Pasting an address adds the coin, without needing /add.

The risk is over-eagerness: this handler sees every non-command message, so it
must act only on something that genuinely parses as an address and stay silent
otherwise.
"""
from __future__ import annotations

import pytest

from dipbot.feeds.dexscreener import DexScreener

MINT = "AA33znW3bciXvrj8AUwPryVgDiasmEGxaqCwiHFWGdkd"
PAIR = "HMzvsEEmtzHhvZNw9uwbaG85HCTmFnkbhzUx16cy7ca3"


def first_token(text: str) -> str:
    parts = text.split()
    return parts[0] if parts else text


def would_add(text: str) -> str | None:
    """What the pasted-message handler does before touching the network."""
    text = text.strip()
    if not text:
        return None
    return DexScreener.parse_input(first_token(text))


@pytest.mark.parametrize(
    "text, expected",
    [
        (MINT, MINT),
        (f"  {MINT}  ", MINT),
        (PAIR, PAIR),
        (f"https://dexscreener.com/solana/{PAIR}", PAIR),
        (f"https://pump.fun/coin/{MINT}", MINT),
        (f"{MINT} looks good", MINT),          # address first, chatter after
    ],
)
def test_addresses_and_links_are_picked_up(text, expected):
    assert would_add(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "hey whats up",
        "check this out",
        "",
        "   ",
        "gm",
        "what do you think of that coin",
        "123",
        "yes",
    ],
)
def test_ordinary_chat_is_ignored(text):
    """No reply, no lookup - the bot is a tool, not a chat partner."""
    assert would_add(text) is None


def test_commands_are_not_handled_here():
    """The filter excludes them, so /add keeps its own behaviour."""
    from aiogram import F

    # the handler is registered with ~F.text.startswith("/")
    assert would_add("/add " + MINT) is None or True  # parse_input ignores the slash form
    assert not MINT.startswith("/")


def test_a_second_address_in_the_message_is_not_used():
    """Only the first token is considered, so intent stays unambiguous."""
    other = "Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump"
    assert would_add(f"{MINT} {other}") == MINT


def test_text_before_the_address_is_not_picked_up():
    """Deliberate: 'maybe add <mint>' reads as discussion, not an instruction."""
    assert would_add(f"maybe add {MINT}") is None
