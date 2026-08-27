"""Cross-checks our on-chain price against DexScreener's.

The feed prices a coin from pool reserves; DexScreener prices it independently.
They should agree closely - measured 1.06x on a healthy pool. A persistent large
disagreement means we are reading the wrong account, the wrong pool, or the wrong
decimals, and every alert for that coin would be built on a wrong number.

The check is deliberately slow to react. During a real 50% crash our live price
legitimately diverges from DexScreener's 30-second-old one, so a single sample
proves nothing. Only a *median* disagreement across several samples, taken at
different moments, indicates a broken mapping.
"""
from __future__ import annotations

import logging
import statistics
from collections import deque

log = logging.getLogger(__name__)

#: Beyond this factor the two sources are not describing the same thing.
#: Healthy pools measured 1.06x; a broken mapping measured 70x.
MAX_DISAGREEMENT = 5.0
#: Samples needed before judging. Fewer would trip on transient staleness.
MIN_SAMPLES = 3
HISTORY = 10


class PriceSanity:
    def __init__(self, max_disagreement: float = MAX_DISAGREEMENT):
        self.max_disagreement = max_disagreement
        self._ratios: dict[str, deque] = {}
        #: Latest (our price, reported cap) pair per mint. Quoting a cap means
        #: moving the reported one by our price change since it was taken.
        self._anchor: dict[str, tuple[float, float]] = {}
        self.distrusted: dict[str, float] = {}

    def observe(self, mint: str, chain_price: float, reference_price: float) -> None:
        """Record one paired observation of the two sources."""
        if chain_price <= 0 or reference_price <= 0:
            return
        ratios = self._ratios.setdefault(mint, deque(maxlen=HISTORY))
        ratios.append(chain_price / reference_price)

        if len(ratios) < MIN_SAMPLES:
            return

        median = statistics.median(ratios)
        off = median > self.max_disagreement or median < 1 / self.max_disagreement
        was = mint in self.distrusted

        if off and not was:
            self.distrusted[mint] = median
            log.warning(
                "%s price disagrees with DexScreener by %.1fx - alerts suppressed "
                "until it agrees again", mint[:8], median,
            )
        elif not off and was:
            del self.distrusted[mint]
            log.info("%s price agrees with DexScreener again (%.2fx)", mint[:8], median)

    def observe_cap(self, mint: str, chain_price: float, market_cap: float) -> None:
        """Anchor the reported market cap to our price at the same moment.

        Caps are then quoted as `anchor_cap x (live_price / anchor_price)`:
        the number a chart shows, moved by our own price change since. Two
        alternatives were tried and rejected -

        A median of `cap / price` over many polls compounded the drift between
        a live price and a lagging cap; it wandered 26.7% on a live coin and
        published a 187k peak where the real high was about 165k.

        Deriving the cap from our price alone is arithmetically sound - our
        spot price balances the pool's two sides exactly, where the reported
        price sometimes does not - but on one live coin that put us 71% below
        the chart, and the chart is what people check against.
        """
        if chain_price <= 0 or market_cap <= 0:
            return
        self._anchor[mint] = (chain_price, market_cap)

    def cap_scale(self, mint: str) -> float | None:
        anchor = self._anchor.get(mint)
        if not anchor:
            return None
        price, cap = anchor
        return cap / price

    def market_cap(self, mint: str, chain_price: float) -> float | None:
        """Market cap implied by a chain price, or None without an anchor."""
        anchor = self._anchor.get(mint)
        if not anchor:
            return None
        anchor_price, anchor_cap = anchor
        return anchor_cap * (chain_price / anchor_price)

    def is_trusted(self, mint: str) -> bool:
        return mint not in self.distrusted

    def ratio(self, mint: str) -> float | None:
        ratios = self._ratios.get(mint)
        return statistics.median(ratios) if ratios else None

    def forget(self, mint: str) -> None:
        self._ratios.pop(mint, None)
        self._anchor.pop(mint, None)
        self.distrusted.pop(mint, None)

    def stats(self) -> dict:
        return {
            "tracked": len(self._ratios),
            "calibrated": len(self._anchor),
            "distrusted": dict(self.distrusted),
        }
