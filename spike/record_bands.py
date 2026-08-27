"""Record real price ticks across liquidity bands, for Phase 3 replay tuning.

Specific coins don't generalise; classes of coin do. So the sample is picked
across bands rather than from a watchlist:

    fresh    - pump.fun bonding curves minutes old (the 49-93% swing case)
    thin     - recently migrated PumpSwap pools, low liquidity
    established - PumpSwap pools with real depth

Writes one JSON line per price change to spike/data/ticks_<band>.jsonl:

    {"ts": 1787.., "slot": 39.., "mint": "..", "symbol": "..",
     "band": "thin", "source": "pool", "price_sol": 1.23e-8}

Prices come from slot-complete state only - a half-updated pool produced a
phantom 30% swing in Phase 0.

Usage: python3 spike/record_bands.py [minutes]
"""
from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import struct
import sys
import time

import httpx
import websockets

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import vaults as V  # noqa: E402

HERE = pathlib.Path(__file__).parent
DATA = HERE / "data"
RPC_WS = "wss://api.mainnet-beta.solana.com"
PUMP_WS = "wss://pumpportal.fun/api/data"
DEX = "https://api.dexscreener.com"
UA = V.UA

MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
PUMP_CURVE_DECIMALS = 6

BANDS = {
    "thin": (1_000, 60_000),
    "established": (60_000, 10_000_000_000),
}
PER_BAND = 8
FRESH_TARGET = 10


async def dex_get(client: httpx.AsyncClient, path: str):
    r = await client.get(DEX + path, params={"_": str(int(time.time() * 1000))})
    r.raise_for_status()
    return r.json()


async def pick_pool_tokens(client: httpx.AsyncClient) -> list[dict]:
    """Pick migrated PumpSwap tokens spread across liquidity bands."""
    mints: list[str] = []
    for path in ("/token-profiles/latest/v1", "/token-boosts/top/v1", "/token-boosts/latest/v1"):
        try:
            data = await dex_get(client, path)
        except Exception:
            continue
        mints += [d["tokenAddress"] for d in data if d.get("chainId") == "solana" and d.get("tokenAddress")]
        await asyncio.sleep(0.3)
    mints = list(dict.fromkeys(mints))

    pairs = []
    for i in range(0, len(mints), 30):
        try:
            pairs += await dex_get(client, f"/tokens/v1/solana/{','.join(mints[i:i + 30])}")
        except Exception:
            pass
        await asyncio.sleep(0.3)

    best: dict[str, dict] = {}
    for p in pairs:
        if p.get("chainId") != "solana" or p.get("dexId") != "pumpswap":
            continue
        liq = (p.get("liquidity") or {}).get("usd") or 0
        txns = (p.get("txns") or {}).get("m5") or {}
        if txns.get("buys", 0) + txns.get("sells", 0) < 3:
            continue  # dead pools teach us nothing
        mint = p["baseToken"]["address"]
        if liq > ((best.get(mint) or {}).get("liquidity") or {}).get("usd", 0):
            best[mint] = p

    picked = []
    for band, (lo, hi) in BANDS.items():
        in_band = [
            p for p in best.values() if lo <= ((p.get("liquidity") or {}).get("usd") or 0) < hi
        ]
        in_band.sort(
            key=lambda p: -((p.get("txns") or {}).get("m5") or {}).get("buys", 0)
        )
        for p in in_band[:PER_BAND]:
            picked.append(
                {
                    "mint": p["baseToken"]["address"],
                    "symbol": p["baseToken"].get("symbol"),
                    "pair": p["pairAddress"],
                    "band": band,
                    "liquidity_usd": (p.get("liquidity") or {}).get("usd"),
                }
            )
    return picked


async def harvest_fresh(seconds: float = 60.0) -> list[dict]:
    """Grab brand-new pump.fun launches straight off the creation stream."""
    out: list[dict] = []
    try:
        async with websockets.connect(PUMP_WS, ping_interval=20) as ws:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            t0 = time.time()
            while time.time() - t0 < seconds and len(out) < FRESH_TARGET:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                except Exception:
                    break
                if m.get("txType") == "create" and m.get("bondingCurveKey"):
                    out.append(
                        {
                            "mint": m["mint"],
                            "symbol": m.get("symbol"),
                            "curve": m["bondingCurveKey"],
                            "band": "fresh",
                            "liquidity_usd": None,
                        }
                    )
    except Exception as e:
        print(f"  fresh harvest failed: {e}")
    return out


async def resolve_vaults(client: httpx.AsyncClient, tokens: list[dict]) -> list[dict]:
    ok = []
    for t in tokens:
        try:
            found = await V.discover(client, t["pair"], t["mint"])
            if not found:
                continue
            mint_acc = (await V.rpc(client, "getAccountInfo", [t["mint"], {"encoding": "base64"}]))["value"]
            t["decimals"] = base64.b64decode(mint_acc["data"][0])[44]
            t["base_vault"] = found["base_vault"]["pubkey"]
            t["quote_vault"] = found["quote_vault"]["pubkey"]
            t["base_amount"] = found["base_vault"]["amount"]
            t["quote_amount"] = found["quote_vault"]["amount"]
            ok.append(t)
            print(f"  {t['band']:11} {(t['symbol'] or '?')[:10]:10} liq=${t['liquidity_usd'] or 0:>11,.0f}")
        except Exception as e:
            print(f"  {(t.get('symbol') or '?')[:10]:10} resolve failed: {str(e)[:60]}")
        await asyncio.sleep(0.4)
    return ok


