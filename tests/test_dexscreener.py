"""DexScreener parsing, tier assignment, and the cached-empty retry.

No network: the HTTP layer is stubbed so the retry logic can be asserted
deterministically. The live behaviour it models was recorded live via spike/.
"""
from __future__ import annotations

import pytest

from dipbot.feeds.dexscreener import DexScreener, tier_for
from dipbot.models import Tier
from tests.conftest import make_meta

MINT = "Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump"
PAIR = "HMzvsEEmtzHhvZNw9uwbaG85HCTmFnkbhzUx16cy7ca3"


@pytest.mark.parametrize(
    "text, expected",
    [
        (MINT, MINT),
        (PAIR, PAIR),
        (f"https://dexscreener.com/solana/{PAIR}", PAIR),
        (f"https://dexscreener.com/solana/{PAIR}?maker=x", PAIR),
        (f"https://pump.fun/coin/{MINT}", MINT),
        (f"https://pump.fun/{MINT}", MINT),
        (f"  {MINT}  ", MINT),
        ("garbage", None),
        ("", None),
        ("https://example.com/nothing", None),
    ],
)
def test_parse_input(text, expected):
    assert DexScreener.parse_input(text) == expected


def test_to_meta_extracts_fields():
    pair = {
        "chainId": "solana",
        "dexId": "pumpswap",
        "pairAddress": PAIR,
        "baseToken": {"address": MINT, "symbol": "CATE", "name": "Cate"},
        "quoteToken": {"symbol": "SOL"},
        "priceUsd": "0.0679",
        "priceNative": "0.00069",
        "liquidity": {"usd": 2_300_000.0},
        "volume": {"h24": 23_000_000.0, "m5": 15_000.0},
        "marketCap": 65_000_000.0,
        "pairCreatedAt": 1787538398000,
        "txns": {"m5": {"buys": 70, "sells": 79}},
    }
    meta = DexScreener._to_meta(pair)
    assert meta.mint == MINT
    assert meta.symbol == "CATE"
    assert meta.price_usd == pytest.approx(0.0679)
    assert meta.liquidity_usd == pytest.approx(2_300_000.0)
    assert meta.txns_m5_buys == 70


def test_to_meta_tolerates_missing_fields():
    """Meteora DBC pairs arrive with no liquidity block at all."""
    meta = DexScreener._to_meta(
        {
            "chainId": "solana",
            "dexId": "meteoradbc",
            "pairAddress": PAIR,
            "baseToken": {"address": MINT, "symbol": "MEMEDEX"},
            "priceUsd": "18.52",
        }
    )
    assert meta.liquidity_usd is None
    assert meta.volume_h24 is None
    assert meta.price_usd == pytest.approx(18.52)


def test_to_meta_handles_unparseable_numbers():
    meta = DexScreener._to_meta(
        {
            "chainId": "solana",
            "pairAddress": PAIR,
            "baseToken": {"address": MINT},
            "priceUsd": "not-a-number",
        }
    )
    assert meta.price_usd is None


# --- tiers ------------------------------------------------------------------


@pytest.mark.parametrize(
    "dex_id, expected",
    [
        ("pump", Tier.REALTIME),
        ("pumpswap", Tier.REALTIME),
        ("meteora", Tier.POLLED),
        ("meteoradbc", Tier.POLLED),
        ("raydium", Tier.POLLED),
        ("orca", Tier.POLLED),
        (None, Tier.POLLED),
    ],
)
def test_tier_for(dex_id, expected):
    assert tier_for(make_meta(dex_id=dex_id)) is expected


def test_polled_tier_cannot_use_fast_windows():
    """30s cached data cannot support a 10s window - this must never regress."""
    assert Tier.POLLED.allowed_windows == ("3m", "5m")
    assert "10s" not in Tier.POLLED.allowed_windows
    assert "1m" not in Tier.POLLED.allowed_windows
    assert Tier.REALTIME.allowed_windows == ("10s", "1m", "3m", "5m")


def test_tier_badges_differ():
    assert Tier.REALTIME.badge != Tier.POLLED.badge


