"""Token eligibility: decides whether a token may alert at all.

Kept separate from the detector so the rules are testable without a feed, and so
the reason a token was skipped can be reported rather than guessed at.

The awkward case is missing liquidity. DexScreener returns `liquidity: null` for
some venues (Meteora DBC among them) - MEMEDEX reports no liquidity at all while
showing $141k of 24h volume and a $185k market cap. Treating null as $0 would
silently mute a real coin; ignoring the filter entirely would let genuine junk
through. Hence an explicit policy setting rather than an accident of arithmetic.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import TokenMeta

# Values for the `unknown_liquidity` setting.
PROXY = "proxy"  # judge on volume instead (default)
SKIP = "skip"  # never alert without a liquidity figure
ALLOW = "allow"  # alert regardless
POLICIES = (PROXY, SKIP, ALLOW)


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reason: str = ""
    #: True when the token passed without a liquidity figure, so alerts can say so.
    liquidity_unknown: bool = False

    def __bool__(self) -> bool:
        return self.passed


def _flag(settings: dict, key: str, default: bool = True) -> bool:
    value = settings.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def filters_enabled(settings: dict) -> bool:
    """Master switch. Off means every watched token may alert."""
    return _flag(settings, "filters_enabled")


def check_token(meta: TokenMeta, settings: dict) -> FilterResult:
    """Decide whether `meta` is eligible to produce alerts."""
    if not filters_enabled(settings):
        return FilterResult(True, "filters off", liquidity_unknown=meta.liquidity_usd is None)

    # Each filter has its own switch as well as a value, so one can be turned
    # off without losing the number you had tuned.
    liq_on = _flag(settings, "filter_liquidity_enabled")
    vol_on = _flag(settings, "filter_volume_enabled")
    age_on = _flag(settings, "filter_age_enabled")

    min_liq = float(settings.get("min_liquidity_usd") or 0) if liq_on else 0.0
    min_vol = float(settings.get("min_volume_24h_usd") or 0) if vol_on else 0.0
    min_age = float(settings.get("min_token_age_minutes") or 0) if age_on else 0.0
    policy = str(settings.get("unknown_liquidity") or PROXY).lower()

    if min_age:
        age = meta.age_minutes
        if age is None:
            return FilterResult(False, "age unknown")
        if age < min_age:
            return FilterResult(False, f"too new ({age:.0f}m < {min_age:.0f}m)")

    # Volume is judged on its own terms. It is a separate dimension from
    # liquidity, so the unknown-liquidity policy does not exempt a token from it:
    # `allow` means "ignore the missing liquidity figure", not "ignore all
    # filters". A token with a known, too-low volume fails here either way.
    volume = meta.volume_h24
    if min_vol and volume is not None and volume < min_vol:
        return FilterResult(False, f"volume ${volume:,.0f} < ${min_vol:,.0f}")

    liquidity = meta.liquidity_usd
    if liquidity is not None:
        if min_liq and liquidity < min_liq:
            return FilterResult(False, f"liquidity ${liquidity:,.0f} < ${min_liq:,.0f}")
        return FilterResult(True, "ok")

    # With the liquidity filter off there is nothing to decide - a missing
    # figure cannot fail a check that isn't running.
    if not liq_on:
        return FilterResult(True, "liquidity filter off", liquidity_unknown=True)

    # No liquidity figure at all. Never treat that as $0 - it would silently
    # mute real coins (MEMEDEX: null liquidity, $141k of 24h volume).
    if policy == SKIP:
        return FilterResult(False, "liquidity unknown")
    if policy == ALLOW:
        return FilterResult(True, "liquidity unknown, allowed", liquidity_unknown=True)

    # PROXY: lean on volume as the liveness signal instead. A token nobody
    # trades is what the liquidity floor was guarding against anyway. Volume
    # was already checked above, so reaching here means it passed or is unknown.
    if volume is None:
        return FilterResult(False, "liquidity and volume both unknown")
    return FilterResult(True, "liquidity unknown, volume ok", liquidity_unknown=True)
