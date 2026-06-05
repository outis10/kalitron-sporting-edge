"""
PositionManager Agent
=====================
Monitors all open pre-match positions and closes them when:

  1. TAKE-PROFIT  — current bid >= entry_price × (1 + take_profit_pct)
  2. STOP-LOSS    — current bid <= entry_price × (1 - stop_loss_pct)
  3. FORCE-CLOSE  — kickoff is within `force_close_minutes_before_kickoff`
                    (model is no longer valid once the match starts)

Price source priority:
  1. WebSocket streamer hot cache (best_bid for SELL, fresh within 15s)
  2. CLOB REST fallback via fetch_real_odds_from_clob()
  3. Skip position this cycle (log warning, try again next run)

Adapted from polymarket-trading-system/risk/position_manager.py:
  - Same stop_loss/take_profit trigger logic
  - Same _close_position() → SELL shares back to the CLOB
  - Added: force-close before kickoff (football-specific)
  - Added: async + SQLAlchemy DB persistence (sibling is in-memory only)
  - Added: FAK order type instead of LIMIT (sibling uses LIMIT)
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update

from sporting_edge.agents.model_predictor import ModelPrediction, OutcomeProbabilities, adjust_prediction_for_lineups
from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.db.models import BetORM, PredictionORM
from sporting_edge.db.session import AsyncSessionLocal
from sporting_edge.tools.football_api import FootballAPIClient
from sporting_edge.tools.polymarket_streamer import get_streamer
from sporting_edge.tools.polymarket_tools import fetch_token_best_ask, fetch_token_best_bid, place_fak_order

log = get_logger(__name__)

# Statuses that represent open, closable positions
OPEN_STATUSES = ("open", "paper")



async def run_position_manager() -> dict:
    """
    Main entry point — called by the APScheduler job every N minutes.
    Returns a summary dict for logging.
    """
    now = datetime.now(tz=timezone.utc)
    summary = {"checked": 0, "closed_tp": 0, "closed_sl": 0, "closed_kickoff": 0, "skipped": 0}

    async with AsyncSessionLocal() as db:
        bets = await _load_open_bets(db)
        summary["checked"] = len(bets)

        for bet in bets:
            reason = _should_close(bet, now)

            # Stage 1: lineup check — may override reason or mark lineup_checked
            if reason == "lineup_check":
                lineup_close = await _run_lineup_check(db, bet)
                if lineup_close:
                    reason = "lineup_ev_loss"
                else:
                    # Mark as checked; hold until force-close window
                    await _mark_lineup_checked(db, bet)
                    continue

            if reason is None:
                continue

            current_price = await _get_current_bid(bet)
            if current_price is None:
                log.warning(
                    "position_price_unavailable",
                    bet_id=str(bet.id)[:8],
                    token_id=(bet.token_id or "")[:8],
                )
                summary["skipped"] += 1
                continue

            closed = await _close_position(bet, current_price, reason)
            if closed:
                await _update_bet_closed(db, bet, current_price, reason, now)
                pnl_usd = (current_price - bet.entry_price) * bet.shares
                summary[f"closed_{reason}"] = summary.get(f"closed_{reason}", 0) + 1
                log.info(
                    "position_closed",
                    bet_id=str(bet.id)[:8],
                    reason=reason,
                    entry=bet.entry_price,
                    exit=current_price,
                    pnl_pct=f"{((current_price - bet.entry_price) / bet.entry_price) * 100:.1f}%",
                    pnl_usd=f"{pnl_usd:.2f}",
                )
                await _notify_position_closed(bet, current_price, reason, pnl_usd)

        await db.commit()

    log.info("position_manager_done", **summary)
    return summary


# ── Decision logic ────────────────────────────────────────────────────────────

def _should_close(bet: BetORM, now: datetime) -> str | None:
    """
    Returns the close reason string, or None if position should stay open.

    Two-stage pre-kickoff logic:
      Stage 1 (lineup_check): fetch lineups + recalculate EV; close if edge gone
      Stage 2 (kickoff):      unconditional force-close
    """
    if not bet.kickoff_utc:
        return "price_check"

    kickoff = bet.kickoff_utc
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    minutes_to_kickoff = (kickoff - now).total_seconds() / 60

    # Stage 2: hard force-close
    if minutes_to_kickoff <= settings.force_close_minutes_before_kickoff:
        return "kickoff"

    # Stage 1: lineup check (runs once per bet)
    if minutes_to_kickoff <= settings.lineup_check_minutes_before_kickoff and not bet.lineup_checked:
        return "lineup_check"

    return "price_check"  # sentinel: caller checks TP/SL against live price


async def _get_current_bid(bet: BetORM) -> float | None:
    """
    Get the current best bid for this position's token.
    Best bid = highest price a buyer will pay = what we'd receive if we SELL.

    Priority:
      1. Mock token simulation (MOCK-* — no real CLOB in paper testing)
      2. WebSocket streamer hot cache (fresh within 15s, zero latency)
      3. CLOB REST orderbook for the single token (slower, always available)
    """
    if not bet.token_id:
        return None

    # 1. Mock tokens have no real orderbook — simulate a bid slightly below entry
    if bet.token_id.startswith("MOCK-"):
        import random
        bid = round(max(0.02, min(0.97, bet.entry_price + random.uniform(-0.04, 0.05))), 4)
        log.debug("position_mock_bid_simulated", token_id=bet.token_id[:12], bid=bid)
        return bid

    # 2. Try WebSocket streamer hot cache
    streamer = get_streamer()
    if streamer:
        snap = streamer.get_cached_book(bet.token_id, max_age_seconds=15.0)
        if snap and snap.best_bid:
            log.debug("position_price_from_ws", token_id=bet.token_id[:8], bid=snap.best_bid)
            return snap.best_bid

    # 3. REST fallback — fetch orderbook for this single token from CLOB
    log.debug("position_price_cache_miss_trying_rest", token_id=bet.token_id[:8])
    bid = fetch_token_best_bid(bet.token_id)
    if bid is not None:
        return bid

    return None


# ── Lineup check (Stage 1) ────────────────────────────────────────────────────

async def _run_lineup_check(db, bet: BetORM) -> bool:
    """
    Fetch confirmed lineups, recalculate EV with lineup-adjusted probabilities.
    Returns True if the position should be closed (edge is gone), False to hold.
    No-ops gracefully on any failure (returns False = hold).
    """
    try:
        async with FootballAPIClient() as client:
            lineups = await client.get_lineups(int(bet.match_id))

        if not lineups.get("home") and not lineups.get("away"):
            log.debug("lineups_not_published_yet", match_id=bet.match_id)
            return False

        # Load the original stored prediction for this match
        from sqlalchemy import select as sa_select
        result = await db.execute(
            sa_select(PredictionORM)
            .where(PredictionORM.match_id == bet.match_id)
            .order_by(PredictionORM.predicted_at.desc())
            .limit(1)
        )
        pred_orm = result.scalar_one_or_none()

        if pred_orm is None:
            log.debug("no_stored_prediction", match_id=bet.match_id)
            return False

        base_pred = ModelPrediction(
            match_id=pred_orm.match_id,
            probabilities=OutcomeProbabilities(
                home=pred_orm.prob_home,
                draw=pred_orm.prob_draw,
                away=pred_orm.prob_away,
                confidence=pred_orm.confidence,
            ),
            model_version=pred_orm.model_version,
        )

        updated_pred = adjust_prediction_for_lineups(base_pred, lineups)

        # Get current market ask price for the token
        current_ask = _get_current_ask(bet)
        if current_ask is None or current_ask <= 0:
            return False

        # Recalculate EV with updated model probabilities
        from sporting_edge.models.schemas import Outcome
        from sporting_edge.agents.odds_analyzer import calculate_ev
        model_prob = updated_pred.probabilities.for_outcome(Outcome(bet.outcome))
        if bet.side.upper() == "NO":
            model_prob = 1.0 - model_prob

        ev_current = calculate_ev(model_prob, current_ask)
        threshold = (
            settings.min_ev_threshold if settings.paper_trading
            else settings.min_ev_threshold_live
        )

        log.info(
            "lineup_check_result",
            bet_id=str(bet.id)[:8],
            ev_current=f"{ev_current:.3f}",
            threshold=threshold,
            close=ev_current < threshold,
            lineup_factors=updated_pred.factors_used[-3:],
        )

        return ev_current < threshold

    except Exception as exc:
        log.warning("lineup_check_error", bet_id=str(bet.id)[:8], error=str(exc))
        return False


def _get_current_ask(bet: BetORM) -> float | None:
    """Best ask for the bet's token — used for EV recalculation."""
    if not bet.token_id:
        return None

    streamer = get_streamer()
    if streamer:
        snap = streamer.get_cached_book(bet.token_id, max_age_seconds=30.0)
        if snap and snap.best_ask:
            return snap.best_ask

    return fetch_token_best_ask(bet.token_id)


