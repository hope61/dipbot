"""Inline settings editor: registry, value parsing, keyboards, persistence."""
from __future__ import annotations

import pytest

from dipbot.bot import settings_ui as ui
from dipbot.config import WINDOWS


# --- registry ---------------------------------------------------------------


def test_every_seeded_setting_is_editable():
    """A setting the bot uses but nobody can change is a trap."""
    from dipbot import config

    seeded = set(config.load().seed) | {"filters_enabled"}
    # enabled_windows has no numeric editor; min_trade_sol is not enforced yet
    not_editable = seeded - set(ui.BY_KEY) - {"enabled_windows", "min_trade_sol"}
    assert not_editable == set(), f"no editor for: {sorted(not_editable)}"


def test_registry_keys_are_unique():
    keys = [s.key for s in ui.REGISTRY]
    assert len(keys) == len(set(keys))


def test_all_settings_belong_to_a_known_group():
    assert all(s.group in ui.GROUPS for s in ui.REGISTRY)


def test_one_threshold_per_window():
    """Only bonded coins alert, so there is a single set of thresholds."""
    for window in WINDOWS:
        assert f"threshold_migrated_{window}" in ui.BY_KEY
    assert not [key for key in ui.BY_KEY if "bonding" in key]


def test_callback_data_fits_telegram_limit():
    """Telegram caps callback_data at 64 bytes; long keys plus prefixes creep up."""
    for setting in ui.REGISTRY:
        for template in (f"s:e:{setting.key}", f"s:a:{setting.key}:-5", f"s:v:{setting.key}:1000000"):
            assert len(template.encode()) <= 64, template


# --- formatting -------------------------------------------------------------


@pytest.mark.parametrize(
    "key, value, expected",
    [
        ("threshold_migrated_1m", 15, "15%"),
        ("cooldown_per_token_seconds", 600, "600s"),
        ("min_liquidity_usd", 10_000, "$10k"),
        ("min_liquidity_usd", 2_500_000, "$2.5M"),
        ("min_token_age_minutes", 30, "30 min"),
        ("outlier_median_multiple", 3, "3.0x"),
        ("filters_enabled", True, "on"),
        ("filters_enabled", False, "off"),
        ("quote_mode", "SOL", "SOL"),
    ],
)
def test_format(key, value, expected):
    assert ui.BY_KEY[key].format(value) == expected


# --- stepping ---------------------------------------------------------------


def test_step_up_and_down():
    setting = ui.BY_KEY["threshold_migrated_1m"]
    assert ui.apply_step(setting, 15, 1) == 16
    assert ui.apply_step(setting, 15, -1) == 14
    assert ui.apply_step(setting, 15, 5) == 20


def test_step_respects_bounds():
    setting = ui.BY_KEY["rearm_recovery_pct"]
    assert ui.apply_step(setting, 100, 5) == 100
    assert ui.apply_step(setting, 0, -5) == 0


def test_step_from_unset_value_uses_minimum():
    setting = ui.BY_KEY["confirm_ticks"]
    assert ui.apply_step(setting, None, 1) == 1


def test_step_uses_setting_specific_size():
    assert ui.apply_step(ui.BY_KEY["min_liquidity_usd"], 10_000, 1) == 11_000
    assert ui.apply_step(ui.BY_KEY["warmup_seconds"], 60, 1) == 75


# --- parsing typed values ---------------------------------------------------


@pytest.mark.parametrize(
    "key, raw, expected",
    [
        ("threshold_migrated_1m", "12", 12.0),
        ("threshold_migrated_1m", "12%", 12.0),
        ("min_liquidity_usd", "5000", 5000.0),
        ("min_liquidity_usd", "$5,000", 5000.0),
        ("min_liquidity_usd", "5k", 5000.0),
        ("min_liquidity_usd", "2.5M", 2_500_000.0),
        ("filters_enabled", "off", False),
        ("filters_enabled", "ON", True),
        ("quote_mode", "usd", "USD"),
        ("unknown_liquidity", "SKIP", "skip"),
    ],
)
def test_coerce(key, raw, expected):
    assert ui.coerce(ui.BY_KEY[key], raw) == expected


@pytest.mark.parametrize(
    "key, raw",
    [
        ("threshold_migrated_1m", "banana"),
        ("threshold_migrated_1m", "200"),      # above max
        ("threshold_migrated_1m", "0"),        # below min
        ("filters_enabled", "maybe"),
        ("quote_mode", "eur"),
        ("unknown_liquidity", "whatever"),
    ],
)
def test_coerce_rejects_bad_input(key, raw):
    with pytest.raises(ValueError):
        ui.coerce(ui.BY_KEY[key], raw)


def test_coerce_error_explains_choices():
    with pytest.raises(ValueError, match="proxy"):
        ui.coerce(ui.BY_KEY["unknown_liquidity"], "nonsense")


# --- keyboards --------------------------------------------------------------


def test_group_menu_lists_every_group():
    labels = [b[0].text for b in ui.groups_keyboard().inline_keyboard]
    for label in ui.GROUPS.values():
        assert label in labels


def test_group_keyboard_shows_current_values():
    values = {"threshold_migrated_1m": 15, "threshold_migrated_10s": 8}
    texts = [b[0].text for b in ui.group_keyboard("thresholds", values).inline_keyboard]
    assert any("drop 1m: 15%" in t for t in texts)


def test_group_keyboard_marks_settings_whose_filter_is_off():
    values = {"min_liquidity_usd": 10_000, "filter_liquidity_enabled": False}
    texts = [b[0].text for b in ui.group_keyboard("filters", values).inline_keyboard]
    assert any(t.startswith("· min liquidity") for t in texts)


def test_bool_editor_marks_current_state():
    texts = [b.text for row in ui.editor_keyboard(ui.BY_KEY["filters_enabled"], True).inline_keyboard for b in row]
    assert "✅ on" in texts
    assert "off" in texts


def test_choice_editor_marks_current_choice():
    texts = [b.text for row in ui.editor_keyboard(ui.BY_KEY["quote_mode"], "SOL").inline_keyboard for b in row]
    assert "✅ SOL" in texts
    assert "USD" in texts


def test_number_editor_has_adjust_and_presets():
    kb = ui.editor_keyboard(ui.BY_KEY["threshold_migrated_1m"], 15)
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "−" in texts and "+" in texts
    assert any("%" in t for t in texts)


def test_editor_warns_when_parent_filter_is_off():
    text = ui.editor_text(
        ui.BY_KEY["min_liquidity_usd"], 10_000,
        {"filter_liquidity_enabled": False, "filters_enabled": True},
    )
    assert "is off" in text


def test_editor_warns_when_all_filters_off():
    text = ui.editor_text(ui.BY_KEY["min_liquidity_usd"], 10_000, {"filters_enabled": False})
    assert "All filters are off" in text


# --- persistence ------------------------------------------------------------


@pytest.mark.asyncio
async def test_edited_value_persists(db):
    setting = ui.BY_KEY["threshold_migrated_1m"]
    new_value = ui.apply_step(setting, await db.get(setting.key), 5)
    await db.set(setting.key, new_value)
    assert await db.get(setting.key) == 20


@pytest.mark.asyncio
async def test_edits_survive_reseeding(db):
    """Re-running with .env defaults must not clobber a tuned value."""
    await db.set("threshold_migrated_1m", 33)
    await db.seed_settings({"threshold_migrated_1m": 15})
    assert await db.get("threshold_migrated_1m") == 33

