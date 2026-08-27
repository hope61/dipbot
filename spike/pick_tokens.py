"""Pick active pump.fun-ecosystem tokens from DexScreener for the Phase 0 spike.

Mint sources: token-boosts (top+latest) and token-profiles (latest). The /search
endpoint proved near-useless for this (1 pump-ecosystem hit across 6 queries).
Mints are then resolved in batches via /tokens/v1/solana/{addrs}, which also
tells us the real batch-size limit.

Splits results into bonding-curve (dexId=pump) and migrated (dexId=pumpswap) so
the spike can measure PumpPortal coverage separately for each.

Writes spike/tokens.json.
"""
import asyncio
import json
import pathlib
import time

import httpx

# DexScreener 403s the default python UA.
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

BASE = "https://api.dexscreener.com"
MINT_SOURCES = [
    "/token-boosts/top/v1",
    "/token-boosts/latest/v1",
    "/token-profiles/latest/v1",
]
BATCH = 30  # documented-ish limit; verified empirically below

OUT = pathlib.Path(__file__).parent / "tokens.json"


def m5_txns(pair: dict) -> int:
    t = pair.get("txns", {}).get("m5") or {}
    return (t.get("buys") or 0) + (t.get("sells") or 0)


async def collect_mints(client: httpx.AsyncClient) -> list[str]:
    mints: list[str] = []
    for path in MINT_SOURCES:
        try:
            r = await client.get(BASE + path)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  {path} failed: {e}")
            continue
        found = [
            d["tokenAddress"]
            for d in data
            if d.get("chainId") == "solana" and d.get("tokenAddress")
        ]
        mints += found
        print(f"  {path}: {len(found)} solana mints")
        await asyncio.sleep(0.3)
    return list(dict.fromkeys(mints))


async def resolve(client: httpx.AsyncClient, mints: list[str]) -> list[dict]:
    pairs: list[dict] = []
    for i in range(0, len(mints), BATCH):
        chunk = mints[i : i + BATCH]
        try:
            r = await client.get(f"{BASE}/tokens/v1/solana/{','.join(chunk)}")
            r.raise_for_status()
            got = r.json()
        except Exception as e:
            print(f"  batch {i // BATCH}: failed ({e})")
            continue
        print(f"  batch {i // BATCH}: {len(chunk)} mints -> {len(got)} pairs")
        pairs += got
        await asyncio.sleep(0.3)
    return pairs


async def main() -> None:
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=25) as client:
        print("collecting mints...")
        mints = await collect_mints(client)
        print(f"  unique solana mints: {len(mints)}\n")
        print("resolving pairs...")
        pairs = await resolve(client, mints)

    best: dict[str, dict] = {}
    for p in pairs:
        if p.get("chainId") != "solana" or p.get("dexId") not in ("pump", "pumpswap"):
            continue
        mint = p["baseToken"]["address"]
        if m5_txns(p) > m5_txns(best.get(mint, {})):
            best[mint] = p

    rows = [
        {
            "mint": mint,
            "symbol": p["baseToken"].get("symbol"),
            "dex_id": p["dexId"],
            "tier": "migrated" if p["dexId"] == "pumpswap" else "bonding",
            "pair_address": p["pairAddress"],
            "price_usd": float(p["priceUsd"]) if p.get("priceUsd") else None,
            "liquidity_usd": (p.get("liquidity") or {}).get("usd"),
            "market_cap": p.get("marketCap"),
            "m5_txns": m5_txns(p),
            "vol_m5": (p.get("volume") or {}).get("m5"),
            "pair_created_at": p.get("pairCreatedAt"),
            "snapshot_ts": time.time(),
        }
        for mint, p in best.items()
    ]
    rows.sort(key=lambda r: -r["m5_txns"])

    bonding = [r for r in rows if r["tier"] == "bonding"][:15]
    migrated = [r for r in rows if r["tier"] == "migrated"][:15]
    picked = bonding + migrated
    OUT.write_text(json.dumps(picked, indent=2))

    print(f"\npump-ecosystem tokens: {len(rows)}")
    print(f"picked {len(bonding)} bonding + {len(migrated)} migrated -> {OUT.name}\n")
    for r in picked:
        print(
            f"  {r['tier']:9} {(r['symbol'] or '?')[:12]:12} m5_txns={r['m5_txns']:4} "
            f"liq=${(r['liquidity_usd'] or 0):>12,.0f}  {r['mint']}"
        )
    active = [r for r in picked if r["m5_txns"] > 0]
    print(f"\n{len(active)}/{len(picked)} picked tokens traded in the last 5m")
    if not bonding:
        print("WARNING: no bonding-curve tokens; will supplement from PumpPortal newToken.")
    if not migrated:
        print("WARNING: no migrated tokens; the key risk stays untested.")


if __name__ == "__main__":
    asyncio.run(main())