async def _mark_lineup_checked(db, bet: BetORM) -> None:
    await db.execute(
        update(BetORM).where(BetORM.id == bet.id).values(lineup_checked=True)
    )
    await db.commit()
    log.debug("lineup_checked_marked", bet_id=str(bet.id)[:8])


# ── Execution ─────────────────────────────────────────────────────────────────

async def _close_position(bet: BetORM, current_bid: float, reason: str) -> bool:
    """
    Decide whether to actually close based on current price, then execute SELL.
    Mirrors OrderExecutor._close_position() from the sibling repo.
    """
    entry = bet.entry_price
    tp_price = entry * (1 + settings.take_profit_pct)
    sl_price = entry * (1 - settings.stop_loss_pct)

    if reason == "price_check":
        if current_bid >= tp_price:
            reason = "tp"   # take-profit triggered
        elif current_bid <= sl_price:
            reason = "sl"   # stop-loss triggered
        else:
            return False    # price within acceptable range, hold

    # For force-close (kickoff) we close regardless of price
    log.info(
        "closing_position",
        bet_id=str(bet.id)[:8],
        reason=reason,
        current_bid=current_bid,
        entry=entry,
    )

    # SELL the shares we hold back into the CLOB
    # For SELL orders: size parameter = number of shares (not USD)
    try:
        place_fak_order(
            token_id=bet.token_id,
            side="SELL",
            size_usd=bet.shares,   # py_clob_client interprets this as shares for SELL
            hint_price=current_bid,
        )
        return True
    except Exception as exc:
        log.error("position_close_failed", bet_id=str(bet.id)[:8], error=str(exc))
        return False


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _load_open_bets(db) -> list[BetORM]:
    result = await db.execute(
        select(BetORM).where(BetORM.status.in_(OPEN_STATUSES))
    )
    return list(result.scalars().all())


