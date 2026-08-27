"""Phase 0: can we price pre-migration (bonding curve) pump.fun tokens?

Post-migration tokens are priced from pool vault balances. Pre-migration ones
have no pool at all — price lives in the bonding curve account's virtual
reserves. This checks whether that account can be decoded and subscribed to the
same way.

Method:
  1. harvest fresh mints from PumpPortal's free subscribeNewToken stream, which
     hands us bondingCurveKey plus its own vSol/vTokens figures
  2. fetch those bonding curve accounts and decode the Anchor layout
  3. cross-check decoded reserves against PumpPortal's numbers - if they agree,
     the layout is right
  4. accountSubscribe to them and count real price ticks

Writes spike/data/bonding.json.
"""
import asyncio
import base64
import json
import pathlib
import struct
import sys
import time

import httpx
import websockets

PUMP_WS = "wss://pumpportal.fun/api/data"
RPC_HTTP = "https://api.mainnet-beta.solana.com"
RPC_WS = "wss://api.mainnet-beta.solana.com"
HERE = pathlib.Path(__file__).parent

HARVEST_SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 90
WATCH_SECS = int(sys.argv[2]) if len(sys.argv) > 2 else 180
MAX_TOKENS = 40

# BondingCurve account: 8-byte anchor discriminator, then five u64 and a bool.
FIELDS = [
    "virtual_token_reserves",
    "virtual_sol_reserves",
    "real_token_reserves",
    "real_sol_reserves",
    "token_total_supply",
]


def decode_curve(data: bytes) -> dict | None:
    if len(data) < 8 + 8 * 5 + 1:
        return None
    vals = struct.unpack_from("<5Q", data, 8)
    out = dict(zip(FIELDS, vals))
    out["complete"] = bool(data[8 + 40])
    if out["virtual_token_reserves"] == 0:
        return None
    out["price_sol"] = (out["virtual_sol_reserves"] / 1e9) / (
        out["virtual_token_reserves"] / 1e6
    )
    return out


async def harvest() -> list[dict]:
    """Collect fresh mints + their bondingCurveKey from PumpPortal."""
    found: list[dict] = []
    async with websockets.connect(PUMP_WS, ping_interval=20) as ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        t0 = time.time()
        while time.time() - t0 < HARVEST_SECS and len(found) < MAX_TOKENS:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
            if m.get("txType") != "create" or not m.get("bondingCurveKey"):
                continue
            found.append(
                {
                    "mint": m["mint"],
                    "symbol": m.get("symbol"),
                    "curve": m["bondingCurveKey"],
                    "pp_v_sol": m.get("vSolInBondingCurve"),
                    "pp_v_tokens": m.get("vTokensInBondingCurve"),
                    "pp_mcap_sol": m.get("marketCapSol"),
                    "created_ts": time.time(),
                }
            )
    print(f"harvested {len(found)} fresh mints in {HARVEST_SECS}s")
    return found


async def rpc(client: httpx.AsyncClient, method: str, params: list, tries: int = 5):
    delay = 1.0
    for _ in range(tries):
        r = await client.post(
            RPC_HTTP, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        if r.status_code == 429:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 20)
            continue
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(body["error"])
        return body["result"]
    raise RuntimeError("rate limited")


