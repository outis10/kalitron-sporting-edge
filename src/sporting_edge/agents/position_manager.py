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

from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.db.models import BetORM
from sporting_edge.db.session import AsyncSessionLocal
from sporting_edge.tools.polymarket_streamer import get_streamer
from sporting_edge.tools.polymarket_tools import fetch_token_best_bid, place_fak_order

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
            reason = await _should_close(bet, now)
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

async def _should_close(bet: BetORM, now: datetime) -> str | None:
    """
    Returns the close reason string, or None if position should stay open.
    Mirrors polymarket-trading-system PositionManager.should_close_position().
    """
    # Force-close: kickoff is imminent — model is no longer valid post-kickoff
    if bet.kickoff_utc:
        kickoff = bet.kickoff_utc
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=timezone.utc)
        minutes_to_kickoff = (kickoff - now).total_seconds() / 60
        if minutes_to_kickoff <= settings.force_close_minutes_before_kickoff:
            return "kickoff"

    # Price-based checks require a current price — fetched by caller
    # We return the reason here; caller fetches price then calls _close_position
    # Pre-check: we can only estimate based on entry price thresholds
    # (actual current price fetched by _get_current_bid in caller)
    return "price_check"  # sentinel: caller will verify actual price


async def _get_current_bid(bet: BetORM) -> float | None:
    """
    Get the current best bid for this position's token.
    Best bid = highest price a buyer will pay = what we'd receive if we SELL.

    Priority:
      1. WebSocket streamer hot cache (fresh within 15s, zero latency)
      2. CLOB REST orderbook for the single token (slower, always available)
    """
    if not bet.token_id:
        return None

    # 1. Try WebSocket streamer hot cache
    streamer = get_streamer()
    if streamer:
        snap = streamer.get_cached_book(bet.token_id, max_age_seconds=15.0)
        if snap and snap.best_bid:
            log.debug("position_price_from_ws", token_id=bet.token_id[:8], bid=snap.best_bid)
            return snap.best_bid

    # 2. REST fallback — fetch orderbook for this single token from CLOB
    log.debug("position_price_cache_miss_trying_rest", token_id=bet.token_id[:8])
    bid = fetch_token_best_bid(bet.token_id)
    if bid is not None:
        return bid

    return None


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
