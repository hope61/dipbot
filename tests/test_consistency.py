"""Guards against documentation and UI drifting from the code.

Every one of these exists because a real mistake got through: the settings
editor was updated while the /settings panel still rendered a removed feature,
and SETTINGS.md kept describing a setting that no longer existed. Reviewing by
eye missed both. These fail loudly instead.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from dipbot import config
from dipbot.bot import guide, settings_ui as ui

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = list((ROOT / "dipbot").rglob("*.py"))


def source_text() -> str:
    return "".join(p.read_text() for p in SOURCE)


def real_settings() -> set[str]:
    return set(config.load().seed)


def family(key: str) -> str:
    """Collapse per-window keys so docs can describe them as one entry."""
    return re.sub(r"_(10s|1m|3m|5m)$", "", key)


# --- settings exist everywhere they are referenced --------------------------


def test_every_setting_has_an_editor():
    missing = real_settings() - set(ui.BY_KEY) - {"enabled_windows"}
    assert missing == set(), f"no editor for: {sorted(missing)}"


def test_editor_has_no_settings_that_are_never_seeded():
    extra = set(ui.BY_KEY) - real_settings()
    assert extra == set(), f"editor offers unseeded settings: {sorted(extra)}"


def test_no_code_references_a_removed_setting():
    """Catches a UI still reading a key that config no longer produces."""
    referenced = set(re.findall(r'"(threshold_[a-z]+_[0-9a-z]+)"', source_text()))
    referenced |= set(re.findall(r'f"(threshold_\{[a-z_]+\}_\{[a-z]+\})"', source_text()))
    literal = {r for r in referenced if "{" not in r}
    unknown = literal - real_settings()
    assert unknown == set(), f"code reads settings that don't exist: {sorted(unknown)}"


def test_bonding_thresholds_are_fully_gone():
    """They were removed; nothing should still read or render them."""
    for path in SOURCE:
        assert "threshold_bonding" not in path.read_text(), path


# --- documentation matches the code ----------------------------------------


def test_settings_doc_covers_every_setting():
    doc = set(re.findall(r"`([a-z_0-9]+)`", (ROOT / "SETTINGS.md").read_text()))
    documented = {family(d) for d in doc}
    missing = {family(k) for k in real_settings()} - documented
    assert missing == set(), f"SETTINGS.md is missing: {sorted(missing)}"


def test_settings_doc_describes_no_removed_setting():
    doc = set(re.findall(r"^### `([a-z_0-9]+)`", (ROOT / "SETTINGS.md").read_text(), re.M))
    real = {family(k) for k in real_settings()}
    stale = {d for d in doc if family(d) not in real}
    assert stale == set(), f"SETTINGS.md documents removed settings: {sorted(stale)}"


def test_env_example_lists_no_removed_setting():
    env_keys = set(re.findall(r"^([A-Z_]+)=", (ROOT / ".env.example").read_text(), re.M))
    read_by_config = set(re.findall(r'getenv\(\s*"([A-Z_]+)"', source_text()))
    read_by_config |= set(re.findall(r'_(?:int|float|bool|req)\(\s*"([A-Z_]+)"', source_text()))
    read_by_config |= set(re.findall(r'f"(?:THRESHOLD_MIGRATED|MIN_TICKS)_\{[a-z]+\}"', source_text()))
    # per-window keys are built dynamically
    dynamic = {k for k in env_keys if re.search(r"_(10S|1M|3M|5M)$", k)}
    stale = env_keys - read_by_config - dynamic
    assert stale == set(), f".env.example lists keys nothing reads: {sorted(stale)}"


# --- the in-bot guide matches the bot ---------------------------------------


def test_guide_mentions_only_real_commands():
    handlers = (ROOT / "dipbot" / "bot" / "handlers.py").read_text()
    real = set(re.findall(r'Command\("([a-z]+)"\)', handlers)) | {"start"}
    mentioned = set(re.findall(r"<code>/([a-z]+)", "".join(b for _, b in guide.PAGES.values())))
    assert mentioned <= real, f"guide mentions missing commands: {sorted(mentioned - real)}"


def test_guide_mentions_only_real_settings():
    bodies = "".join(b for _, b in guide.PAGES.values())
    mentioned = set(re.findall(r"(threshold_[a-z]+_[0-9a-z]+|min_[a-z_0-9]+)", bodies))
    unknown = mentioned - real_settings()
    assert unknown == set(), f"guide names settings that don't exist: {sorted(unknown)}"


def test_help_text_mentions_only_real_commands():
    from dipbot.bot import handlers

    handlers_src = (ROOT / "dipbot" / "bot" / "handlers.py").read_text()
    real = set(re.findall(r'Command\("([a-z]+)"\)', handlers_src)) | {"start"}
    mentioned = set(re.findall(r"^/([a-z]+)", handlers.HELP, re.M))
    assert mentioned <= real, f"/help lists missing commands: {sorted(mentioned - real)}"


# --- claims that must stay true ---------------------------------------------


def test_polled_tier_still_excludes_fast_windows():
    from dipbot.models import Tier

    assert Tier.POLLED.allowed_windows == ("3m", "5m")


@pytest.mark.parametrize("path", ["README.md", "SETTINGS.md"])
def test_docs_have_no_broken_local_links(path):
    text = (ROOT / path).read_text()
    for target in re.findall(r"\]\(([^)h][^)]*)\)", text):
        target = target.split("#")[0]
        if target:
            assert (ROOT / target).exists(), f"{path} links to missing {target}"