# --- cached-empty retry -----------------------------------------------------


class FakeDex(DexScreener):
    """Stubs the HTTP layer, recording the params of every call."""

    def __init__(self, responses):
        super().__init__(user_agent="test")
        self._responses = list(responses)
        self.calls: list[dict | None] = []

    async def _get(self, path, params=None, tries=4):
        self.calls.append(params)
        self.request_count += 1
        return self._responses.pop(0) if self._responses else []


@pytest.mark.asyncio
async def test_get_pairs_returns_data_without_retry():
    dex = FakeDex([[{"ok": 1}]])
    assert await dex._get_pairs("/x") == [{"ok": 1}]
    assert len(dex.calls) == 1
    assert dex.empty_retries == 0
    await dex.close()


@pytest.mark.asyncio
async def test_get_pairs_retries_past_cached_empty():
    """A valid mint returned 0 pairs, then full data once cache was bypassed."""
    dex = FakeDex([[], [{"ok": 1}]])
    assert await dex._get_pairs("/x") == [{"ok": 1}]
    assert len(dex.calls) == 2
    assert dex.empty_retries == 1
    assert "_" in dex.calls[1]  # cache-busting param present
    await dex.close()


@pytest.mark.asyncio
async def test_get_pairs_gives_up_after_retry():
    dex = FakeDex([[], []])
    assert await dex._get_pairs("/x") == []
    assert len(dex.calls) == 2
    await dex.close()


@pytest.mark.asyncio
async def test_bucket_produces_stable_key_within_a_cycle():
    dex = FakeDex([[{"a": 1}], [{"a": 1}]])
    await dex._get_pairs("/x", bucket=20.0)
    await dex._get_pairs("/x", bucket=20.0)
    assert dex.calls[0]["_"] == dex.calls[1]["_"]
    await dex.close()


@pytest.mark.asyncio
async def test_fetch_many_keeps_most_liquid_pair():
    pairs = [
        {
            "chainId": "solana", "dexId": "pumpswap", "pairAddress": "p1",
            "baseToken": {"address": MINT, "symbol": "CATE"},
            "liquidity": {"usd": 100.0},
        },
        {
            "chainId": "solana", "dexId": "pumpswap", "pairAddress": "p2",
            "baseToken": {"address": MINT, "symbol": "CATE"},
            "liquidity": {"usd": 900.0},
        },
    ]
    dex = FakeDex([pairs])
    out = await dex.fetch_many([MINT])
    assert out[MINT].pair_address == "p2"
    await dex.close()


@pytest.mark.asyncio
async def test_fetch_many_ignores_other_chains():
    dex = FakeDex([[{"chainId": "base", "pairAddress": "x", "baseToken": {"address": "y"}}]])
    assert await dex.fetch_many([MINT]) == {}
    await dex.close()


# --- migrated coins must not be represented by their bonding-curve pair ------
#
# Live regression: CRYPTO (7KkXNCoJ...) had migrated to a pumpswap pool worth
# ~$400k, but `/tokens/v1/solana/<mint>` returned exactly one pair - the
# abandoned `pumpfun` curve, frozen at the moment of migration: marketCap
# 39,329, liquidity null, 5m volume 0. The bot published "MC: 39k / ATH: 39k",
# never saw a price move again, and its 24h volume of 40k fell under the
# volume filter, so a 60% dip produced no alert at all.

CURVE_PAIR = {
    "chainId": "solana",
    "dexId": "pumpfun",
    "pairAddress": "4TgwdisarGqxoyQ6sNrzWLFrLoj96jmnRpDi5Dd8rdPA",
    "baseToken": {"address": MINT, "symbol": "CRYPTO"},
    "priceUsd": "0.00003932",
    "marketCap": 39329.45,
    "volume": {"h24": 40469.14, "m5": 0.0},
}
POOL_PAIR = {
    "chainId": "solana",
    "dexId": "pumpswap",
    "pairAddress": "4snXWos2LSE8tkn2fx65hXq8BMmQAPizWdFSXJVUJG67",
    "baseToken": {"address": MINT, "symbol": "CRYPTO"},
    "priceUsd": "0.0001854",
    "liquidity": {"usd": 42743.89},
    "marketCap": 183627.0,
    "volume": {"h24": 1_000_000.0, "m5": 8781.0},
}


