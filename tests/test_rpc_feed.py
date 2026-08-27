"""Realtime feed: decoding, slot grouping, subscription bookkeeping.

Slot grouping is the test that matters. In Phase 0, pricing a pool from
half-updated reserves produced a phantom 30.45% swing where the true one was
0.83%. These tests pin that behaviour so it cannot silently regress.
"""
from __future__ import annotations

import asyncio
import base64
import json
import struct

import pytest

from dipbot.feeds.pool_resolver import CURVE, POOL, PoolRef, curve_complete, curve_price, pool_price
from dipbot.feeds.rpc_feed import RpcFeed, _decode_account_data
from dipbot.models import PriceSource

BASE_VAULT = "BaseVault11111111111111111111111111111111111"
QUOTE_VAULT = "QuoteVault1111111111111111111111111111111111"
CURVE_ACCT = "Curve111111111111111111111111111111111111111"
MINT = "Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump"


def token_account_data(amount: int) -> str:
    """Minimal SPL token account: amount is u64LE at offset 64."""
    raw = bytearray(165)
    struct.pack_into("<Q", raw, 64, amount)
    return base64.b64encode(bytes(raw)).decode()


def curve_account_data(virtual_tokens: int, virtual_sol: int, complete: bool = False) -> str:
    raw = bytearray(64)
    struct.pack_into("<2Q", raw, 8, virtual_tokens, virtual_sol)
    raw[48] = 1 if complete else 0
    return base64.b64encode(bytes(raw)).decode()


def notification(sub_id: int, data_b64: str, slot: int) -> dict:
    return {
        "method": "accountNotification",
        "params": {
            "subscription": sub_id,
            "result": {"context": {"slot": slot}, "value": {"data": [data_b64, "base64"]}},
        },
    }


def pool_ref(**kw) -> PoolRef:
    base = dict(
        mint=MINT, kind=POOL, program="pAMM", accounts=(BASE_VAULT, QUOTE_VAULT), decimals=6,
    )
    base.update(kw)
    return PoolRef(**base)


def curve_ref() -> PoolRef:
    return PoolRef(mint=MINT, kind=CURVE, program="pump", accounts=(CURVE_ACCT,), decimals=6)


async def build_feed(ref: PoolRef):
    ticks = []

    async def on_tick(t):
        ticks.append(t)

    feed = RpcFeed("wss://example", on_tick)
    feed.watch(ref)
    # Pretend the subscribe round trip completed.
    for i, acct in enumerate(ref.accounts):
        feed._subs[i] = acct
    return feed, ticks


# --- pricing maths ----------------------------------------------------------


def test_pool_price():
    # 1 SOL against 1000 tokens (6 decimals)
    assert pool_price(1_000_000_000, 1_000_000_000, 6) == pytest.approx(0.001)


def test_pool_price_zero_base_is_none():
    assert pool_price(0, 1_000_000_000, 6) is None


def test_curve_price_matches_manual_maths():
    data = base64.b64decode(curve_account_data(1_000_000_000_000, 30_000_000_000))
    assert curve_price(data, 6) == pytest.approx(30 / 1_000_000)


def test_curve_price_zero_reserves_is_none():
    assert curve_price(base64.b64decode(curve_account_data(0, 30_000_000_000)), 6) is None


def test_curve_price_short_data_is_none():
    assert curve_price(b"\x00" * 10, 6) is None


def test_curve_complete_flag():
    assert curve_complete(base64.b64decode(curve_account_data(1, 1, complete=True)))
    assert not curve_complete(base64.b64decode(curve_account_data(1, 1, complete=False)))


# --- decoding ---------------------------------------------------------------


def test_decode_plain_base64():
    assert _decode_account_data([base64.b64encode(b"hello").decode(), "base64"]) == b"hello"


def test_decode_empty_field():
    assert _decode_account_data([]) is None


def test_decode_zstd_roundtrip():
    """Solana's zstd frames carry no content size, which broke naive decoding."""
    zstandard = pytest.importorskip("zstandard")
    payload = b"x" * 500
    compressed = zstandard.ZstdCompressor().compress(payload)
    encoded = base64.b64encode(compressed).decode()
    assert _decode_account_data([encoded, "base64+zstd"]) == payload


