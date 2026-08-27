"""Work out how to price a token from chain state.

Two shapes are supported, deliberately scoped to pump.fun:

  bonding curve - one account holding virtual reserves. Atomic, so no slot
                  grouping needed, and half the subscription cost of a pool.
  PumpSwap pool - two vault accounts whose ratio is the price.

Tier is keyed on the pool's **owning program**, never DexScreener's `dexId`:
MEMEDEX's dexId changed from `meteoradbc` to `meteora` mid-watch when it
graduated, so the label is not a stable identifier. Other constant-product
venues are pricable the same way but are intentionally out of scope.

Resolution is cached in the database - without that, every restart re-resolves
every token over HTTP.
"""
from __future__ import annotations

import base64
import logging
import struct
from dataclasses import dataclass

from ..solana import bonding_curve_address
from .solana_rpc import WSOL_MINT, RpcError, SolanaRpc, parse_token_account

log = logging.getLogger(__name__)

PUMP_CURVE_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

#: Programs we can price. Anything else stays on DexScreener polling.
SUPPORTED_PROGRAMS = frozenset({PUMP_CURVE_PROGRAM, PUMPSWAP_PROGRAM})

#: Known vault offsets, checked before falling back to a full scan.
#: All 11 PumpSwap pools sampled in Phase 0 used 139/171.
KNOWN_VAULT_OFFSETS = {PUMPSWAP_PROGRAM: (139, 171)}

CURVE = "curve"
POOL = "pool"


@dataclass(frozen=True)
class PoolRef:
    """Everything needed to price a token from chain state."""

    mint: str
    kind: str  # CURVE or POOL
    program: str
    accounts: tuple[str, ...]  # curve account, or (base_vault, quote_vault)
    decimals: int
    base_amount: int = 0
    quote_amount: int = 0

    @property
    def is_curve(self) -> bool:
        return self.kind == CURVE

    def to_row(self) -> dict:
        return {
            "mint": self.mint,
            "kind": self.kind,
            "program": self.program,
            "accounts": list(self.accounts),
            "decimals": self.decimals,
        }

    @classmethod
    def from_row(cls, row: dict) -> "PoolRef":
        return cls(
            mint=row["mint"],
            kind=row["kind"],
            program=row["program"],
            accounts=tuple(row["accounts"]),
            decimals=row["decimals"],
        )


def curve_price(data: bytes, decimals: int = 6) -> float | None:
    """Price from a pump.fun bonding curve account.

    Layout: 8-byte anchor discriminator, then virtual_token_reserves,
    virtual_sol_reserves, real_token_reserves, real_sol_reserves,
    token_total_supply as u64LE, then a `complete` flag.
    """
    if len(data) < 48:
        return None
    virtual_tokens, virtual_sol = struct.unpack_from("<2Q", data, 8)
    if not virtual_tokens:
        return None
    return (virtual_sol / 1e9) / (virtual_tokens / 10**decimals)


def curve_complete(data: bytes) -> bool:
    """True once the curve has graduated and trading has moved to a pool."""
    return bool(data[48]) if len(data) > 48 else False


def pool_price(base_amount: int, quote_amount: int, decimals: int) -> float | None:
    if not base_amount:
        return None
    return (quote_amount / 1e9) / (base_amount / 10**decimals)


class PoolResolver:
    def __init__(self, rpc: SolanaRpc):
        self.rpc = rpc

    async def _vaults_at_known_offsets(
        self, pool_data: bytes, program: str, mint: str
    ) -> tuple[str, str, int, int] | None:
        from ..solana import b58encode

        offsets = KNOWN_VAULT_OFFSETS.get(program)
        if not offsets or len(pool_data) < max(offsets) + 32:
            return None
        base_pk = b58encode(pool_data[offsets[0] : offsets[0] + 32])
        quote_pk = b58encode(pool_data[offsets[1] : offsets[1] + 32])
        accounts = await self.rpc.get_accounts([base_pk, quote_pk])
        base = parse_token_account(accounts[0] if accounts else None)
        quote = parse_token_account(accounts[1] if len(accounts) > 1 else None)
        if not base or not quote or base[0] != mint or quote[0] != WSOL_MINT:
            return None
        return base_pk, quote_pk, base[1], quote[1]

    async def _scan_for_vaults(
        self, pool_data: bytes, mint: str
    ) -> tuple[str, str, int, int] | None:
        """Fallback: treat every 32-byte window as a candidate pubkey."""
        from ..solana import b58encode

        seen: dict[str, int] = {}
        for off in range(0, max(0, len(pool_data) - 31)):
            pk = b58encode(pool_data[off : off + 32])
            if 32 <= len(pk) <= 44:
                seen.setdefault(pk, off)

        keys = list(seen)
        base = quote = None
        accounts = await self.rpc.get_accounts(keys)
        for pk, account in zip(keys, accounts):
            parsed = parse_token_account(account)
            if not parsed:
                continue
            acct_mint, amount = parsed
            if acct_mint == mint and amount > 0 and base is None:
                base = (pk, amount)
            elif acct_mint == WSOL_MINT and amount > 0 and quote is None:
                quote = (pk, amount)
        if not (base and quote):
            return None
        return base[0], quote[0], base[1], quote[1]

    async def resolve(self, mint: str, pair_address: str | None = None) -> PoolRef | None:
        """Resolve a token to a chain price source, or None if unsupported.

        Tries the bonding curve first: brand-new launches are not indexed by
        DexScreener for some minutes, so their curve address must be derived
        from the mint rather than looked up.
        """
        try:
            curve = bonding_curve_address(mint)
            account = await self.rpc.get_account(curve)
        except (RpcError, ValueError) as e:
            log.debug("curve lookup failed for %s: %s", mint[:8], e)
            account = None

        if account and account.get("owner") == PUMP_CURVE_PROGRAM:
            data = base64.b64decode(account["data"][0])
            if not curve_complete(data):
                decimals = await self.rpc.get_mint_decimals(mint) or 6
                return PoolRef(
                    mint=mint, kind=CURVE, program=PUMP_CURVE_PROGRAM,
                    accounts=(curve,), decimals=decimals,
                )
            log.debug("%s curve complete, using pool", mint[:8])

        if not pair_address:
            return None

        try:
            pool = await self.rpc.get_account(pair_address)
        except RpcError as e:
            log.debug("pool lookup failed for %s: %s", pair_address[:8], e)
            return None
        if not pool:
            return None

        program = pool.get("owner")
        if program not in SUPPORTED_PROGRAMS:
            log.debug("%s on unsupported program %s", mint[:8], program)
            return None

        pool_data = base64.b64decode(pool["data"][0])
        found = await self._vaults_at_known_offsets(pool_data, program, mint)
        if not found:
            found = await self._scan_for_vaults(pool_data, mint)
        if not found:
            log.info("no vaults resolved for %s", mint[:8])
            return None

        base_pk, quote_pk, base_amount, quote_amount = found
        decimals = await self.rpc.get_mint_decimals(mint) or 6
        return PoolRef(
            mint=mint, kind=POOL, program=program, accounts=(base_pk, quote_pk),
            decimals=decimals, base_amount=base_amount, quote_amount=quote_amount,
        )
