"""Dip detection.

A dip is a fall from the **rolling high inside the window**, not from the
window's opening price. A coin that pumps 40% then dumps 25% is exactly the
alert worth having; comparing against the opening price would miss it entirely.

Every guard here exists because of something measured in Phase 0:

  global cooldown   one PKG dump fired 10s, 3m and 5m at an identical -22.5%
                    from a single tick. Per-window cooldowns do not stop that,
                    so the fastest window to trip wins and silences the rest.
  min ticks         a 3m alert fired on exactly 4 ticks. Three points and a
                    rumour is not a trend, so the floor scales with the window.
  outlier baseline  one bogus high print inflates the rolling high and
                    manufactures a dip that never happened.
  warmup            price history is in memory, so a fresh buffer has no past
                    and the first tick would read as a total collapse.
  bonded only       coins still on their launch curve swung 49/71/93% in three
                    minutes with nothing wrong. Rather than tune around that,
                    they are not alerted on at all - tracking begins once the
                    coin bonds and starts trading on a real pool.

The detector is pure: it takes ticks and settings and returns alerts. No I/O,
so it can be replayed against recorded ticks at thousands of times real speed.
"""
from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass, field

from .alerts import Alert
from .filters import check_token
from .models import PriceTick, Tier, TokenMeta

log = logging.getLogger(__name__)

WINDOW_SECONDS = {"10s": 10, "1m": 60, "3m": 180, "5m": 300}
#: Evaluated fastest first so the earliest warning wins the cascade.
WINDOW_ORDER = ("10s", "1m", "3m", "5m")

#: Threshold keys keep the `migrated` word for backwards compatibility with
#: databases seeded before bonding-curve coins were dropped.
PROFILE = "migrated"


@dataclass
class TokenState:
    """Rolling price history and alert bookkeeping for one token."""

    ticks: deque = field(default_factory=deque)
    first_tick_at: float = 0.0
    last_tick_at: float = 0.0
    last_alert_at: float = 0.0
    last_alert_price: float = 0.0
    last_alert_baseline: float = 0.0
    armed: bool = True
    pending_window: str | None = None
    pending_count: int = 0

    def trim(self, now: float, horizon: float) -> None:
        cutoff = now - horizon
        while self.ticks and self.ticks[0][0] < cutoff:
            self.ticks.popleft()


def _robust_high(prices: list[float], multiple: float) -> float:
    """Rolling high with single bogus spikes excluded.

    A lone print far above the median would otherwise become the baseline and
    manufacture a dip. Anything within `multiple` MADs of the median is kept;
    if that would discard everything, fall back to the plain maximum.
    """
    if len(prices) < 4 or multiple <= 0:
        return max(prices)
    median = statistics.median(prices)
    if median <= 0:
        return max(prices)
    deviations = [abs(p - median) for p in prices]
    mad = statistics.median(deviations)
    # A quiet series has MAD 0, which would make the test vacuous and let the
    # spike through - exactly the case this guard is for. Floor it at 1% of the
    # median so "no observed variation" still admits ordinary noise.
    mad = max(mad, median * 0.01)
    kept = [p for p in prices if (p - median) <= multiple * mad]
    return max(kept) if kept else max(prices)


