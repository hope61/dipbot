"""Phase 0 recorder: capture PumpPortal WS + DexScreener polls side by side.

Runs two concurrent tasks for DURATION seconds:

  ws   - subscribeTokenTrade on the migrated tokens from tokens.json, plus
         bonding-curve mints harvested live from subscribeNewToken. Every frame
         is written with a local receive timestamp.
  poll - /tokens/v1/solana/{addrs} every POLL_EVERY seconds for the same tokens,
         recording the cache age header so WS-vs-DexScreener lead time is
         measurable.

Writes spike/data/ws.jsonl and spike/data/poll.jsonl.

Usage: python3 spike/record.py [duration_seconds]
"""
import asyncio
import json
import pathlib
import sys
import time

import httpx
import websockets

WS_URL = "wss://pumpportal.fun/api/data"
BASE = "https://api.dexscreener.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

HERE = pathlib.Path(__file__).parent
DATA = HERE / "data"
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 600
POLL_EVERY = 20
MAX_NEW_MINTS = 60  # cap on bonding-curve mints harvested from newToken
BATCH = 30

stats = {"frames": 0, "trades": 0, "creates": 0, "polls": 0, "reconnects": 0}


async def ws_task(seed_mints: list[str], out, stop: asyncio.Event, tracked: dict):
    """Record WS frames. Reconnects with backoff; resubscribes everything."""
    backoff = 1.0
    new_mints: list[str] = []

    while not stop.is_set():
        try:
            async with websockets.connect(WS_URL, ping_interval=20) as ws:
                backoff = 1.0
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                for i in range(0, len(seed_mints + new_mints), BATCH):
                    keys = (seed_mints + new_mints)[i : i + BATCH]
                    await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": keys}))
                print(f"[ws] connected, subscribed {len(seed_mints + new_mints)} mints")

                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        print("[ws] 30s silent")
                        continue
                    now = time.time()
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    stats["frames"] += 1
                    out.write(json.dumps({"recv_ts": now, "msg": msg}) + "\n")

                    tx = msg.get("txType")
                    mint = msg.get("mint")
                    if tx == "create":
                        stats["creates"] += 1
                        if mint and len(new_mints) < MAX_NEW_MINTS:
                            new_mints.append(mint)
                            tracked[mint] = "bonding"
                            await ws.send(
                                json.dumps({"method": "subscribeTokenTrade", "keys": [mint]})
                            )
                    elif tx in ("buy", "sell"):
                        stats["trades"] += 1
                        if stats["trades"] % 50 == 0:
                            print(f"[ws] trades={stats['trades']} frames={stats['frames']}")
        except Exception as e:
            if stop.is_set():
                break
            stats["reconnects"] += 1
            print(f"[ws] disconnected ({type(e).__name__}: {e}); retry in {backoff:.0f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def poll_task(mints: list[str], out, stop: asyncio.Event):
    """Poll DexScreener for the seed tokens, recording cache age."""
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=25) as client:
        while not stop.is_set():
            t0 = time.time()
            for i in range(0, len(mints), BATCH):
                chunk = mints[i : i + BATCH]
                try:
                    r = await client.get(f"{BASE}/tokens/v1/solana/{','.join(chunk)}")
                    r.raise_for_status()
                except Exception as e:
                    print(f"[poll] failed: {e}")
                    continue
                out.write(
                    json.dumps(
                        {
                            "recv_ts": time.time(),
                            "cf_age": r.headers.get("age"),
                            "cf_cache": r.headers.get("cf-cache-status"),
                            "pairs": [
                                {
                                    "mint": p["baseToken"]["address"],
                                    "symbol": p["baseToken"].get("symbol"),
                                    "dex_id": p.get("dexId"),
                                    "price_usd": p.get("priceUsd"),
                                    "price_native": p.get("priceNative"),
                                    "liquidity_usd": (p.get("liquidity") or {}).get("usd"),
                                    "txns_m5": (p.get("txns") or {}).get("m5"),
                                }
                                for p in r.json()
                            ],
                        }
                    )
                    + "\n"
                )
                stats["polls"] += 1
            out.flush()
            await asyncio.sleep(max(0.0, POLL_EVERY - (time.time() - t0)))


async def main() -> None:
    DATA.mkdir(exist_ok=True)
    tokens = json.loads((HERE / "tokens.json").read_text())
    seed = [t["mint"] for t in tokens]
    tracked = {t["mint"]: t["tier"] for t in tokens}
    print(f"recording {DURATION}s | seed mints: {len(seed)} | poll every {POLL_EVERY}s\n")

    stop = asyncio.Event()
    with (DATA / "ws.jsonl").open("w") as ws_out, (DATA / "poll.jsonl").open("w") as poll_out:
        tasks = [
            asyncio.create_task(ws_task(seed, ws_out, stop, tracked)),
            asyncio.create_task(poll_task(seed, poll_out, stop)),
        ]
        try:
            await asyncio.sleep(DURATION)
        finally:
            stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            ws_out.flush()

    (DATA / "tracked.json").write_text(json.dumps(tracked, indent=2))
    print(f"\ndone: {stats}")
    print(f"wrote {DATA / 'ws.jsonl'} and {DATA / 'poll.jsonl'}")


if __name__ == "__main__":
    asyncio.run(main())
