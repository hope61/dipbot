"""Replay recorded ticks through the real detector.

Answers "how many alerts would these settings have fired?" in seconds, against
the same market every time - which is the only way to compare two candidate
thresholds fairly.

Usage:
    python3 spike/replay.py                    # current .env settings
    python3 spike/replay.py --sweep            # try a grid of thresholds
    python3 spike/replay.py --band thin
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dipbot import config  # noqa: E402
from dipbot.detector import Detector  # noqa: E402
from dipbot.models import PriceSource, PriceTick, Tier  # noqa: E402

DATA = pathlib.Path(__file__).parent / "data"


def load_ticks(band: str | None = None) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(DATA.glob("ticks_*.jsonl")):
        if band and path.stem != f"ticks_{band}":
            continue
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows.sort(key=lambda r: r["ts"])
    return rows


def replay(rows: list[dict], settings: dict) -> tuple[list, Detector]:
    """Feed rows through the detector; metadata is skipped so filters don't
    mask threshold behaviour - this measures the detector, not the filters."""
    detector = Detector(settings)
    alerts = []
    for row in rows:
        source = PriceSource.BONDING_CURVE if row["source"] == "curve" else PriceSource.POOL_VAULTS
        tick = PriceTick(
            mint=row["mint"], price_sol=row["price_sol"], ts=row["ts"], source=source,
            slot=row.get("slot"),
        )
        alert = detector.on_tick(tick, meta=None, tier=Tier.REALTIME)
        if alert:
            alerts.append((alert, row.get("symbol") or row["mint"][:8], row["band"]))
    return alerts, detector


def summarise(rows: list[dict]) -> float:
    if len(rows) < 2:
        return 0.0
    return (rows[-1]["ts"] - rows[0]["ts"]) / 3600


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--band", help="fresh | thin | established")
    ap.add_argument("--sweep", action="store_true", help="try a grid of thresholds")
    args = ap.parse_args()

    rows = load_ticks(args.band)
    if not rows:
        print("no recorded ticks found - run spike/record_bands.py first")
        return
    hours = summarise(rows)
    tokens = {r["mint"] for r in rows}
    print(f"{len(rows):,} ticks · {len(tokens)} tokens · {hours:.2f}h of market\n")

    base = dict(config.load().seed)

    if not args.sweep:
        alerts, detector = replay(rows, base)
        print(f"alerts: {len(alerts)}  ({len(alerts) / hours if hours else 0:.1f}/hour)\n")
        for alert, symbol, band in alerts[:25]:
            print(f"  {band:11} {symbol[:12]:12} {alert.window:>3} -{alert.drawdown:5.1f}%")
        if len(alerts) > 25:
            print(f"  ... and {len(alerts) - 25} more")
        print(f"\nsuppressed: {detector.stats()['suppressed']}")
        return

    print(f"{'thresholds':32} {'alerts':>7} {'per hour':>9} {'tokens hit':>11}")
    grid = [
        (5, 10, 15, 20),
        (8, 15, 22, 30),
        (12, 20, 30, 40),
        (15, 25, 35, 45),
        (20, 30, 40, 50),
        (25, 40, 50, 60),
    ]
    for combo in grid:
        settings = dict(base)
        for window, value in zip(("10s", "1m", "3m", "5m"), combo):
            settings[f"threshold_migrated_{window}"] = float(value)
        alerts, _ = replay(rows, settings)
        hit = len({a[0].mint for a in alerts})
        label = "/".join(f"{c}%" for c in combo)
        print(f"{label:32} {len(alerts):7} {len(alerts) / hours if hours else 0:9.1f} {hit:11}")


if __name__ == "__main__":
    main()
