"""
ShockDetector
=============
Reactive entry signal for outright markets.

Integrates with PolymarketStreamer via on_price callback.
Monitors price windows per outright token and triggers when:
  - relative drop >= OUTRIGHT_SHOCK_DROP_PCT (default 15%)
  - absolute drop >= OUTRIGHT_SHOCK_DROP_ABS (default 3¢)

On shock detection: calls OutrightAnalyzer to verify EV still > threshold.
If confirmed, fires run_outright_execution() with trigger="shock".

Design:
  - Stateless except for a rolling price window (in-memory dict)
  - Does NOT initiate its own DB session — delegates execution to outright pipeline
  - Ignores shocks for teams already at position limit
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.models.schemas import OutrightMarket

log = get_logger(__name__)

# Rolling window size for peak price detection
_WINDOW_SECONDS = 300  # 5 minutes


@dataclass
class _PricePoint:
    price: float
    ts: float  # time.time()


class ShockDetector:
    """
    Monitors outright token prices via the WebSocket streamer.
    Call register_markets() after OutrightCollector runs.
    """

    def __init__(self) -> None:
        # token_id → deque of recent PricePoints
        self._history: dict[str, deque[_PricePoint]] = {}
        # token_id → OutrightMarket (for EV check)
        self._markets: dict[str, OutrightMarket] = {}
        # token_ids currently being processed (avoid duplicate signals)
        self._in_flight: set[str] = set()

    def register_markets(self, markets: list[OutrightMarket]) -> None:
        """Register markets to monitor. Called after OutrightCollector."""
        for m in markets:
            tid = m.yes_token_id
            self._markets[tid] = m
            if tid not in self._history:
                self._history[tid] = deque()
        log.info("shock_detector_registered", count=len(markets))

    async def on_price_event(self, msg: dict) -> None:
        """
        Callback wired to PolymarketStreamer.on_price.
        msg format: {"asset_id": str, "last_trade_price": str, "event_type": str}
        """
        token_id = msg.get("asset_id", "")
        if token_id not in self._markets:
            return

        price_raw = msg.get("last_trade_price") or msg.get("price")
        if price_raw is None:
            return
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            return

        now = time.time()
        window = self._history[token_id]
        window.append(_PricePoint(price=price, ts=now))

        # Evict points older than the window
        cutoff = now - _WINDOW_SECONDS
        while window and window[0].ts < cutoff:
            window.popleft()

        if len(window) < 2:
            return

        # Peak = highest price in the window
        peak = max(p.price for p in window)
        drop_rel = (peak - price) / peak if peak > 0 else 0.0
        drop_abs = peak - price

        if (
            drop_rel >= settings.outright_shock_drop_pct
            and drop_abs >= settings.outright_shock_drop_abs
            and token_id not in self._in_flight
        ):
            log.info(
                "outright_shock_detected",
                team=self._markets[token_id].team_name,
                token=token_id[:8],
                peak=round(peak, 4),
                current=round(price, 4),
                drop_rel=f"{drop_rel:.1%}",
                drop_abs=round(drop_abs, 4),
            )
            asyncio.create_task(self._handle_shock(token_id, price))

    async def _handle_shock(self, token_id: str, current_price: float) -> None:
        """Verify EV and fire execution if signal holds."""
        self._in_flight.add(token_id)
        try:
            market = self._markets[token_id]

            # Update market price with the shocked price
            shocked_market = market.model_copy(
                update={"yes_price": current_price, "best_ask": current_price}
            )

            # Re-run analyzer on just this one market
            # Use all registered markets for proper p_model normalization
            all_markets = list(self._markets.values())
            all_markets_updated = [
                m.model_copy(update={"yes_price": current_price, "best_ask": current_price})
                if m.yes_token_id == token_id else m
                for m in all_markets
            ]

            from sporting_edge.agents.outright_analyzer import analyze_outright_markets
            signals = analyze_outright_markets(
                [shocked_market], trigger="shock"
            )
            # Re-normalize using full market set for p_model
            signals_full = analyze_outright_markets(all_markets_updated, trigger="shock")
            # Take signal for this token only
            signals = [s for s in signals_full if s.market.yes_token_id == token_id]

            if not signals:
                log.info(
                    "shock_no_ev",
                    team=market.team_name,
                    current_price=current_price,
                )
                return

            # Fire execution
            from sporting_edge.graph.outright_pipeline import execute_outright_signals
            await execute_outright_signals(signals)

        except Exception as exc:
            log.error("shock_handler_error", token=token_id[:8], error=str(exc))
        finally:
            # Cooldown: allow re-trigger after 10 min
            await asyncio.sleep(600)
            self._in_flight.discard(token_id)


# ── Global singleton ──────────────────────────────────────────────────────────

_detector: ShockDetector | None = None


def get_shock_detector() -> ShockDetector | None:
    return _detector


def init_shock_detector() -> ShockDetector:
    global _detector
    if _detector is None:
        _detector = ShockDetector()
    return _detector
