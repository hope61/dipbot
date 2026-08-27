"""Discover the two token vaults backing each pool, without per-DEX IDL knowledge.

Method: read the pool account, treat every 32-byte window as a candidate pubkey,
fetch them all, and keep the ones that are SPL Token / Token-2022 accounts whose
mint is the pair's base or quote mint. Works for any AMM whose pool account
references its vaults, so it should generalise past PumpSwap to Raydium/Meteora.

Writes spike/vaults.json.
"""
import asyncio
import base64
import json
import pathlib
import struct

import httpx

RPC = "https://api.mainnet-beta.solana.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}
WSOL = "So11111111111111111111111111111111111111112"
HERE = pathlib.Path(__file__).parent
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + s


async def rpc(client: httpx.AsyncClient, method: str, params: list, tries: int = 5):
    """POST a JSON-RPC call, backing off on the public endpoint's 429s."""
    delay = 1.0
    for attempt in range(tries):
        r = await client.post(
            RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
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
    raise RuntimeError(f"{method}: rate limited after {tries} attempts")


def parse_token_account(acc: dict) -> tuple[str, int] | None:
    """Return (mint, raw_amount) if this is an SPL/Token-2022 account."""
    if not acc or acc["owner"] not in TOKEN_PROGRAMS:
        return None
    d = base64.b64decode(acc["data"][0])
    if len(d) < 72:
        return None
    return b58(d[0:32]), struct.unpack_from("<Q", d, 64)[0]


async def discover(client: httpx.AsyncClient, pair: str, base_mint: str) -> dict | None:
    pool = await rpc(client, "getAccountInfo", [pair, {"encoding": "base64"}])
    if not pool or not pool.get("value"):
        return None
    data = base64.b64decode(pool["value"]["data"][0])

    seen: dict[str, int] = {}
    for off in range(0, max(0, len(data) - 31)):
        pk = b58(data[off : off + 32])
        if 32 <= len(pk) <= 44:
            seen.setdefault(pk, off)

    keys = list(seen)
    base_vault = quote_vault = None
    for i in range(0, len(keys), 100):
        chunk = keys[i : i + 100]
        accs = (await rpc(client, "getMultipleAccounts", [chunk, {"encoding": "base64"}]))["value"]
        for pk, acc in zip(chunk, accs):
            parsed = parse_token_account(acc)
            if not parsed:
                continue
            mint, amount = parsed
            if mint == base_mint and amount > 0 and base_vault is None:
                base_vault = {"pubkey": pk, "offset": seen[pk], "mint": mint, "amount": amount}
            elif mint == WSOL and amount > 0 and quote_vault is None:
                quote_vault = {"pubkey": pk, "offset": seen[pk], "mint": mint, "amount": amount}
        await asyncio.sleep(0.15)

    if not (base_vault and quote_vault):
        return None
    return {"base_vault": base_vault, "quote_vault": quote_vault, "pool_program": pool["value"]["owner"]}


async def main() -> None:
    tokens = json.loads((HERE / "tokens.json").read_text())
    out = []
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=30) as client:
        for t in tokens:
            try:
                found = await discover(client, t["pair_address"], t["mint"])
            except Exception as e:
                print(f"  {t['symbol']:>10}: error {e}")
                continue
            if not found:
                print(f"  {t['symbol']:>10}: vaults NOT found")
                continue
            # base decimals from the mint account
            mint_acc = (await rpc(client, "getAccountInfo", [t["mint"], {"encoding": "base64"}]))["value"]
            decimals = base64.b64decode(mint_acc["data"][0])[44]
            row = {**t, **found, "base_decimals": decimals}
            out.append(row)
            price = (found["quote_vault"]["amount"] / 1e9) / (
                found["base_vault"]["amount"] / 10**decimals
            )
            print(
                f"  {t['symbol']:>10}: base@{found['base_vault']['offset']:3} "
                f"quote@{found['quote_vault']['offset']:3} dec={decimals} spot={price:.4e} SOL"
            )
            await asyncio.sleep(0.2)

    (HERE / "vaults.json").write_text(json.dumps(out, indent=2))
    print(f"\nresolved {len(out)}/{len(tokens)} pools -> vaults.json")


if __name__ == "__main__":
    asyncio.run(main())
