"""The in-bot guide: structure, formatting, and that it stays truthful.

The guide is documentation shipped inside the product, so the risk is it drifting
out of step with the bot - describing commands that don't exist, or promising
behaviour that changed. Several tests check it against the real code.
"""
from __future__ import annotations

import re

import pytest

from dipbot.bot import guide


def bodies() -> str:
    return "".join(body for _, body in guide.PAGES.values())


def test_guide_has_pages():
    assert len(guide.PAGES) >= 6


def test_every_page_fits_a_telegram_message():
    for key, (_, body) in guide.PAGES.items():
        assert len(body) < 4096, key


def test_every_page_has_balanced_html():
    for key, (_, body) in guide.PAGES.items():
        opened = re.findall(r"<([a-z]+)>", body)
        closed = re.findall(r"</([a-z]+)>", body)
        assert sorted(opened) == sorted(closed), key


def test_no_unescaped_ampersands_or_angle_brackets():
    """Telegram rejects the whole message if HTML parsing fails."""
    for key, (_, body) in guide.PAGES.items():
        assert not re.findall(r"&(?!amp;|lt;|gt;|quot;)", body), key
        assert not re.findall(r"<(?![/a-z])", body), key


def test_page_titles_are_unique():
    titles = [title for title, _ in guide.PAGES.values()]
    assert len(titles) == len(set(titles))


# --- navigation -------------------------------------------------------------


def test_menu_lists_every_page():
    labels = [row[0].text for row in guide.menu_keyboard().inline_keyboard]
    for title, _ in guide.PAGES.values():
        assert title in labels


def test_menu_buttons_point_at_real_pages():
    for row in guide.menu_keyboard().inline_keyboard:
        key = row[0].callback_data.split(":", 1)[1]
        assert key in guide.PAGES


def test_every_page_can_get_back_to_the_menu():
    back = guide.page_keyboard().inline_keyboard[0][0]
    assert back.callback_data == f"guide:{guide.MENU}"


def test_start_button_opens_the_guide():
    button = guide.start_keyboard().inline_keyboard[0][0]
    assert button.callback_data == f"guide:{guide.MENU}"
    assert "guide" in button.text.lower()


def test_callback_data_fits_telegram_limit():
    for row in guide.menu_keyboard().inline_keyboard:
        assert len(row[0].callback_data.encode()) <= 64


# --- stays honest about the bot ---------------------------------------------


def test_guide_only_mentions_commands_that_exist():
    """A guide describing a command that was renamed is worse than no guide."""
    import pathlib

    handlers = pathlib.Path("dipbot/bot/handlers.py").read_text()
    real = set(re.findall(r'Command\("([a-z]+)"\)', handlers)) | {"start"}

    mentioned = set(re.findall(r"<code>/([a-z]+)", bodies()))
    assert mentioned <= real, f"guide mentions missing commands: {sorted(mentioned - real)}"


def test_guide_mentions_settings_keys_that_exist():
    from dipbot.bot import settings_ui

    mentioned = set(re.findall(r"/set(?:tings)? ([a-z_]+) ", bodies()))
    mentioned |= set(re.findall(r"threshold_[a-z]+_[0-9a-z]+", bodies()))
    unknown = {m for m in mentioned if m.startswith(("threshold_", "min_", "cooldown"))}
    assert unknown <= set(settings_ui.BY_KEY), f"unknown settings: {sorted(unknown - set(settings_ui.BY_KEY))}"


def test_guide_explains_the_polled_limitation():
    text = guide.PAGES["badges"][1]
    assert "30 second" in text or "30 seconds" in text
    assert "3m" in text and "5m" in text


def test_troubleshooting_page_covers_the_common_causes():
    text = guide.PAGES["quiet"][1].lower()
    for cause in ("filter", "threshold", "mute", "feed"):
        assert cause in text


def test_admin_page_states_what_admins_cannot_do():
    text = guide.PAGES["admins"][1]
    assert "cannot" in text
    assert "owner can never be removed" in text


# --- handler wiring ---------------------------------------------------------


@pytest.mark.parametrize("key", list(guide.PAGES))
def test_each_page_resolves(key):
    title, body = guide.PAGES[key]
    assert title and body


def test_menu_text_is_present():
    assert "Guide" in guide.menu_text()