async def verify_layout(tokens: list[dict]) -> list[dict]:
    """Decode each curve account and compare against PumpPortal's own figures."""
    ok: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        keys = [t["curve"] for t in tokens]
        accs = []
        for i in range(0, len(keys), 100):
            accs += (
                await rpc(client, "getMultipleAccounts", [keys[i : i + 100], {"encoding": "base64"}])
            )["value"]
            await asyncio.sleep(0.3)

    print(f"\n{'symbol':>10} {'decoded SOL':>12} {'pumpportal':>12} {'diff':>8}  {'tokens match':>12}")
    for t, acc in zip(tokens, accs):
        if not acc:
            print(f"{(t['symbol'] or '?')[:10]:>10}   curve account not found")
            continue
        cur = decode_curve(base64.b64decode(acc["data"][0]))
        if not cur:
            print(f"{(t['symbol'] or '?')[:10]:>10}   decode failed")
            continue
        v_sol = cur["virtual_sol_reserves"] / 1e9
        v_tok = cur["virtual_token_reserves"] / 1e6
        pp_sol = t["pp_v_sol"] or 0
        pp_tok = t["pp_v_tokens"] or 0
        diff = abs(v_sol - pp_sol) / pp_sol * 100 if pp_sol else float("nan")
        tok_ok = abs(v_tok - pp_tok) / pp_tok * 100 if pp_tok else float("nan")
        print(
            f"{(t['symbol'] or '?')[:10]:>10} {v_sol:12.4f} {pp_sol:12.4f} {diff:7.2f}% {tok_ok:11.2f}%"
        )
        t["decoded"] = cur
        t["owner_program"] = acc["owner"]
        ok.append(t)
    return ok


async def watch(tokens: list[dict]) -> dict:
    """accountSubscribe to the curve accounts and count price ticks."""
    by_sub: dict[int, dict] = {}
    id_to_tok = {i: t for i, t in enumerate(tokens, start=1)}
    ticks: dict[str, list] = {t["mint"]: [] for t in tokens}
    notifications = 0
    t0 = time.time()

    async with websockets.connect(RPC_WS, ping_interval=20, max_size=2**23) as ws:
        for i, t in id_to_tok.items():
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": i,
                        "method": "accountSubscribe",
                        "params": [t["curve"], {"encoding": "base64", "commitment": "processed"}],
                    }
                )
            )
            await asyncio.sleep(0.05)
        print(f"\nwatching {len(tokens)} bonding curves for {WATCH_SECS}s")

        while time.time() - t0 < WATCH_SECS:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            except asyncio.TimeoutError:
                print(f"  [{time.time() - t0:5.0f}s] quiet")
                continue
            except Exception as e:
                print(f"  ws error: {e}")
                break
            if "result" in m and isinstance(m["result"], int):
                by_sub[m["result"]] = id_to_tok[m["id"]]
                continue
            if m.get("method") != "accountNotification":
                continue
            tok = by_sub.get(m["params"]["subscription"])
            if not tok:
                continue
            cur = decode_curve(base64.b64decode(m["params"]["result"]["value"]["data"][0]))
            if not cur:
                continue
            notifications += 1
            ticks[tok["mint"]].append((time.time() - t0, cur["price_sol"], cur["complete"]))

    elapsed = time.time() - t0
    active = {k: v for k, v in ticks.items() if v}
    print(f"\nnotifications: {notifications} in {elapsed:.0f}s")
    print(f"curves that traded: {len(active)}/{len(tokens)}")
    sym = {t["mint"]: t["symbol"] for t in tokens}
    for mint, series in sorted(active.items(), key=lambda kv: -len(kv[1]))[:10]:
        ps = [p for _, p, _ in series]
        swing = (max(ps) - min(ps)) / max(ps) * 100
        done = any(c for _, _, c in series)
        print(
            f"  {(sym[mint] or '?')[:10]:>10} ticks={len(series):4} "
            f"({len(series) / elapsed * 60:5.1f}/min) swing={swing:6.2f}%"
            f"{'  MIGRATED mid-run' if done else ''}"
        )
    return {"elapsed": elapsed, "notifications": notifications, "active": len(active), "ticks": ticks}


async def main() -> None:
    tokens = await harvest()
    if not tokens:
        print("no fresh mints harvested; aborting")
        return
    verified = await verify_layout(tokens)
    if not verified:
        print("layout verification failed for every token; aborting")
        return
    result = await watch(verified)
    (HERE / "data").mkdir(exist_ok=True)
    (HERE / "data" / "bonding.json").write_text(
        json.dumps({"tokens": [{k: v for k, v in t.items()} for t in verified], "watch": result}, indent=2, default=str)
    )
    print("\nwrote spike/data/bonding.json")


if __name__ == "__main__":
    asyncio.run(main())
