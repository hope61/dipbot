"""Pool resolution: token-account parsing, program gating, curve-vs-pool choice.

The RPC layer is faked so the decision logic can be asserted without network.
Live behaviour is covered in spike/.
"""
from __future__ import annotations

import base64
import struct

import pytest

from dipbot.feeds.pool_resolver import (
    CURVE,
    POOL,
    PUMPSWAP_PROGRAM,
    PUMP_CURVE_PROGRAM,
    PoolRef,
    PoolResolver,
)
from dipbot.feeds.solana_rpc import (
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    WSOL_MINT,
    parse_token_account,
)
from dipbot.solana import b58decode, b58encode, bonding_curve_address

MINT = "FmsQNEebvJLMcfiKuVuvGRgAQJzpJfyjJ8PHUGnzpump"
PAIR = "HMzvsEEmtzHhvZNw9uwbaG85HCTmFnkbhzUx16cy7ca3"
BASE_VAULT = "6JyR1sXCA8FaykmtokPz5UhG4MqMtXDr6TXB3HMMPtKF"
QUOTE_VAULT = "34bbJYM5Vkjy97DzWuMxhALDbVQwb1fNXHnPDhf42hT4"

MEMEDEX_DAMM_PROGRAM = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"


def token_account(mint: str, amount: int, owner: str = TOKEN_PROGRAM, size: int = 165) -> dict:
    raw = bytearray(size)
    raw[0:32] = b58decode(mint)
    struct.pack_into("<Q", raw, 64, amount)
    return {"owner": owner, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}


def mint_account(decimals: int = 6) -> dict:
    raw = bytearray(82)
    raw[44] = decimals
    return {"owner": TOKEN_PROGRAM, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}


def curve_account(complete: bool = False, owner: str = PUMP_CURVE_PROGRAM) -> dict:
    raw = bytearray(64)
    struct.pack_into("<2Q", raw, 8, 1_000_000_000_000, 30_000_000_000)
    raw[48] = 1 if complete else 0
    return {"owner": owner, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}


def pumpswap_pool(owner: str = PUMPSWAP_PROGRAM) -> dict:
    """Pool account with vault pubkeys at the known 139/171 offsets."""
    raw = bytearray(301)
    raw[139:171] = b58decode(BASE_VAULT)
    raw[171:203] = b58decode(QUOTE_VAULT)
    return {"owner": owner, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}


class FakeRpc:
    def __init__(self, accounts: dict):
        self.accounts = accounts
        self.request_count = 0
        self.error_count = 0

    async def get_account(self, address):
        self.request_count += 1
        return self.accounts.get(address)

    async def get_accounts(self, addresses):
        self.request_count += 1
        return [self.accounts.get(a) for a in addresses]

    async def get_mint_decimals(self, mint):
        self.request_count += 1
        return 6


# --- token account parsing --------------------------------------------------


def test_parse_spl_token_account():
    parsed = parse_token_account(token_account(WSOL_MINT, 12_345))
    assert parsed == (WSOL_MINT, 12_345)


def test_parse_token_2022_account():
    """pump.fun base vaults are Token-2022 and run longer than 165 bytes.

    Using the wrong program id here silently found nothing at all.
    """
    parsed = parse_token_account(token_account(MINT, 999, owner=TOKEN_2022_PROGRAM, size=170))
    assert parsed == (MINT, 999)


def test_parse_rejects_non_token_program():
    assert parse_token_account(token_account(MINT, 1, owner="SomeOtherProgram111")) is None


def test_parse_rejects_none_and_short_data():
    assert parse_token_account(None) is None
    assert parse_token_account({"owner": TOKEN_PROGRAM, "data": ["AAAA", "base64"]}) is None


# --- PoolRef serialisation --------------------------------------------------