async def _update_bet_closed(
    db,
    bet: BetORM,
    close_price: float,
    reason: str,
    now: datetime,
) -> None:
    pnl_usd = (close_price - bet.entry_price) * bet.shares

    await db.execute(
        update(BetORM)
        .where(BetORM.id == bet.id)
        .values(
            status="closed",
            close_price=close_price,
            close_reason=reason,
            settled_at=now,
            pnl_usd=round(pnl_usd, 4),
            settlement_price=close_price,
        )
    )


async def _notify_position_closed(
    bet: BetORM,
    close_price: float,
    reason: str,
    pnl_usd: float,
) -> None:
    if not settings.notifications_enabled:
        return

    reason_labels = {
        "tp": "Take-profit ✅",
        "sl": "Stop-loss 🛑",
        "kickoff": "Pre-kickoff force-close ⏱️",
    }
    label = reason_labels.get(reason, reason)
    emoji = "✅" if pnl_usd >= 0 else "🔴"
    pct = ((close_price - bet.entry_price) / bet.entry_price) * 100

    text = (
        f"*{emoji} Position Closed — {label}*\n\n"
        f"Market: `{(bet.market_question or '')[:60]}`\n"
        f"Entry: `{bet.entry_price:.3f}` → Exit: `{close_price:.3f}` ({pct:+.1f}%)\n"
        f"P&L: `${pnl_usd:+.2f}`\n"
        f"Mode: `{'PAPER' if bet.paper_trade else 'LIVE'}`"
    )

    try:
        from telegram import Bot
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            parse_mode="Markdown",
        )
    except Exception as exc:
        log.warning("telegram_position_notify_failed", error=str(exc))