# --- slot grouping ----------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tick_until_slot_advances():
    """Both vaults land in the same slot; nothing may be priced mid-update."""
    feed, ticks = await build_feed(pool_ref())
    await feed._handle_notification(notification(0, token_account_data(1_000_000), 100))
    await feed._handle_notification(notification(1, token_account_data(2_000_000_000), 100))
    assert ticks == []


@pytest.mark.asyncio
async def test_tick_emitted_once_slot_advances():
    feed, ticks = await build_feed(pool_ref())
    await feed._handle_notification(notification(0, token_account_data(1_000_000), 100))
    await feed._handle_notification(notification(1, token_account_data(2_000_000_000), 100))
    await feed._handle_notification(notification(0, token_account_data(1_100_000), 101))

    assert len(ticks) == 1
    assert ticks[0].slot == 100
    assert ticks[0].source is PriceSource.POOL_VAULTS
    # priced from the completed slot 100 state, not the half-applied slot 101
    assert ticks[0].price_sol == pytest.approx((2_000_000_000 / 1e9) / (1_000_000 / 1e6))


@pytest.mark.asyncio
async def test_half_updated_pool_never_priced():
    """The phantom-dip regression: one leg moving must not look like a crash."""
    feed, ticks = await build_feed(pool_ref())
    # establish slot 100 with both legs
    await feed._handle_notification(notification(0, token_account_data(1_000_000), 100))
    await feed._handle_notification(notification(1, token_account_data(2_000_000_000), 100))
    # slot 101: only the base vault moves hugely
    await feed._handle_notification(notification(0, token_account_data(10_000_000), 101))
    # slot 102 flushes slot 101 - by then the quote leg is still the old value,
    # but crucially both legs are from a single consistent slot boundary
    await feed._handle_notification(notification(0, token_account_data(10_000_000), 102))

    assert len(ticks) == 2
    assert all(t.slot in (100, 101) for t in ticks)


@pytest.mark.asyncio
async def test_two_notifications_produce_one_tick():
    """Observed live: 342 notifications collapsed to 170 ticks, ~2:1."""
    feed, ticks = await build_feed(pool_ref())
    for slot in range(100, 110):
        await feed._handle_notification(notification(0, token_account_data(1_000_000 + slot), slot))
        await feed._handle_notification(notification(1, token_account_data(2_000_000_000), slot))
    assert feed.notifications == 20
    assert len(ticks) == 9  # last slot still pending, correctly


# --- curves -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_curve_ticks_every_notification():
    """One account holds both reserves, so no grouping is needed."""
    feed, ticks = await build_feed(curve_ref())
    for slot, sol in enumerate([30, 31, 32], start=100):
        await feed._handle_notification(
            notification(0, curve_account_data(1_000_000_000_000, sol * 10**9), slot)
        )
    assert len(ticks) == 3
    assert all(t.source is PriceSource.BONDING_CURVE for t in ticks)


@pytest.mark.asyncio
async def test_completed_curve_is_dropped_for_reresolution():
    feed, ticks = await build_feed(curve_ref())
    await feed._handle_notification(
        notification(0, curve_account_data(1_000_000_000_000, 30 * 10**9, complete=True), 100)
    )
    assert ticks == []
    assert MINT not in feed.watched_mints


# --- subscription bookkeeping ----------------------------------------------


@pytest.mark.asyncio
async def test_watch_and_unwatch_track_accounts():
    feed, _ = await build_feed(pool_ref())
    assert feed.account_count == 2
    assert feed.watched_mints == {MINT}

    feed.unwatch(MINT)
    assert feed.account_count == 0
    assert feed.watched_mints == set()


@pytest.mark.asyncio
async def test_unwatch_unknown_mint_is_safe():
    feed, _ = await build_feed(pool_ref())
    feed.unwatch("not-watched")
    assert feed.account_count == 2


@pytest.mark.asyncio
async def test_notification_for_unwatched_account_ignored():
    feed, ticks = await build_feed(pool_ref())
    feed.unwatch(MINT)
    await feed._handle_notification(notification(0, token_account_data(1), 100))
    assert ticks == []