def test_pool_ref_round_trip():
    ref = PoolRef(
        mint=MINT, kind=POOL, program=PUMPSWAP_PROGRAM,
        accounts=(BASE_VAULT, QUOTE_VAULT), decimals=6,
    )
    restored = PoolRef.from_row(ref.to_row())
    assert restored == ref
    assert restored.accounts == (BASE_VAULT, QUOTE_VAULT)


def test_pool_ref_is_curve_flag():
    assert PoolRef(MINT, CURVE, "p", ("a",), 6).is_curve
    assert not PoolRef(MINT, POOL, "p", ("a", "b"), 6).is_curve


# --- resolution -------------------------------------------------------------



async def test_resolves_bonding_curve_without_pair_address():
    """Brand-new launches aren't in DexScreener, so no pair address exists."""
    curve = bonding_curve_address(MINT)
    rpc = FakeRpc({curve: curve_account(), MINT: mint_account()})
    ref = await PoolResolver(rpc).resolve(MINT)
    assert ref is not None
    assert ref.kind == CURVE
    assert ref.accounts == (curve,)
    assert ref.program == PUMP_CURVE_PROGRAM


async def test_completed_curve_falls_through_to_pool():
    """After graduation the curve is done; price lives in the pool."""
    curve = bonding_curve_address(MINT)
    rpc = FakeRpc({
        curve: curve_account(complete=True),
        PAIR: pumpswap_pool(),
        BASE_VAULT: token_account(MINT, 1_000_000),
        QUOTE_VAULT: token_account(WSOL_MINT, 2_000_000_000),
        MINT: mint_account(),
    })
    ref = await PoolResolver(rpc).resolve(MINT, PAIR)
    assert ref is not None
    assert ref.kind == POOL
    assert ref.accounts == (BASE_VAULT, QUOTE_VAULT)
    assert ref.base_amount == 1_000_000
    assert ref.quote_amount == 2_000_000_000


async def test_resolves_pumpswap_pool_at_known_offsets():
    rpc = FakeRpc({
        PAIR: pumpswap_pool(),
        BASE_VAULT: token_account(MINT, 500),
        QUOTE_VAULT: token_account(WSOL_MINT, 900),
        MINT: mint_account(),
    })
    ref = await PoolResolver(rpc).resolve(MINT, PAIR)
    assert ref.kind == POOL
    assert ref.decimals == 6


async def test_unsupported_program_returns_none():
    """MEMEDEX's Meteora DAMM v2 pool is pricable but deliberately out of scope."""
    rpc = FakeRpc({PAIR: pumpswap_pool(owner=MEMEDEX_DAMM_PROGRAM), MINT: mint_account()})
    assert await PoolResolver(rpc).resolve(MINT, PAIR) is None


async def test_missing_pool_returns_none():
    rpc = FakeRpc({MINT: mint_account()})
    assert await PoolResolver(rpc).resolve(MINT, PAIR) is None


async def test_no_pair_address_and_no_curve_returns_none():
    rpc = FakeRpc({MINT: mint_account()})
    assert await PoolResolver(rpc).resolve(MINT) is None


async def test_swapped_vaults_fall_back_to_scan_and_still_resolve():
    """If the known offsets hold the mints the other way round, the fast path
    must reject them - but the scan should still find the right pair."""
    rpc = FakeRpc({
        PAIR: pumpswap_pool(),
        BASE_VAULT: token_account(WSOL_MINT, 500),
        QUOTE_VAULT: token_account(MINT, 900),
        MINT: mint_account(),
    })
    ref = await PoolResolver(rpc).resolve(MINT, PAIR)
    assert ref is not None
    # roles assigned by which mint each vault holds, not by offset order
    assert ref.accounts == (QUOTE_VAULT, BASE_VAULT)
    assert ref.base_amount == 900
    assert ref.quote_amount == 500


async def test_curve_on_foreign_program_is_ignored():
    curve = bonding_curve_address(MINT)
    rpc = FakeRpc({curve: curve_account(owner="NotPumpProgram1111"), MINT: mint_account()})
    assert await PoolResolver(rpc).resolve(MINT) is None
