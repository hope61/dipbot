"""Inline settings editor.

Every tunable value is declared once in REGISTRY and the UI is generated from
that, so adding a setting means adding a row here rather than writing another
keyboard. Values live in the database, so edits take effect without a restart
and survive one.

Callback data is capped at 64 bytes by Telegram, hence the terse `s:` prefixes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

NUMBER = "number"
BOOL = "bool"
CHOICE = "choice"

WINDOWS = ("10s", "1m", "3m", "5m")


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    group: str
    kind: str = NUMBER
    unit: str = ""
    step: float = 1.0
    minimum: float = 0.0
    maximum: float = 1e9
    decimals: int = 0
    choices: tuple[str, ...] = ()
    presets: tuple[float, ...] = ()
    note: str = ""
    #: Filters whose own on/off switch gates this value.
    gated_by: str = ""

    def clamp(self, value: float) -> float:
        return max(self.minimum, min(self.maximum, value))

    def format(self, value) -> str:
        if self.kind == BOOL:
            return "on" if _truthy(value) else "off"
        if self.kind == CHOICE:
            return str(value)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        if self.unit == "$":
            return _money(number)
        text = f"{number:.{self.decimals}f}"
        return f"{text}{self.unit}"


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def _money(value: float) -> str:
    for cutoff, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if value >= cutoff:
            return f"${value / cutoff:g}{suffix}"
    return f"${value:g}"


def _thresholds(profile: str, label: str) -> list[Setting]:
    return [
        Setting(
            key=f"threshold_{profile}_{w}",
            label=f"{label} {w}",
            group="thresholds",
            unit="%",
            step=1,
            minimum=0.5,
            maximum=95,
            presets=(5, 10, 15, 20, 30, 40),
        )
        for w in WINDOWS
    ]


def _min_ticks() -> list[Setting]:
    return [
        Setting(
            key=f"min_ticks_{w}",
            label=f"min ticks {w}",
            group="gating",
            step=1,
            minimum=1,
            maximum=100,
            presets=(3, 5, 8, 15, 20),
        )
        for w in WINDOWS
    ]


REGISTRY: tuple[Setting, ...] = (
    *_thresholds("migrated", "drop"),
    Setting("cooldown_per_token_seconds", "cooldown", "gating", unit="s", step=60,
            maximum=86_400, presets=(0, 300, 600, 1800, 3600)),
    Setting("rearm_recovery_pct", "re-arm", "gating", unit="%", step=10, maximum=100,
            presets=(0, 25, 50, 75, 100)),
    Setting("warmup_seconds", "warmup", "gating", unit="s", step=15, maximum=3600,
            presets=(0, 30, 60, 120, 300)),
    Setting("confirm_ticks", "confirm", "gating", unit=" tick", step=1, maximum=10,
            presets=(0, 1, 2, 3)),
    *_min_ticks(),
    Setting("filters_enabled", "all filters", "filters", kind=BOOL,
            note="master switch"),
    Setting("filter_liquidity_enabled", "liquidity filter", "filters", kind=BOOL),
    Setting("min_liquidity_usd", "min liquidity", "filters", unit="$", step=1000,
            maximum=1e8, presets=(0, 1000, 5000, 10_000, 50_000, 250_000),
            gated_by="filter_liquidity_enabled"),
    Setting("filter_volume_enabled", "volume filter", "filters", kind=BOOL),
    Setting("min_volume_24h_usd", "min volume 24h", "filters", unit="$", step=5000,
            maximum=1e9, presets=(0, 10_000, 50_000, 250_000, 1_000_000),
            gated_by="filter_volume_enabled"),
    Setting("filter_age_enabled", "age filter", "filters", kind=BOOL),
    Setting("min_token_age_minutes", "min age", "filters", unit=" min", step=5,
            maximum=10_080, presets=(0, 5, 30, 60, 1440),
            gated_by="filter_age_enabled"),
    Setting("unknown_liquidity", "if liquidity unknown", "filters", kind=CHOICE,
            choices=("proxy", "skip", "allow")),
    Setting("quote_mode", "measure drop in", "feed", kind=CHOICE, choices=("SOL", "USD")),
    Setting("outlier_median_multiple", "outlier guard", "feed", unit="x", step=0.5,
            maximum=20, decimals=1, presets=(0, 2, 3, 5, 10)),
)

BY_KEY = {s.key: s for s in REGISTRY}

GROUPS = {
    "thresholds": "📉 Thresholds",
    "gating": "🔔 Gating",
    "filters": "🚫 Filters",
    "feed": "⚙️ Feed",
}


def group_settings(group: str) -> list[Setting]:
    return [s for s in REGISTRY if s.group == group]


# --- keyboards --------------------------------------------------------------


def groups_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"s:g:{key}")]
        for key, label in GROUPS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="help:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_keyboard(group: str, values: dict) -> InlineKeyboardMarkup:
    """One row per setting: label on the left, current value as the button."""
    rows = []
    for setting in group_settings(group):
        shown = setting.format(values.get(setting.key))
        disabled = setting.gated_by and not _truthy(values.get(setting.gated_by, True))
        prefix = "· " if disabled else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{prefix}{setting.label}: {shown}",
                callback_data=f"s:e:{setting.key}",
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="s:m")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def editor_keyboard(setting: Setting, value) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if setting.kind == BOOL:
        rows.append([
            InlineKeyboardButton(
                text="✅ on" if _truthy(value) else "on",
                callback_data=f"s:v:{setting.key}:1",
            ),
            InlineKeyboardButton(
                text="✅ off" if not _truthy(value) else "off",
                callback_data=f"s:v:{setting.key}:0",
            ),
        ])
    elif setting.kind == CHOICE:
        rows.append([
            InlineKeyboardButton(
                text=f"✅ {choice}" if str(value) == choice else choice,
                callback_data=f"s:v:{setting.key}:{choice}",
            )
            for choice in setting.choices
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="−−", callback_data=f"s:a:{setting.key}:-5"),
            InlineKeyboardButton(text="−", callback_data=f"s:a:{setting.key}:-1"),
            InlineKeyboardButton(text="+", callback_data=f"s:a:{setting.key}:1"),
            InlineKeyboardButton(text="++", callback_data=f"s:a:{setting.key}:5"),
        ])
        preset_row = [
            InlineKeyboardButton(
                text=f"✅ {setting.format(preset)}" if _same(value, preset) else setting.format(preset),
                callback_data=f"s:v:{setting.key}:{preset:g}",
            )
            for preset in setting.presets
        ]
        for i in range(0, len(preset_row), 3):
            rows.append(preset_row[i : i + 3])

    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"s:g:{setting.group}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _same(value, preset) -> bool:
    try:
        return abs(float(value) - float(preset)) < 1e-9
    except (TypeError, ValueError):
        return False


# --- text -------------------------------------------------------------------


def editor_text(setting: Setting, value, settings: dict) -> str:
    lines = [f"<b>{setting.label}</b>", "", f"Now: <b>{setting.format(value)}</b>"]

    if setting.gated_by and not _truthy(settings.get(setting.gated_by, True)):
        lines.append(f"\n⚠️ <i>The {BY_KEY[setting.gated_by].label} is off, so this is ignored.</i>")
    if not _truthy(settings.get("filters_enabled", True)) and setting.group == "filters":
        if setting.key != "filters_enabled":
            lines.append("\n⚠️ <i>All filters are off, so this is ignored.</i>")
    if setting.note:
        lines.append(f"\n<i>{setting.note}</i>")
    if setting.kind == NUMBER:
        lines.append(f"\n<i>Or set exactly: <code>/set {setting.key} 12</code></i>")
    return "\n".join(lines)


def group_text(group: str) -> str:
    return f"<b>{GROUPS[group]}</b>\n\nTap a value to change it."


def menu_text() -> str:
    return "<b>Edit settings</b>\n\nChanges apply immediately — no restart."


def apply_step(setting: Setting, value, steps: float) -> float:
    try:
        current = float(value)
    except (TypeError, ValueError):
        current = setting.minimum
    return round(setting.clamp(current + setting.step * steps), 4)


def coerce(setting: Setting, raw: str):
    """Parse a value typed by the user for `/set`."""
    text = raw.strip()
    if setting.kind == BOOL:
        if text.lower() in ("on", "true", "yes", "1"):
            return True
        if text.lower() in ("off", "false", "no", "0"):
            return False
        raise ValueError("use on or off")
    if setting.kind == CHOICE:
        if text.lower() not in [c.lower() for c in setting.choices]:
            raise ValueError("choose one of: " + ", ".join(setting.choices))
        return next(c for c in setting.choices if c.lower() == text.lower())
    cleaned = text.replace("$", "").replace(",", "").replace("%", "").strip()
    multiplier = 1.0
    if cleaned and cleaned[-1].lower() in ("k", "m", "b"):
        multiplier = {"k": 1e3, "m": 1e6, "b": 1e9}[cleaned[-1].lower()]
        cleaned = cleaned[:-1]
    try:
        number = float(cleaned) * multiplier
    except ValueError as e:
        raise ValueError("that isn't a number") from e
    if not (setting.minimum <= number <= setting.maximum):
        raise ValueError(f"must be between {setting.minimum:g} and {setting.maximum:g}")
    return number

