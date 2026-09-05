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
    KNOWN_VAULT_OFFSETS,
    POOL,
    PUMPSWAP_PROGRAM,
    PUMP_CURVE_PROGRAM,
    RAYDIUM_CPMM_PROGRAM,
    RAYDIUM_V4_PROGRAM,
    SUPPORTED_PROGRAMS,
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

#: Concentrated-liquidity programs. Their pools contain two token vaults that
#: the scan finds happily, but whose ratio is 36-96% away from spot - the
#: numbers are in the pool_resolver docstring. Nothing may price these.
RAYDIUM_CLMM_PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
ORCA_WHIRLPOOL_PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
METEORA_DLMM_PROGRAM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"


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


def offset_pool(program: str, size: int, first: str, second: str, owner: str | None = None) -> dict:
    """A pool account carrying two vault pubkeys at `program`'s known offsets.

    `first`/`second` are written in offset order, so a test can put the token
    vault in either slot - which is what real Raydium pools do.
    """
    lo, hi = KNOWN_VAULT_OFFSETS[program]
    raw = bytearray(size)
    raw[lo : lo + 32] = b58decode(first)
    raw[hi : hi + 32] = b58decode(second)
    return {"owner": owner or program, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}


def raydium_v4_pool(token_vault_first: bool = True, **kw) -> dict:
    """LIQUIDITY_STATE_LAYOUT_V4 is 752 bytes; vaults at 336/368."""
    a, b = (BASE_VAULT, QUOTE_VAULT) if token_vault_first else (QUOTE_VAULT, BASE_VAULT)
    return offset_pool(RAYDIUM_V4_PROGRAM, 752, a, b, **kw)


def raydium_cpmm_pool(token_vault_first: bool = True, **kw) -> dict:
    """CPMM PoolState is 637 bytes; token_0/token_1 vaults at 72/104."""
    a, b = (BASE_VAULT, QUOTE_VAULT) if token_vault_first else (QUOTE_VAULT, BASE_VAULT)
    return offset_pool(RAYDIUM_CPMM_PROGRAM, 637, a, b, **kw)


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


async def test_swapped_vaults_resolve_without_scanning():
    """The known offsets hold the mints the other way round.

    Roles come from the mints, so the fast path must still resolve it rather
    than falling through to the ~700-key scan.
    """
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
    # one pool read + one batched vault read + decimals; a scan would be many more
    assert rpc.request_count <= 4


async def test_curve_on_foreign_program_is_ignored():
    curve = bonding_curve_address(MINT)
    rpc = FakeRpc({curve: curve_account(owner="NotPumpProgram1111"), MINT: mint_account()})
    assert await PoolResolver(rpc).resolve(MINT) is None


# --- Raydium ----------------------------------------------------------------
# Added after sampling live pools: v4 priced within -0.25%..+0.30% over 12
# pools, CPMM +0.00%..+0.36% over 7. Vault offsets were confirmed against the
# mint fields in each layout, not just by finding token accounts there.


@pytest.mark.parametrize("token_vault_first", [True, False])
async def test_resolves_raydium_v4_pool_in_either_vault_order(token_vault_first):
    """WSOL is the pool's baseMint in most sampled SOL pairs and the quoteMint
    in the rest, so both orders have to work on the fast path."""
    first, second = (
        (token_account(MINT, 500), token_account(WSOL_MINT, 900))
        if token_vault_first
        else (token_account(WSOL_MINT, 900), token_account(MINT, 500))
    )
    rpc = FakeRpc({
        PAIR: raydium_v4_pool(token_vault_first=token_vault_first),
        BASE_VAULT: first,
        QUOTE_VAULT: second,
        MINT: mint_account(),
    })
    ref = await PoolResolver(rpc).resolve(MINT, PAIR)
    assert ref is not None
    assert ref.kind == POOL
    assert ref.program == RAYDIUM_V4_PROGRAM
    assert ref.base_amount == 500
    assert ref.quote_amount == 900
    assert rpc.request_count <= 4          # fast path, not the full scan


@pytest.mark.parametrize("token_vault_first", [True, False])
async def test_resolves_raydium_cpmm_pool_in_either_vault_order(token_vault_first):
    first, second = (
        (token_account(MINT, 250), token_account(WSOL_MINT, 400))
        if token_vault_first
        else (token_account(WSOL_MINT, 400), token_account(MINT, 250))
    )
    rpc = FakeRpc({
        PAIR: raydium_cpmm_pool(token_vault_first=token_vault_first),
        BASE_VAULT: first,
        QUOTE_VAULT: second,
        MINT: mint_account(),
    })
    ref = await PoolResolver(rpc).resolve(MINT, PAIR)
    assert ref is not None
    assert ref.program == RAYDIUM_CPMM_PROGRAM
    assert ref.base_amount == 250
    assert ref.quote_amount == 400
    assert rpc.request_count <= 4


async def test_raydium_vaults_are_ordered_token_then_sol_whatever_the_layout():
    """PoolRef.accounts feeds the feed's price maths, which assumes that order."""
    rpc = FakeRpc({
        PAIR: raydium_v4_pool(token_vault_first=False),
        BASE_VAULT: token_account(WSOL_MINT, 900),
        QUOTE_VAULT: token_account(MINT, 500),
        MINT: mint_account(),
    })
    ref = await PoolResolver(rpc).resolve(MINT, PAIR)
    assert ref.accounts == (QUOTE_VAULT, BASE_VAULT)   # token vault first


async def test_raydium_pool_with_a_non_sol_quote_is_rejected():
    """ZCAT/ZEC was the coin that started this: a pool quoted in something
    other than SOL cannot be priced, since pool_price divides by 1e9."""
    other_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    rpc = FakeRpc({
        PAIR: raydium_v4_pool(),
        BASE_VAULT: token_account(MINT, 500),
        QUOTE_VAULT: token_account(other_mint, 900),
        MINT: mint_account(),
    })
    assert await PoolResolver(rpc).resolve(MINT, PAIR) is None


# --- concentrated liquidity must never be priced this way -------------------


@pytest.mark.parametrize(
    "program",
    [RAYDIUM_CLMM_PROGRAM, ORCA_WHIRLPOOL_PROGRAM, METEORA_DLMM_PROGRAM],
)
async def test_concentrated_liquidity_programs_are_rejected(program):
    """These pools DO contain two findable token vaults - that is the trap.

    Sampled live, their vault ratio was 36-96% away from spot, because the
    balances span every price range rather than the current one. The allowlist
    is the only thing stopping the scan from pricing them, so it is a
    regression test, not a formality.
    """
    rpc = FakeRpc({
        PAIR: pumpswap_pool(owner=program),
        BASE_VAULT: token_account(MINT, 500),
        QUOTE_VAULT: token_account(WSOL_MINT, 900),
        MINT: mint_account(),
    })
    assert await PoolResolver(rpc).resolve(MINT, PAIR) is None
    assert program not in SUPPORTED_PROGRAMS


def test_every_supported_pool_program_has_known_offsets():
    """A program added without offsets still works, but pays the ~700-key scan
    on every resolve; that is a mistake worth catching at test time."""
    pool_programs = SUPPORTED_PROGRAMS - {PUMP_CURVE_PROGRAM}
    assert pool_programs == set(KNOWN_VAULT_OFFSETS)