def curve_price(data: bytes) -> float | None:
    if len(data) < 48:
        return None
    v_tok, v_sol = struct.unpack_from("<2Q", data, 8)
    if not v_tok:
        return None
    return (v_sol / 1e9) / (v_tok / 10**PUMP_CURVE_DECIMALS)


async def record(pools: list[dict], curves: list[dict], seconds: float) -> None:
    DATA.mkdir(exist_ok=True)
    files = {b: (DATA / f"ticks_{b}.jsonl").open("a") for b in ("fresh", "thin", "established")}

    sub_map: dict[int, tuple[str, str]] = {}  # sub id -> (mint, role)
    state: dict[str, dict] = {}
    req_id = 0
    pending: list[tuple[int, str, str]] = []

    for p in pools:
        state[p["mint"]] = {
            **p, "base": p["base_amount"], "quote": p["quote_amount"],
            "kind": "pool", "pending_slot": None, "ticks": 0,
        }
        for role, acct in (("base", p["base_vault"]), ("quote", p["quote_vault"])):
            req_id += 1
            pending.append((req_id, acct, p["mint"]))
            sub_map[req_id] = (p["mint"], role)
    for c in curves:
        state[c["mint"]] = {**c, "kind": "curve", "ticks": 0}
        req_id += 1
        pending.append((req_id, c["curve"], c["mint"]))
        sub_map[req_id] = (c["mint"], "curve")

    id_to_role = dict(sub_map)
    live: dict[int, tuple[str, str]] = {}
    total = 0
    t0 = time.time()

    async with websockets.connect(RPC_WS, ping_interval=20, max_size=2**23) as ws:
        for rid, acct, _mint in pending:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": rid, "method": "accountSubscribe",
                "params": [acct, {"encoding": "base64", "commitment": "processed"}],
            }))
            await asyncio.sleep(0.04)
        print(f"\nsubscribed {len(pending)} accounts, recording {seconds / 60:.0f} min\n")

        while time.time() - t0 < seconds:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"  ws closed: {e}")
                break

            if "result" in msg and isinstance(msg["result"], int):
                live[msg["result"]] = id_to_role[msg["id"]]
                continue
            if msg.get("method") != "accountNotification":
                continue

            params = msg["params"]
            entry = live.get(params["subscription"])
            if not entry:
                continue
            mint, role = entry
            st = state[mint]
            slot = params["result"]["context"]["slot"]
            data = base64.b64decode(params["result"]["value"]["data"][0])

            if st["kind"] == "curve":
                price = curve_price(data)
                if price is None:
                    continue
            else:
                amount = struct.unpack_from("<Q", data, 64)[0]
                # emit the previous slot before applying a newer one
                if st["pending_slot"] is not None and slot != st["pending_slot"] and st["base"] > 0:
                    price = (st["quote"] / 1e9) / (st["base"] / 10 ** st["decimals"])
                    files[st["band"]].write(json.dumps({
                        "ts": round(time.time(), 3), "slot": st["pending_slot"], "mint": mint,
                        "symbol": st["symbol"], "band": st["band"], "source": "pool",
                        "price_sol": price,
                    }) + "\n")
                    st["ticks"] += 1
                    total += 1
                st["pending_slot"] = slot
                st[role] = amount
                continue

            files[st["band"]].write(json.dumps({
                "ts": round(time.time(), 3), "slot": slot, "mint": mint,
                "symbol": st["symbol"], "band": st["band"], "source": "curve",
                "price_sol": price,
            }) + "\n")
            st["ticks"] += 1
            total += 1

            if total % 200 == 0:
                for f in files.values():
                    f.flush()
                print(f"  [{(time.time() - t0) / 60:4.1f}m] {total} ticks")

    for f in files.values():
        f.flush()
        f.close()

    elapsed = time.time() - t0
    print(f"\nrecorded {total} ticks in {elapsed / 60:.1f} min")
    by_band: dict[str, int] = {}
    for st in state.values():
        by_band[st["band"]] = by_band.get(st["band"], 0) + st["ticks"]
    for band, n in sorted(by_band.items()):
        print(f"  {band:12} {n:6} ticks")
    (DATA / "record_manifest.json").write_text(json.dumps(
        [{k: v for k, v in st.items() if k not in ("pending_slot",)} for st in state.values()],
        indent=2, default=str,
    ))


async def main() -> None:
    print("picking tokens across bands...")
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=30) as client:
        pool_tokens = await pick_pool_tokens(client)
        print(f"  {len(pool_tokens)} pool candidates")
        print("harvesting fresh launches (60s)...")
        curves = await harvest_fresh(60)
        print(f"  {len(curves)} fresh curves")
        print("resolving vaults...")
        pools = await resolve_vaults(client, pool_tokens)

    if not pools and not curves:
        print("nothing to record")
        return
    await record(pools, curves, MINUTES * 60)


if __name__ == "__main__":
    asyncio.run(main())