class Detector:
    """Turns a stream of `PriceTick`s into `Alert`s."""

    def __init__(self, settings: dict):
        self.settings = settings
        self.states: dict[str, TokenState] = {}
        self.suppressed: dict[str, int] = {}
        #: Per-token setting overrides, keyed by mint. A token that behaves
        #: unlike its profile can be tuned without moving everyone else.
        self.overrides: dict[str, dict] = {}

    def effective(self, mint: str) -> dict:
        """Settings for one token, with its overrides applied on top."""
        override = self.overrides.get(mint)
        if not override:
            return self.settings
        merged = dict(self.settings)
        merged.update(override)
        return merged

    # --- settings helpers ---------------------------------------------------

    def _enabled_windows(self, settings: dict) -> tuple[str, ...]:
        raw = str(settings.get("enabled_windows") or "").split(",")
        chosen = {w.strip() for w in raw if w.strip()}
        return tuple(w for w in WINDOW_ORDER if w in chosen)

    def _threshold(self, settings: dict, window: str) -> float:
        return float(settings.get(f"threshold_{PROFILE}_{window}") or 0)

    def _min_ticks(self, settings: dict, window: str) -> int:
        return int(settings.get(f"min_ticks_{window}") or 1)

    @staticmethod
    def _on_bonding_curve(tick: PriceTick, meta: TokenMeta | None) -> bool:
        """True while the coin still trades on its launch curve."""
        from .models import PriceSource

        if tick.source is PriceSource.BONDING_CURVE:
            return True
        return meta is not None and meta.is_bonding_curve

    def _note(self, reason: str) -> None:
        self.suppressed[reason] = self.suppressed.get(reason, 0) + 1

    # --- main entry point ---------------------------------------------------

    def on_tick(
        self,
        tick: PriceTick,
        meta: TokenMeta | None = None,
        tier: Tier = Tier.REALTIME,
    ) -> Alert | None:
        settings = self.effective(tick.mint)

        state = self.states.get(tick.mint)
        if state is None:
            state = self.states[tick.mint] = TokenState(first_tick_at=tick.ts)

        # A gap in the feed means the buffer no longer describes the present.
        warmup = float(settings.get("warmup_seconds") or 0)
        gap = tick.ts - state.last_tick_at if state.last_tick_at else 0.0
        if warmup and gap > warmup:
            state.ticks.clear()
            state.first_tick_at = tick.ts
            state.pending_window = None
            state.pending_count = 0

        state.last_tick_at = tick.ts
        state.ticks.append((tick.ts, tick.price_sol))
        state.trim(tick.ts, max(WINDOW_SECONDS.values()) + 30)

        if warmup and tick.ts - state.first_tick_at < warmup:
            self._note("warmup")
            return None

        # Pre-bond coins are not alerted on at all. Their launch-curve
        # volatility is indistinguishable from a real dip, so waiting until the
        # coin bonds is more honest than tuning a threshold around the noise.
        if self._on_bonding_curve(tick, meta):
            self._note("not bonded yet")
            return None

        if meta is not None and not check_token(meta, settings):
            self._note("filtered")
            return None

        # Global per-token cooldown: this is what collapses the cascade.
        cooldown = float(settings.get("cooldown_per_token_seconds") or 0)
        if state.last_alert_at and tick.ts - state.last_alert_at < cooldown:
            self._note("cooldown")
            return None

        # Re-arm: after an alert the price must recover part of the fall before
        # the same slide can alert again.
        if not state.armed:
            recovery = float(settings.get("rearm_recovery_pct") or 0)
            drop = state.last_alert_baseline - state.last_alert_price
            needed = state.last_alert_price + drop * recovery / 100
            if drop <= 0 or tick.price_sol >= needed:
                state.armed = True
            else:
                self._note("not re-armed")
                return None

        allowed = set(tier.allowed_windows)
        candidate = None

        for window in self._enabled_windows(settings):
            if window not in allowed:
                continue  # polled data cannot support 10s/1m
            threshold = self._threshold(settings, window)
            if threshold <= 0:
                continue

            span = WINDOW_SECONDS[window]
            recent = [p for ts, p in state.ticks if ts >= tick.ts - span]
            if len(recent) < self._min_ticks(settings, window):
                continue

            baseline = _robust_high(recent, float(settings.get("outlier_median_multiple") or 0))
            if baseline <= 0:
                continue
            drawdown = (baseline - tick.price_sol) / baseline * 100
            if drawdown >= threshold:
                # Fastest window wins; slower ones describe the same event.
                candidate = (window, drawdown, baseline)
                break

        if candidate is None:
            state.pending_window = None
            state.pending_count = 0
            return None

        window, drawdown, baseline = candidate

        # Confirmation: the fall must still be there on the next tick, which
        # filters single bad prints at the cost of a couple of seconds.
        confirm = int(settings.get("confirm_ticks") or 0)
        if confirm:
            if state.pending_window == window:
                state.pending_count += 1
            else:
                state.pending_window = window
                state.pending_count = 0
            if state.pending_count < confirm:
                self._note("awaiting confirmation")
                return None

        state.pending_window = None
        state.pending_count = 0
        state.last_alert_at = tick.ts
        state.last_alert_price = tick.price_sol
        state.last_alert_baseline = baseline
        state.armed = False

        return Alert(
            mint=tick.mint,
            window=window,
            drawdown=drawdown,
            price_sol=tick.price_sol,
            baseline_sol=baseline,
            tier=tier,
            meta=meta,
            ts=tick.ts,
        )

    def stats(self) -> dict:
        return {
            "tokens": len(self.states),
            "buffered_ticks": sum(len(s.ticks) for s in self.states.values()),
            "overrides": len(self.overrides),
            "suppressed": dict(self.suppressed),
        }