class PathDex(FakeDex):
    """FakeDex that also records the path of every call."""

    def __init__(self, responses):
        super().__init__(responses)
        self.paths: list[str] = []

    async def _get(self, path, params=None, tries=4):
        self.paths.append(path)
        return await super()._get(path, params=params, tries=tries)


def test_bonding_curve_recognised_under_both_names():
    """DexScreener says `pump` on one endpoint and `pumpfun` on another."""
    assert make_meta(dex_id="pump").is_bonding_curve
    assert make_meta(dex_id="pumpfun").is_bonding_curve
    assert not make_meta(dex_id="pumpswap").is_bonding_curve
    assert tier_for(make_meta(dex_id="pumpfun")) is Tier.REALTIME


def test_pairs_of_unwraps_the_object_response():
    """`/latest/dex/*` wraps pairs in an object and nulls it when empty."""
    assert DexScreener._pairs_of({"pairs": [{"a": 1}]}) == [{"a": 1}]
    assert DexScreener._pairs_of({"pairs": None}) == []
    assert DexScreener._pairs_of([{"a": 1}]) == [{"a": 1}]
    assert DexScreener._pairs_of(None) == []


@pytest.mark.asyncio
async def test_get_pairs_retries_past_a_cached_null_pairs_object():
    dex = FakeDex([{"pairs": None}, {"pairs": [{"ok": 1}]}])
    assert await dex._get_pairs("/x") == [{"ok": 1}]
    assert dex.empty_retries == 1
    await dex.close()


@pytest.mark.asyncio
async def test_resolve_prefers_the_real_pool_over_the_dead_curve():
    dex = PathDex([{"pairs": [CURVE_PAIR, POOL_PAIR]}])
    meta = await dex.resolve(MINT)
    assert meta.dex_id == "pumpswap"
    assert meta.market_cap == pytest.approx(183627.0)
    assert dex.paths[0].startswith("/latest/dex/tokens/")  # the all-pairs endpoint
    await dex.close()


@pytest.mark.asyncio
async def test_resolve_takes_the_curve_only_when_it_is_all_there_is():
    """A coin that genuinely has not migrated yet still resolves."""
    dex = PathDex([{"pairs": [CURVE_PAIR]}])
    meta = await dex.resolve(MINT)
    assert meta.dex_id == "pumpfun"
    assert meta.is_bonding_curve
    await dex.close()


@pytest.mark.asyncio
async def test_resolve_falls_back_through_the_other_endpoints():
    dex = PathDex([{"pairs": None}, {"pairs": None}, [], [], {"pairs": [POOL_PAIR]}])
    meta = await dex.resolve(MINT)
    assert meta.pair_address == POOL_PAIR["pairAddress"]
    assert dex.paths[-1].startswith("/latest/dex/pairs/solana/")
    await dex.close()


@pytest.mark.asyncio
async def test_fetch_pairs_asks_for_the_recorded_pool_by_address():
    dex = PathDex([{"pairs": [POOL_PAIR]}])
    out = await dex.fetch_pairs([POOL_PAIR["pairAddress"]])
    assert out[MINT].dex_id == "pumpswap"
    assert POOL_PAIR["pairAddress"] in dex.paths[0]
    assert dex.paths[0].startswith("/latest/dex/pairs/solana/")
    await dex.close()


@pytest.mark.asyncio
async def test_fetch_many_prefers_a_pool_over_a_curve_regardless_of_liquidity():
    """The curve pair reports no liquidity at all, so a plain max() picks it."""
    dex = FakeDex([[CURVE_PAIR, POOL_PAIR]])
    out = await dex.fetch_many([MINT])
    assert out[MINT].dex_id == "pumpswap"
    await dex.close()