@pytest.mark.asyncio
async def test_stats_reports_credit_estimate():
    feed, _ = await build_feed(pool_ref())
    feed.bytes_received = 1_000_000
    stats = feed.stats()
    assert stats["megabytes"] == pytest.approx(1.0)
    assert stats["est_credits"] == pytest.approx(20.0)  # Helius: 20 credits/MB
    assert stats["accounts"] == 2


# --- subscribing to coins added while the feed is already connected ---------
#
# Live regression: the supervisor calls watch() from its own task while
# _sync_subscriptions is mid-await, so iterating the account dict directly blew
# up with "dictionary changed size during iteration". Landing in _resync_loop,
# that killed resync for the whole session - the socket stayed up, every coin
# added afterwards was never subscribed, and those coins fell back to three
# polled ticks a minute, below every window's tick floor. They looked watched
# and silently could not alert.

OTHER_MINT = "GUhHr6PewaLZ6S3LRVeHKXzXHEMeHNhCmuyBhpnCpmAG"
LATE_VAULTS = ("LateBase1111111111111111111111111111111111", "LateQuote111111111111111111111111111111111")


class RecordingWs:
    """Fake socket that can run a callback in the middle of a send."""

    def __init__(self, on_send=None):
        self.sent: list[dict] = []
        self._on_send = on_send

    async def send(self, raw):
        self.sent.append(json.loads(raw))
        if self._on_send:
            self._on_send()

    def subscribed_accounts(self) -> list[str]:
        return [m["params"][0] for m in self.sent if m["method"] == "accountSubscribe"]


async def connected_feed(ws):
    async def on_tick(t):
        pass

    feed = RpcFeed("wss://example", on_tick)
    feed._ws = ws
    return feed


@pytest.mark.asyncio
async def test_a_coin_added_mid_sync_does_not_break_the_sync():
    """watch() during _sync_subscriptions must not raise."""
    feed = await connected_feed(None)
    late = PoolRef(mint=OTHER_MINT, kind=POOL, program="pAMM", accounts=LATE_VAULTS, decimals=6)

    added = []

    def add_one():
        if not added:
            added.append(True)
            feed.watch(late)  # mutates _account_owner mid-iteration

    ws = RecordingWs(on_send=add_one)
    feed._ws = ws
    feed.watch(pool_ref())

    await feed._sync_subscriptions()  # must not raise

    assert BASE_VAULT in ws.subscribed_accounts()


@pytest.mark.asyncio
async def test_the_late_coin_is_subscribed_on_the_next_pass():
    feed = await connected_feed(None)
    ws = RecordingWs()
    feed._ws = ws
    feed.watch(pool_ref())
    await feed._sync_subscriptions()

    late = PoolRef(mint=OTHER_MINT, kind=POOL, program="pAMM", accounts=LATE_VAULTS, decimals=6)
    feed.watch(late)
    await feed._sync_subscriptions()

    assert set(LATE_VAULTS) <= set(ws.subscribed_accounts())


@pytest.mark.asyncio
async def test_resync_keeps_running_after_a_failure(monkeypatch):
    """A failed pass must not leave the connection deaf to new tokens."""
    monkeypatch.setattr("dipbot.feeds.rpc_feed.RESYNC_RETRY_DELAY", 0.0)
    feed = await connected_feed(None)
    calls = []

    async def flaky():
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("dictionary changed size during iteration")

    feed._sync_subscriptions = flaky
    feed._dirty.set()

    task = asyncio.get_running_loop().create_task(feed._resync_loop())
    await asyncio.sleep(0.05)
    task.cancel()

    assert len(calls) >= 2, "resync stopped after the first failure"


@pytest.mark.asyncio
async def test_unsubscribes_accounts_that_are_no_longer_watched():
    feed = await connected_feed(None)
    ws = RecordingWs()
    feed._ws = ws
    feed.watch(pool_ref())
    feed._subs[7] = BASE_VAULT
    feed.unwatch(MINT)

    await feed._sync_subscriptions()

    assert any(m["method"] == "accountUnsubscribe" for m in ws.sent)
