"""Can the generic vault scan price non-pump.fun constant-product pools?

Method is spike/vaults.py's, widened: sample real SOL-quoted pools across many
DEX programs, find their vaults by scanning the pool account for 32-byte
windows that resolve to token accounts, then check the implied spot price
against DexScreener's priceNative.

A program is safe to add to SUPPORTED_PROGRAMS only if every sampled pool
resolves AND prices within a percent or so. A program whose pools resolve but
misprice is concentrated-liquidity or vault-indirected, and must stay out.

Writes spike/other_dex_vaults.json.
"""
import asyncio
import base64
import json
import pathlib
import struct
from collections import defaultdict

import httpx

RPC = "https://api.mainnet-beta.solana.com"
DEX = "https://api.dexscreener.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}
WSOL = "So11111111111111111111111111111111111111112"
HERE = pathlib.Path(__file__).parent
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

#: Liquid coins that trade on many venues at once, so one lookup each yields
#: pools across Raydium/Orca/Meteora without hunting per DEX.
SEEDS = [
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",   # BONK
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",   # WIF
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",    # JUP
    "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",   # POPCAT
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",   # RAY
    "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",    # MEW
    "ukHH6c7mMyiWCf1b9pnWe25TSpkDDt3H5pQZgZ74J82",    # BOME
    "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",   # FWOG
]
MAX_POOLS_PER_PROGRAM = 3
MIN_LIQ_USD = 40_000


def b58(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + s


async def rpc(client, method, params, tries=6):
    delay = 1.0
    for _ in range(tries):
        r = await client.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if r.status_code == 429:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 20)
            continue
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(body["error"])
        return body["result"]
    raise RuntimeError(f"{method}: rate limited")


def parse_token_account(acc):
    if not acc or acc.get("owner") not in TOKEN_PROGRAMS:
        return None
    d = base64.b64decode(acc["data"][0])
    if len(d) < 72:
        return None
    return b58(d[0:32]), struct.unpack_from("<Q", d, 64)[0]


async def scan_vaults(client, pool_data: bytes, base_mint: str):
    """spike/vaults.py's method: every 32-byte window is a candidate pubkey."""
    seen = {}
    for off in range(0, max(0, len(pool_data) - 31)):
        pk = b58(pool_data[off : off + 32])
        if 32 <= len(pk) <= 44:
            seen.setdefault(pk, off)

    keys = list(seen)
    base = quote = None
    for i in range(0, len(keys), 100):
        chunk = keys[i : i + 100]
        accs = (await rpc(client, "getMultipleAccounts", [chunk, {"encoding": "base64"}]))["value"]
        for pk, acc in zip(chunk, accs):
            parsed = parse_token_account(acc)
            if not parsed:
                continue
            mint, amount = parsed
            if mint == base_mint and amount > 0 and base is None:
                base = (pk, seen[pk], amount)
            elif mint == WSOL and amount > 0 and quote is None:
                quote = (pk, seen[pk], amount)
        await asyncio.sleep(0.15)
    return base, quote


async def main():
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=40) as client:
        # 1. Collect candidate SOL-quoted pools, grouped by owning program.
        by_program = defaultdict(list)
        for mint in SEEDS:
            try:
                r = await client.get(f"{DEX}/latest/dex/tokens/{mint}")
                pairs = r.json().get("pairs") or []
            except Exception as e:
                print(f"  dexscreener failed for {mint[:8]}: {e}")
                continue
            for p in pairs:
                if p.get("chainId") != "solana":
                    continue
                if (p.get("quoteToken") or {}).get("address") != WSOL:
                    continue
                if (p.get("liquidity") or {}).get("usd", 0) < MIN_LIQ_USD:
                    continue
                by_program["?"].append(p)
            await asyncio.sleep(0.3)

        # 2. Resolve each pool's owning program (batched).
        pools = by_program.pop("?")
        addrs = list({p["pairAddress"] for p in pools})
        owners = {}
        for i in range(0, len(addrs), 100):
            chunk = addrs[i : i + 100]
            accs = (await rpc(client, "getMultipleAccounts", [chunk, {"encoding": "base64"}]))["value"]
            for a, acc in zip(chunk, accs):
                if acc:
                    owners[a] = acc["owner"]
            await asyncio.sleep(0.2)

        grouped = defaultdict(list)
        for p in pools:
            owner = owners.get(p["pairAddress"])
            if owner:
                grouped[owner].append(p)

        print(f"\n{len(pools)} SOL-quoted pools across {len(grouped)} programs\n")
        results = []
        for program, plist in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
            labels = {(p.get("labels") or ["-"])[0] for p in plist}
            dexids = {p["dexId"] for p in plist}
            print(f"{program}  ({', '.join(sorted(dexids))} {sorted(labels)}, {len(plist)} pools)")
            seen_pairs = set()
            for p in plist:
                if len(seen_pairs) >= MAX_POOLS_PER_PROGRAM:
                    break
                if p["pairAddress"] in seen_pairs:
                    continue
                seen_pairs.add(p["pairAddress"])
                sym = p["baseToken"]["symbol"]
                try:
                    acc = (await rpc(client, "getAccountInfo", [p["pairAddress"], {"encoding": "base64"}]))["value"]
                    data = base64.b64decode(acc["data"][0])
                    base, quote = await scan_vaults(client, data, p["baseToken"]["address"])
                except Exception as e:
                    print(f"    {sym:>8}: error {e}")
                    continue
                if not (base and quote):
                    print(f"    {sym:>8}: vaults NOT found (len={len(data)}) -> cannot price this way")
                    results.append({"program": program, "symbol": sym, "resolved": False})
                    continue
                mint_acc = (await rpc(client, "getAccountInfo", [p["baseToken"]["address"], {"encoding": "base64"}]))["value"]
                dec = base64.b64decode(mint_acc["data"][0])[44]
                spot = (quote[2] / 1e9) / (base[2] / 10**dec)
                ref = float(p["priceNative"])
                err = (spot - ref) / ref * 100 if ref else float("nan")
                flag = "OK " if abs(err) < 2 else "BAD"
                print(f"    {sym:>8}: base@{base[1]:>4} quote@{quote[1]:>4} len={len(data):>4} "
                      f"spot={spot:.6e} ref={ref:.6e} err={err:+7.2f}% {flag}")
                results.append({
                    "program": program, "symbol": sym, "dex_id": p["dexId"],
                    "labels": p.get("labels"), "pair": p["pairAddress"],
                    "data_len": len(data), "base_offset": base[1], "quote_offset": quote[1],
                    "spot": spot, "ref": ref, "err_pct": err, "resolved": True,
                })
                await asyncio.sleep(0.2)
            print()

        (HERE / "other_dex_vaults.json").write_text(json.dumps(results, indent=2))
        print(f"wrote {len(results)} rows -> spike/other_dex_vaults.json")


if __name__ == "__main__":
    asyncio.run(main())
