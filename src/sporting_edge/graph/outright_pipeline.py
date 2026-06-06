"""
Outright Pipeline
=================
Standalone pipeline for WC 2026 outright tournament trading.

OutrightCollector → OutrightAnalyzer → execute_outright_signals

Runs on the same scheduler interval as the match pipeline but is
completely independent — shares only ExecutionAgent infrastructure
(CLOB client, FAK orders, BetORM persistence).

Position management (TP/SL) is handled by the existing PositionManager
using outright_tp_multiplier / outright_sl_multiplier from settings.
Settlement is handled by Polymarket (token resolves to $1 or $0) —
BetSettler skips outright bets (kickoff_utc=None filter).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from sporting_edge.agents.outright_analyzer import analyze_outright_markets
from sporting_edge.agents.outright_collector import collect_outright_markets
from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.db.models import BetORM
from sporting_edge.db.session import AsyncSessionLocal
from sporting_edge.models.schemas import BetStatus, OutrightSignal
from sporting_edge.tools.polymarket_tools import (
    estimate_fill,
    fetch_real_odds_from_clob,
    place_fak_order,
)

log = get_logger(__name__)


async def run_outright_pipeline() -> dict:
    """
    Main entry point — called by APScheduler on the same interval as the match pipeline.
    Returns a summary dict for logging.
    """
    summary = {"markets_found": 0, "signals": 0, "bets_placed": 0, "skipped": 0}

    try:
        markets = await collect_outright_markets()
        summary["markets_found"] = len(markets)

        if not markets:
            log.info("outright_pipeline_no_markets")
            return summary

        # Register markets with ShockDetector (updates subscription list)
        from sporting_edge.agents.shock_detector import get_shock_detector
        detector = get_shock_detector()
        if detector:
            detector.register_markets(markets)
            # Subscribe outright token IDs to the WebSocket streamer
            from sporting_edge.tools.polymarket_streamer import get_streamer
            streamer = get_streamer()
            if streamer:
                token_ids = [m.yes_token_id for m in markets]
                added = streamer.sync_assets(token_ids)
                log.debug("outright_tokens_subscribed", added=added)

        signals = analyze_outright_markets(markets, trigger="proactive")
        summary["signals"] = len(signals)

        if not signals:
            log.info("outright_pipeline_no_signals", markets=len(markets))
            return summary

        # Enforce max positions limit
        open_outright_count = await _count_open_outright_positions()
        available_slots = settings.outright_max_positions - open_outright_count
        if available_slots <= 0:
            log.info(
                "outright_max_positions_reached",
                open=open_outright_count,
                limit=settings.outright_max_positions,
            )
            return summary

        signals_to_execute = signals[:available_slots]
        placed = await execute_outright_signals(signals_to_execute)
        summary["bets_placed"] = placed
        summary["skipped"] = len(signals) - len(signals_to_execute)

    except Exception as exc:
        log.error("outright_pipeline_error", error=str(exc))

    log.info("outright_pipeline_done", **summary)
    return summary


async def execute_outright_signals(signals: list[OutrightSignal]) -> int:
    """
    Place orders for a list of OutrightSignals.
    Returns number of bets successfully placed.
    Shared by both the proactive pipeline and ShockDetector.
    """
    placed = 0
    is_paper = settings.paper_trading or not settings.execute_trades

    async with AsyncSessionLocal() as db:
        for signal in signals:
            market = signal.market
            token_id = market.yes_token_id
            size_usd = signal.size_usd
            entry_price = market.best_ask or market.yes_price

            # Skip if we already have an open position for this team
            if await _has_open_position(db, market.condition_id):
                log.info(
                    "outright_position_exists",
                    team=market.team_name,
                    condition_id=market.condition_id[:16],
                )
                continue

            # Fetch live CLOB prices in live mode
            clob_prices = None
            if not is_paper and market.yes_token_id and market.no_token_id:
                clob_prices = fetch_real_odds_from_clob(
                    yes_token_id=market.yes_token_id,
                    no_token_id=market.no_token_id,
                )

            if clob_prices:
                entry_price = clob_prices.yes_ask
                ask_levels = clob_prices.yes_asks

                if ask_levels:
                    fill_est = estimate_fill(ask_levels, size_usd)
                    if fill_est.insufficient_liquidity:
                        log.warning(
                            "outright_no_liquidity",
                            team=market.team_name,
                        )
                        continue
                    if fill_est.avg_fill_price:
                        entry_price = fill_est.avg_fill_price

            if entry_price <= 0:
                continue

            shares = round(size_usd / entry_price, 4)
            hint_price = clob_prices.yes_ask if clob_prices else 0.0

            resp = place_fak_order(
                token_id=token_id,
                side="BUY",
                size_usd=size_usd,
                hint_price=hint_price,
            )

            polymarket_order_id = resp.get("order_id") or resp.get("orderID")
            status = BetStatus.PAPER if is_paper else BetStatus.OPEN
            bet_id = uuid.uuid4()

            stmt = pg_insert(BetORM).values(
                id=bet_id,
                signal_id=bet_id,            # outright bets don't go through SignalORM
                match_id=None,               # outright bets have no match row
                condition_id=market.condition_id,
                market_question=market.question,
                outcome="home",              # sentinel — outright has no 1X2 outcome
                side="YES",
                entry_price=entry_price,
                size_usd=size_usd,
                shares=shares,
                token_id=token_id,
                kickoff_utc=None,            # no kickoff → PositionManager uses price_check only
                paper_trade=is_paper,
                status=status.value,
                polymarket_order_id=polymarket_order_id,
                bet_type="outright",
            ).on_conflict_do_nothing()
            await db.execute(stmt)

            placed += 1
            log.info(
                "outright_bet_placed",
                team=market.team_name,
                entry_price=entry_price,
                size_usd=size_usd,
                ev=f"{signal.expected_value:.1%}",
                trigger=signal.trigger,
                paper=is_paper,
            )

            await _notify_outright_bet(signal, entry_price, size_usd, is_paper)

        await db.commit()

    return placed


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _count_open_outright_positions() -> int:
    from sqlalchemy import select, func
    from sporting_edge.db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(func.count()).select_from(BetORM).where(
                BetORM.bet_type == "outright",
                BetORM.status.in_(["open", "paper"]),
            )
        )
        return result.scalar() or 0


async def _has_open_position(db, condition_id: str) -> bool:
    from sqlalchemy import select
    result = await db.execute(
        select(BetORM.id).where(
            BetORM.condition_id == condition_id,
            BetORM.status.in_(["open", "paper"]),
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _notify_outright_bet(
    signal: OutrightSignal,
    entry_price: float,
    size_usd: float,
    is_paper: bool,
) -> None:
    if not settings.notifications_enabled:
        return
    try:
        from telegram import Bot
        bot = Bot(token=settings.telegram_bot_token)
        trigger_label = "📊 Proactive" if signal.trigger == "proactive" else "⚡ Shock"
        text = (
            f"*🏆 Outright Bet Placed — {trigger_label}*\n\n"
            f"Team: `{signal.market.team_name}`\n"
            f"Entry: `{entry_price:.3f}` | Size: `${size_usd:.2f}`\n"
            f"Model: `{signal.model_probability:.1%}` | Market: `{signal.market_probability:.1%}`\n"
            f"EV: `{signal.expected_value:.1%}`\n"
            f"Mode: `{'PAPER' if is_paper else 'LIVE'}`"
        )
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            parse_mode="Markdown",
        )
    except Exception as exc:
        log.warning("outright_notify_failed", error=str(exc))
