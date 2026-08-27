"""Phase 0 scale + threshold test.

Subscribes to both vaults of every pool in vaults.json over ONE RPC WebSocket,
rebuilds per-trade prices, and runs the real detector logic against the live
stream. Answers three things at once:

  1. can one connection carry the whole watchlist on this RPC endpoint?
  2. how many price ticks per token per minute do we actually get?
  3. how many alerts would the default thresholds fire?

Prices are computed only from slot-complete state: a vault update is applied to
the token's reserves, and a tick is emitted only once the slot has advanced, so
we never price a half-updated pool (that artifact produced phantom ~15% dips).

Usage: python3 spike/scale_test.py [duration_seconds] [rpc_ws_url]
"""
import asyncio
import base64
import collections
import json
import pathlib
import struct
import sys
import time

import websockets

HERE = pathlib.Path(__file__).parent
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 300
WS_URL = sys.argv[2] if len(sys.argv) > 2 else "wss://api.mainnet-beta.solana.com"

WINDOWS = {"10s": 10, "1m": 60, "3m": 180, "5m": 300}
THRESHOLDS = {"10s": 5.0, "1m": 10.0, "3m": 15.0, "5m": 20.0}
MIN_TICKS = {"10s": 3, "1m": 4, "3m": 4, "5m": 4}
COOLDOWN = 600.0


class Detector:
    """Rolling-high drawdown detector, one instance per token."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.ticks: collections.deque = collections.deque()
        self.last_alert: dict[str, float] = {}
        self.alerts: list[dict] = []

    def add(self, ts: float, price: float) -> None:
        self.ticks.append((ts, price))
        cutoff = ts - max(WINDOWS.values()) - 30
        while self.ticks and self.ticks[0][0] < cutoff:
            self.ticks.popleft()

        for name, span in WINDOWS.items():
            recent = [(t, p) for t, p in self.ticks if t >= ts - span]
            if len(recent) < MIN_TICKS[name]:
                continue
            baseline = max(p for _, p in recent)
            if baseline <= 0:
                continue
            drawdown = (baseline - price) / baseline * 100
            if drawdown < THRESHOLDS[name]:
                continue
            if ts - self.last_alert.get(name, 0) < COOLDOWN:
                continue
            self.last_alert[name] = ts
            self.alerts.append(
                {"window": name, "drawdown": drawdown, "price": price, "baseline": baseline, "ts": ts}
            )
            print(
                f"  ALERT {self.symbol:>10} {name:>3} -{drawdown:5.1f}%  "
                f"{baseline:.4e} -> {price:.4e} SOL  ({len(recent)} ticks)"
            )


async def main() -> None:
    pools = json.loads((HERE / "vaults.json").read_text())
    acct_to_pool: dict[str, tuple[str, str]] = {}
    state: dict[str, dict] = {}
    for p in pools:
        mint = p["mint"]
        acct_to_pool[p["base_vault"]["pubkey"]] = (mint, "base")
        acct_to_pool[p["quote_vault"]["pubkey"]] = (mint, "quote")
        state[mint] = {
            "symbol": p["symbol"] or mint[:6],
            "dec": p["base_decimals"],
            "base": p["base_vault"]["amount"],
            "quote": p["quote_vault"]["amount"],
            "det": Detector(p["symbol"] or mint[:6]),
            "ticks": 0,
            "pending_slot": None,
        }

    print(f"subscribing {len(acct_to_pool)} accounts ({len(pools)} pools) on {WS_URL}")
    print(f"thresholds {THRESHOLDS}, running {DURATION}s\n")

    subs: dict[int, str] = {}
    id_to_acct: dict[int, str] = {}
    notifications = 0
    errors: list[str] = []
    t0 = time.time()

    async with websockets.connect(WS_URL, ping_interval=20, max_size=2**23) as ws:
        for i, acct in enumerate(acct_to_pool, start=1):
            id_to_acct[i] = acct
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": i,
                        "method": "accountSubscribe",
                        "params": [acct, {"encoding": "base64", "commitment": "processed"}],
                    }
                )
            )
            await asyncio.sleep(0.05)

        while time.time() - t0 < DURATION:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            except asyncio.TimeoutError:
                print(f"  [{time.time() - t0:5.0f}s] 30s with no notifications")
                continue
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")
                break

            if "result" in msg and isinstance(msg["result"], int):
                subs[msg["result"]] = id_to_acct[msg["id"]]
                continue
            if "error" in msg:
                errors.append(json.dumps(msg["error"])[:200])
                continue
            if msg.get("method") != "accountNotification":
                continue

            params = msg["params"]
            acct = subs.get(params["subscription"])
            if not acct:
                continue
            mint, side = acct_to_pool[acct]
            st = state[mint]
            slot = params["result"]["context"]["slot"]
            data = base64.b64decode(params["result"]["value"]["data"][0])
            amount = struct.unpack_from("<Q", data, 64)[0]
            notifications += 1

            # emit the previous slot's price before applying a newer slot's update
            if st["pending_slot"] is not None and slot != st["pending_slot"] and st["base"] > 0:
                price = (st["quote"] / 1e9) / (st["base"] / 10 ** st["dec"])
                st["det"].add(time.time(), price)
                st["ticks"] += 1
            st["pending_slot"] = slot
            st[side] = amount

    elapsed = time.time() - t0
    print(f"\n--- {elapsed:.0f}s on one connection ---")
    print(f"subscriptions confirmed: {len(subs)}/{len(acct_to_pool)}")
    print(f"notifications: {notifications}  ({notifications / elapsed * 60:.1f}/min)")
    if errors:
        print(f"errors: {errors[:5]}")

    total_alerts = 0
    print(f"\n{'token':>10} {'ticks':>6} {'ticks/min':>9} {'alerts':>6}")
    for mint, st in sorted(state.items(), key=lambda kv: -kv[1]["ticks"]):
        n = len(st["det"].alerts)
        total_alerts += n
        print(f"{st['symbol'][:10]:>10} {st['ticks']:6} {st['ticks'] / elapsed * 60:9.1f} {n:6}")
    print(f"\ntotal alerts at default thresholds: {total_alerts} in {elapsed / 60:.1f} min")

    (HERE / "data").mkdir(exist_ok=True)
    (HERE / "data" / "scale_result.json").write_text(
        json.dumps(
            {
                "elapsed": elapsed,
                "ws_url": WS_URL,
                "accounts": len(acct_to_pool),
                "subs_confirmed": len(subs),
                "notifications": notifications,
                "errors": errors[:20],
                "per_token": {
                    st["symbol"]: {"ticks": st["ticks"], "alerts": st["det"].alerts}
                    for st in state.values()
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
