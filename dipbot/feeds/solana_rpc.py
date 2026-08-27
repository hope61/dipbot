"""Solana JSON-RPC over HTTP.

Used only for low-volume work: resolving a pool's vaults and reading mint
decimals. The high-volume price stream is a WebSocket (see rpc_feed) pointed at
the public endpoint, which keeps Helius credit usage near zero.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import struct

import httpx

log = logging.getLogger(__name__)

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_PROGRAMS = frozenset({TOKEN_PROGRAM, TOKEN_2022_PROGRAM})
WSOL_MINT = "So11111111111111111111111111111111111111112"


class RpcError(RuntimeError):
    pass


def parse_token_account(account: dict | None) -> tuple[str, int] | None:
    """Return (mint, raw_amount) for an SPL or Token-2022 account.

    Token-2022 accounts carry extensions and so run longer than 165 bytes, but
    both layouts keep the mint at offset 0 and the amount as u64LE at offset 64.
    """
    from ..solana import b58encode

    if not account or account.get("owner") not in TOKEN_PROGRAMS:
        return None
    data = base64.b64decode(account["data"][0])
    if len(data) < 72:
        return None
    return b58encode(data[0:32]), struct.unpack_from("<Q", data, 64)[0]


class SolanaRpc:
    def __init__(self, url: str, timeout: float = 25.0):
        self.url = url
        self._client = httpx.AsyncClient(timeout=timeout)
        self.request_count = 0
        self.error_count = 0
        self.last_error: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, params: list, tries: int = 5):
        delay = 1.0
        for attempt in range(tries):
            try:
                r = await self._client.post(
                    self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                )
                self.request_count += 1
                # The public endpoint rate-limits HTTP hard; a provider key avoids this.
                if r.status_code == 429 or r.status_code >= 500:
                    raise RpcError(f"HTTP {r.status_code}")
                r.raise_for_status()
                body = r.json()
                if "error" in body:
                    raise RpcError(str(body["error"])[:200])
                return body["result"]
            except Exception as e:
                self.error_count += 1
                self.last_error = f"{type(e).__name__}: {e}"
                if attempt == tries - 1:
                    raise RpcError(self.last_error) from e
                await asyncio.sleep(delay)
                delay = min(delay * 2, 20)

    async def get_account(self, address: str) -> dict | None:
        result = await self.call("getAccountInfo", [address, {"encoding": "base64"}])
        return (result or {}).get("value")

    async def get_accounts(self, addresses: list[str]) -> list[dict | None]:
        out: list[dict | None] = []
        for i in range(0, len(addresses), 100):
            result = await self.call(
                "getMultipleAccounts", [addresses[i : i + 100], {"encoding": "base64"}]
            )
            out += (result or {}).get("value") or []
            if i + 100 < len(addresses):
                await asyncio.sleep(0.2)
        return out

    async def get_mint_decimals(self, mint: str) -> int | None:
        account = await self.get_account(mint)
        if not account:
            return None
        data = base64.b64decode(account["data"][0])
        return data[44] if len(data) > 44 else None
