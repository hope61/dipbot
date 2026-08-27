"""Realtime price feed over Solana RPC `accountSubscribe`.

One WebSocket carries every watched account. Phase 0 held 22 accounts on a
single public-endpoint connection for 30 minutes with zero disconnects and
6901 notifications.

Two correctness rules are load-bearing:

  Slot grouping - a pool trade updates two vaults, arriving as two separate
      notifications. Pricing a half-updated pool produced a phantom 30.45%
      swing where the true one was 0.83%. A tick is emitted only once the slot
      advances, so reserves are always read as a consistent pair.

  Curves are atomic - a bonding curve holds both reserves in one account, so it
      needs no grouping and costs half as much to stream.

Cost control (Helius bills 20 credits/MB of streamed data):
  - the public endpoint is the default; the provider key is fallback only
  - `base64+zstd` encoding, measured 518 B -> 431 B per notification
  - only subscribed to tokens we would actually alert on
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time
from typing import Awaitable, Callable

import websockets

from ..models import PriceSource, PriceTick
from .pool_resolver import CURVE, PoolRef, curve_complete, curve_price, pool_price

log = logging.getLogger(__name__)

TickHandler = Callable[[PriceTick], Awaitable[None]]

MAX_BACKOFF = 60.0
RESYNC_RETRY_DELAY = 1.0
SILENCE_TIMEOUT = 90.0  # force a reconnect if nothing arrives at all


#: Solana's zstd frames carry no content size in the header, so the decompressor
#: has to be given an output ceiling. Account data we price from is well under
#: 2 KB; 1 MB is generous and still bounded.
MAX_ACCOUNT_BYTES = 1 << 20

try:  # optional - only needed when the zstd encoding is in use
    import zstandard

    _ZSTD = zstandard.ZstdDecompressor()
except ImportError:  # pragma: no cover
    _ZSTD = None


def _decode_account_data(data_field: list) -> bytes | None:
    """Decode `[payload, encoding]` from an account notification."""
    if not data_field:
        return None
    payload, encoding = data_field[0], (data_field[1] if len(data_field) > 1 else "base64")
    raw = base64.b64decode(payload)
    if encoding != "base64+zstd":
        return raw
    if _ZSTD is None:
        log.error("node returned zstd data but the zstandard package is missing")
        return None
    try:
        return _ZSTD.decompress(raw, max_output_size=MAX_ACCOUNT_BYTES)
    except Exception as e:
        log.debug("zstd decode failed: %s", e)
        return None


class RpcFeed:
    """Subscribes to chain accounts and emits `PriceTick`s."""

    def __init__(
        self,
        ws_url: str,
        on_tick: TickHandler,
        fallback_url: str = "",
        encoding: str = "base64",
    ):
        self.ws_url = ws_url
        self.fallback_url = fallback_url
        self.encoding = encoding
        self.on_tick = on_tick

        self._refs: dict[str, PoolRef] = {}
        self._state: dict[str, dict] = {}
        self._account_owner: dict[str, tuple[str, int]] = {}  # account -> (mint, index)
        self._subs: dict[int, str] = {}  # subscription id -> account
        self._req_to_account: dict[int, str] = {}
        self._req_id = 0
        self._ws = None
        self._dirty = asyncio.Event()
        #: Mints whose price source is no longer valid - currently bonding
        #: curves that graduated. The supervisor drains this and re-resolves;
        #: without it, a stale cached ref would be re-watched forever.
        self.stale: set[str] = set()
        #: Latest emitted price per mint, for cross-checking against
        #: DexScreener. A persistent disagreement means we are reading the
        #: wrong account and must not be trusted to alert.
        self.last_price: dict[str, float] = {}

        self.connected = False
        self.using_fallback = False
        self.notifications = 0
        self.ticks = 0
        self.reconnects = 0
        self.bytes_received = 0
        self.last_message_at = 0.0
        self.connected_since = 0.0

    # --- subscription management -------------------------------------------

    def watch(self, ref: PoolRef) -> None:
        if self._refs.get(ref.mint) == ref:
            return
        self._refs[ref.mint] = ref
        self._state[ref.mint] = {
            "base": ref.base_amount,
            "quote": ref.quote_amount,
            "pending_slot": None,
            "have_base": bool(ref.base_amount),
            "have_quote": bool(ref.quote_amount),
        }
        for index, account in enumerate(ref.accounts):
            self._account_owner[account] = (ref.mint, index)
        self._dirty.set()

    def unwatch(self, mint: str) -> None:
        ref = self._refs.pop(mint, None)
        if not ref:
            return
        self._state.pop(mint, None)
        for account in ref.accounts:
            self._account_owner.pop(account, None)
        self._dirty.set()

    @property
    def watched_mints(self) -> set[str]:
        return set(self._refs)

    @property
    def account_count(self) -> int:
        return len(self._account_owner)

    # --- lifecycle ----------------------------------------------------------

    async def run_forever(self) -> None:
        backoff = 1.0
        while True:
            url = self.fallback_url if self.using_fallback and self.fallback_url else self.ws_url
            try:
                await self._session(url)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                self.reconnects += 1
                log.warning("feed disconnected (%s: %s); retry in %.0fs", type(e).__name__, e, backoff)
                # Alternate endpoints on repeated failure rather than hammering one.
                if self.fallback_url and backoff >= 8:
                    self.using_fallback = not self.using_fallback
                    log.info("switching to %s endpoint", "fallback" if self.using_fallback else "primary")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    async def _session(self, url: str) -> None:
        async with websockets.connect(url, ping_interval=20, max_size=2**23) as ws:
            self._ws = ws
            self._subs.clear()
            self._req_to_account.clear()
            self.connected = True
            self.connected_since = time.time()
            self.last_message_at = time.time()
            log.info("feed connected to %s (%d accounts)", url.split("?")[0], self.account_count)

            await self._sync_subscriptions()
            resync = asyncio.create_task(self._resync_loop())
            try:
                await self._read_loop(ws)
            finally:
                resync.cancel()
                self.connected = False
                self._ws = None

    async def _resync_loop(self) -> None:
        """Subscribe to newly watched tokens without dropping the connection."""
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            try:
                await self._sync_subscriptions()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Never give up here. This used to `return`, which left the
                # connection alive but permanently deaf to new tokens: every
                # coin added afterwards fell back to 20s polled ticks, three a
                # minute, which cannot reach any window's tick floor. The coin
                # looked watched and simply never alerted.
                log.warning("resync failed (%s: %s), retrying", type(e).__name__, e)
                self._dirty.set()
                await asyncio.sleep(RESYNC_RETRY_DELAY)

    async def _sync_subscriptions(self) -> None:
        ws = self._ws
        if not ws:
            return
        subscribed = set(self._subs.values()) | set(self._req_to_account.values())
        # Snapshot: this loop awaits, and the supervisor calls watch()/unwatch()
        # meanwhile. Iterating the live dict raised "dictionary changed size
        # during iteration" every time a coin was added mid-sync, which either
        # dropped the whole connection or killed the resync loop outright.
        for account in list(self._account_owner):
            if account in subscribed or account not in self._account_owner:
                continue
            self._req_id += 1
            self._req_to_account[self._req_id] = account
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": self._req_id,
                "method": "accountSubscribe",
                "params": [account, {"encoding": self.encoding, "commitment": "processed"}],
            }))
            await asyncio.sleep(0.03)

        # Drop subscriptions for tokens no longer watched - we pay for those bytes.
        for sub_id, account in list(self._subs.items()):
            if account not in self._account_owner:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 900_000 + sub_id,
                    "method": "accountUnsubscribe", "params": [sub_id],
                }))
                self._subs.pop(sub_id, None)

    async def _read_loop(self, ws) -> None:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=SILENCE_TIMEOUT)
            except asyncio.TimeoutError:
                # Silence is normal on quiet tokens, but an actually dead socket
                # looks identical - reconnect rather than hang forever.
                if not self._account_owner:
                    continue
                raise ConnectionError(f"no messages for {SILENCE_TIMEOUT:.0f}s")

            self.last_message_at = time.time()
            self.bytes_received += len(raw)
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if "result" in msg and isinstance(msg["result"], int):
                account = self._req_to_account.pop(msg.get("id"), None)
                if account:
                    self._subs[msg["result"]] = account
                continue
            if "error" in msg:
                log.warning("rpc error: %s", str(msg["error"])[:160])
                continue
            if msg.get("method") != "accountNotification":
                continue

            await self._handle_notification(msg)

    async def _handle_notification(self, msg: dict) -> None:
        params = msg.get("params") or {}
        account = self._subs.get(params.get("subscription"))
        if not account:
            return
        owner = self._account_owner.get(account)
        if not owner:
            return
        mint, index = owner
        ref = self._refs.get(mint)
        state = self._state.get(mint)
        if not ref or state is None:
            return

        value = (params.get("result") or {}).get("value") or {}
        slot = ((params.get("result") or {}).get("context") or {}).get("slot")
        data = _decode_account_data(value.get("data") or [])
        if data is None:
            return
        self.notifications += 1

        if ref.kind == CURVE:
            # One account holds both reserves, so this is already consistent.
            if curve_complete(data):
                log.info("%s bonding curve complete - needs re-resolution", mint[:8])
                self.stale.add(mint)
                self.unwatch(mint)
                return
            price = curve_price(data, ref.decimals)
            if price:
                await self._emit(mint, price, slot, PriceSource.BONDING_CURVE)
            return

        amount = struct.unpack_from("<Q", data, 64)[0] if len(data) >= 72 else None
        if amount is None:
            return

        # Emit the previous slot's price before applying a newer slot's update,
        # so a price is never computed from half-updated reserves.
        if state["pending_slot"] is not None and slot != state["pending_slot"]:
            await self._flush_pool(mint, ref, state)
        state["pending_slot"] = slot
        if index == 0:
            state["base"], state["have_base"] = amount, True
        else:
            state["quote"], state["have_quote"] = amount, True

    async def _flush_pool(self, mint: str, ref: PoolRef, state: dict) -> None:
        if not (state["have_base"] and state["have_quote"]):
            return
        price = pool_price(state["base"], state["quote"], ref.decimals)
        if price:
            await self._emit(mint, price, state["pending_slot"], PriceSource.POOL_VAULTS)

    async def _emit(self, mint: str, price: float, slot: int | None, source: PriceSource) -> None:
        self.ticks += 1
        self.last_price[mint] = price
        await self.on_tick(PriceTick(mint=mint, price_sol=price, ts=time.time(), source=source, slot=slot))

    # --- reporting ----------------------------------------------------------

    def stats(self) -> dict:
        uptime = time.time() - self.connected_since if self.connected else 0.0
        megabytes = self.bytes_received / 1e6
        return {
            "connected": self.connected,
            "endpoint": "fallback" if self.using_fallback else "primary",
            "accounts": self.account_count,
            "tokens": len(self._refs),
            "notifications": self.notifications,
            "ticks": self.ticks,
            "reconnects": self.reconnects,
            "uptime_s": uptime,
            "megabytes": megabytes,
            # Helius bills 20 credits per MB; the public endpoint is free, so
            # this is what a fallback session would have cost.
            "est_credits": megabytes * 20,
        }
