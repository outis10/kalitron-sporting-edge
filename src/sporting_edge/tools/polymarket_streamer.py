"""
PolymarketStreamer
=================
WebSocket client for real-time Polymarket market data.

Critical for the pre-kickoff window (30-90 min before kickoff) where
line movements are most significant and the edge hypothesis applies.

Features (mirrored from polymarket-trading-system):
  - Hot book cache: asset_id → latest orderbook snapshot
  - Per-asset staleness tracking with configurable max_age
  - Dynamic subscription: add new assets without reconnecting
  - Health monitoring: connected status, stale asset list
  - Ping loop to keep the connection alive (Polymarket drops idle WS)
  - Auto-reconnect on disconnect or error
  - Fallback to REST when cache is stale

Usage:
    streamer = PolymarketStreamer(asset_ids=["token_id_1", "token_id_2"])
    asyncio.create_task(streamer.start())

    # Later, get a fresh book:
    book = streamer.get_cached_book("token_id_1", max_age_seconds=10)
    if book is None:
        book = fetch_rest_fallback("token_id_1")
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sporting_edge.config.logging import get_logger

log = get_logger(__name__)

POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL_SECONDS = 10
RECONNECT_BASE_DELAY_SECONDS = 2     # first reconnect attempt delay
RECONNECT_MAX_DELAY_SECONDS = 60     # cap backoff at 60s
RECONNECT_JITTER_SECONDS = 1.0       # random jitter to avoid thundering herd


@dataclass
class BookSnapshot:
    """A cached orderbook snapshot for a single token."""
    asset_id: str
    bids: list[dict]   # [{"price": float, "shares": float}, ...]
    asks: list[dict]
    last_trade_price: float | None
    updated_at: float  # time.time()

    @property
    def best_bid(self) -> float | None:
        return self.bids[0]["price"] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0]["price"] if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None


class PolymarketStreamer:
    """
    Real-time Polymarket WebSocket client with hot cache and fallback REST.

    Designed to be run as a background asyncio Task alongside the main
    APScheduler pipeline.
    """

    def __init__(
        self,
        asset_ids: list[str],
        on_book: Callable[[BookSnapshot], Awaitable[None]] | None = None,
        on_price: Callable[[dict], Awaitable[None]] | None = None,
    ):
        self.asset_ids: list[str] = list(asset_ids)
        self.on_book = on_book
        self.on_price = on_price

        self._running = False
        self._ws = None
        self._cache: dict[str, BookSnapshot] = {}
        self._last_tick_at: float = 0.0
        self._pending_subscribe: list[str] = []
        self._reconnect_attempts: int = 0
        self._consecutive_errors: int = 0

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect and stream indefinitely — run as a background task."""
        try:
            import websockets
        except ImportError:
            log.error("websockets package not installed — pip install websockets")
            return

        self._running = True
        while self._running:
            try:
                async with websockets.connect(POLYMARKET_WS_URL) as ws:
                    self._ws = ws
                    self._consecutive_errors = 0  # reset on successful connect
                    self._reconnect_attempts += 1
                    log.info(
                        "polymarket_ws_connected",
                        assets=len(self.asset_ids),
                        attempt=self._reconnect_attempts,
                    )

                    await self._subscribe(ws, self.asset_ids)
                    ping_task = asyncio.create_task(self._ping_loop(ws))

                    try:
                        async for raw in ws:
                            if not self._running:
                                break
                            if self._pending_subscribe:
                                await self._flush_pending(ws)
                            self._last_tick_at = time.time()
                            try:
                                msgs = json.loads(raw)
                                if not isinstance(msgs, list):
                                    msgs = [msgs]
                                for msg in msgs:
                                    await self._handle_message(msg)
                            except Exception as exc:
                                log.debug("ws_message_parse_error", error=str(exc))
                    finally:
                        ping_task.cancel()

            except Exception as exc:
                self._consecutive_errors += 1
                delay = self._backoff_delay()
                cls_name = type(exc).__name__
                if "ConnectionClosed" in cls_name:
                    log.warning(
                        "polymarket_ws_disconnected_reconnecting",
                        attempt=self._consecutive_errors,
                        retry_in=delay,
                    )
                else:
                    log.error(
                        "polymarket_ws_error",
                        error=str(exc),
                        attempt=self._consecutive_errors,
                        retry_in=delay,
                    )
                await asyncio.sleep(delay)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    def _backoff_delay(self) -> float:
        """Exponential backoff with jitter: base * 2^attempt, capped at max."""
        delay = RECONNECT_BASE_DELAY_SECONDS * (2 ** min(self._consecutive_errors - 1, 5))
        delay = min(delay, RECONNECT_MAX_DELAY_SECONDS)
        jitter = random.uniform(0, RECONNECT_JITTER_SECONDS)
        return delay + jitter

    # ── Subscription ──────────────────────────────────────────────────────────

    async def _subscribe(self, ws, asset_ids: list[str]) -> None:
        msg = {"assets_ids": asset_ids, "type": "market"}
        await ws.send(json.dumps(msg))
        log.info("polymarket_ws_subscribed", count=len(asset_ids))

    def sync_assets(self, new_asset_ids: list[str]) -> int:
        """
        Queue new assets for subscription without reconnecting.
        The subscription message is sent on the next WS tick.
        Returns how many new assets were added.
        """
        current = set(self.asset_ids)
        added = 0
        for aid in new_asset_ids:
            if aid not in current:
                self.asset_ids.append(aid)
                self._pending_subscribe.append(aid)
                current.add(aid)
                added += 1
        return added

    async def _flush_pending(self, ws) -> None:
        if not self._pending_subscribe:
            return
        to_sub = list(self._pending_subscribe)
        self._pending_subscribe.clear()
        try:
            await self._subscribe(ws, to_sub)
        except Exception as exc:
            log.warning("ws_subscribe_flush_failed", error=str(exc))
            self._pending_subscribe.extend(to_sub)

    # ── Message handling ──────────────────────────────────────────────────────

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        event_type = msg.get("event_type", "")

        if event_type == "book":
            snapshot = self._parse_book_message(msg)
            if snapshot:
                self._cache[snapshot.asset_id] = snapshot
                if self.on_book:
                    await self.on_book(snapshot)

        elif event_type in ("last_trade_price", "price_change"):
            if self.on_price:
                await self.on_price(msg)

    def _parse_book_message(self, msg: dict[str, Any]) -> BookSnapshot | None:
        asset_id = msg.get("asset_id", "")
        if not asset_id:
            return None

        def _parse_levels(raw: list, sort_asc: bool) -> list[dict]:
            levels = []
            for lv in (raw or []):
                try:
                    levels.append({
                        "price": float(lv.get("price", 0)),
                        "shares": float(lv.get("size", lv.get("shares", 0))),
                    })
                except (TypeError, ValueError):
                    pass
            return sorted(levels, key=lambda x: x["price"], reverse=not sort_asc)

        bids = _parse_levels(msg.get("bids", []), sort_asc=False)
        asks = _parse_levels(msg.get("asks", []), sort_asc=True)

        last_trade = None
        if msg.get("last_trade_price"):
            try:
                last_trade = float(msg["last_trade_price"])
            except (TypeError, ValueError):
                pass

        return BookSnapshot(
            asset_id=asset_id,
            bids=bids,
            asks=asks,
            last_trade_price=last_trade,
            updated_at=time.time(),
        )

    # ── Ping ─────────────────────────────────────────────────────────────────

    async def _ping_loop(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_SECONDS)
                await ws.ping()
        except asyncio.CancelledError:
            pass

    # ── Hot cache access ──────────────────────────────────────────────────────

    def get_cached_book(
        self, asset_id: str, max_age_seconds: float = 10.0
    ) -> BookSnapshot | None:
        """
        Return a cached snapshot if it's fresh enough, else None.
        Caller should fall back to REST when this returns None.
        """
        snapshot = self._cache.get(asset_id)
        if snapshot is None:
            return None
        age = time.time() - snapshot.updated_at
        if age > max_age_seconds:
            return None
        return snapshot

    def get_best_ask(self, asset_id: str, max_age_seconds: float = 10.0) -> float | None:
        """Convenience: return best ask price if cache is fresh."""
        snap = self.get_cached_book(asset_id, max_age_seconds)
        return snap.best_ask if snap else None

    # ── Health ────────────────────────────────────────────────────────────────

    def seconds_since_last_tick(self) -> float | None:
        if self._last_tick_at == 0.0:
            return None
        return time.time() - self._last_tick_at

    def health(
        self,
        asset_ids: list[str] | None = None,
        max_age_seconds: float = 10.0,
    ) -> dict:
        """
        Returns a health dict:
          connected, last_tick_age, total_cached, fresh_count, stale_count, stale_assets
        """
        now = time.time()
        check_ids = asset_ids or self.asset_ids
        stale: list[str] = []
        fresh = 0

        for aid in check_ids:
            snap = self._cache.get(aid)
            if snap is None or (now - snap.updated_at) > max_age_seconds:
                stale.append(aid)
            else:
                fresh += 1

        return {
            "connected": self._ws is not None and self._running,
            "last_tick_age": self.seconds_since_last_tick(),
            "total_cached": len(self._cache),
            "fresh_count": fresh,
            "stale_count": len(stale),
            "stale_assets": stale,
        }


# ── Global streamer singleton (managed by FastAPI lifespan) ──────────────────

_streamer: PolymarketStreamer | None = None


def get_streamer() -> PolymarketStreamer | None:
    return _streamer


def init_streamer(asset_ids: list[str]) -> PolymarketStreamer:
    """Create or return the global streamer. Called from FastAPI lifespan."""
    global _streamer
    if _streamer is None:
        _streamer = PolymarketStreamer(asset_ids=asset_ids)
    else:
        _streamer.sync_assets(asset_ids)
    return _streamer
