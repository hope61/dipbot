"""End-to-end check of the real PoolResolver against live Raydium pools.

Everything else in this change was validated with a standalone reimplementation
of the vault scan. This runs the actual production code path - SolanaRpc,
PoolResolver.resolve, pool_price - and compares its output against
DexScreener's priceNative for the same pair.
"""
import asyncio
import httpx

from dipbot.feeds.pool_resolver import (
    POOL, RAYDIUM_CPMM_PROGRAM, RAYDIUM_V4_PROGRAM, PoolResolver, pool_price,
)
from dipbot.feeds.solana_rpc import SolanaRpc

RPC = "https://api.mainnet-beta.solana.com"
DEX = "https://api.dexscreener.com"
WSOL = "So11111111111111111111111111111111111111112"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
QUERIES = ["new", "cat", "dog", "pepe", "inu", "moon", "gold", "boop"]
WANT = {RAYDIUM_V4_PROGRAM: "Raydium AMM v4", RAYDIUM_CPMM_PROGRAM: "Raydium CPMM"}


async def main() -> None:
    rpc = SolanaRpc(RPC)
    resolver = PoolResolver(rpc)
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=40) as c:
        pairs = {}
        for q in QUERIES:
            try:
                r = await c.get(f"{DEX}/latest/dex/search", params={"q": q})
                for p in (r.json().get("pairs") or []):
                    if (p.get("chainId") == "solana"
                            and (p.get("quoteToken") or {}).get("address") == WSOL
                            and (p.get("liquidity") or {}).get("usd", 0) > 30_000):
                        pairs[p["pairAddress"]] = p
            except Exception:
                pass
            await asyncio.sleep(0.3)

        addrs = list(pairs)
        owners = {}
        for i in range(0, len(addrs), 100):
            for a, acc in zip(addrs[i:i+100], await rpc.get_accounts(addrs[i:i+100])):
                if acc:
                    owners[a] = acc["owner"]
            await asyncio.sleep(0.2)

        checked = {k: 0 for k in WANT}
        errors = {k: [] for k in WANT}
        for addr, program in owners.items():
            if program not in WANT or checked[program] >= 4:
                continue
            p = pairs[addr]
            mint = p["baseToken"]["address"]
            before = rpc.request_count
            ref_obj = await resolver.resolve(mint, addr)
            calls = rpc.request_count - before
            if ref_obj is None:
                print(f"  {WANT[program]:>15} {p['baseToken']['symbol'][:9]:>9}: DID NOT RESOLVE")
                continue
            checked[program] += 1
            assert ref_obj.kind == POOL and ref_obj.program == program
            price = pool_price(ref_obj.base_amount, ref_obj.quote_amount, ref_obj.decimals)
            ref = float(p["priceNative"])
            err = (price - ref) / ref * 100
            errors[program].append(err)
            print(f"  {WANT[program]:>15} {p['baseToken']['symbol'][:9]:>9}: "
                  f"dec={ref_obj.decimals} rpc_calls={calls} "
                  f"price={price:.6e} ref={ref:.6e} err={err:+6.2f}%")
            await asyncio.sleep(0.3)

        print()
        for prog, name in WANT.items():
            e = errors[prog]
            if e:
                print(f"{name}: {len(e)} pools resolved, err {min(e):+.2f}%..{max(e):+.2f}%")
            else:
                print(f"{name}: no live pools sampled")
    await rpc.close()


if __name__ == "__main__":
    asyncio.run(main())
