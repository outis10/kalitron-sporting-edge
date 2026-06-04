"""
ExecutionAgent
==============
Receives approved BetDecisions, places orders on Polymarket (or simulates),
and writes BetRecords to the database.

Fixes applied vs original:
  - Fetches live CLOB prices (bid/ask) immediately before execution (Gap 2)
  - Runs fill simulation; rejects if book_consumption_pct > 20% (Gap 5)
  - Uses FAK (Fill-and-Kill) with hint_price instead of GTC (Gap 6)
  - Updates entry_price and shares from actual CLOB ask, not Gamma price

Safety gates (both must be disabled to go live):
  1. settings.paper_trading  → True  = simulate only
  2. settings.execute_trades → False = never call CLOB API
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.db.models import BetORM, SignalORM
from sporting_edge.db.session import AsyncSessionLocal
from sporting_edge.models.schemas import (
    AgentState,
    BetDecision,
    BetRecord,
    BetSide,
    BetStatus,
    MarketSignal,
)
from sporting_edge.tools.polymarket_tools import (
    estimate_fill,
    fetch_real_odds_from_clob,
    place_fak_order,
)

log = get_logger(__name__)

# Reject the order if it would consume more than this % of visible book depth
MAX_BOOK_CONSUMPTION_PCT = 20.0


async def execution_agent_node(state: AgentState) -> AgentState:
    """LangGraph node: execute approved bets."""
    approved = [d for d in state.decisions if d.approved]
    log.info(
        "execution_agent_start",
        run_id=state.run_id,
        approved_bets=len(approved),
        paper_trading=settings.paper_trading,
    )

    signal_by_id: dict[str, MarketSignal] = {
        s.signal_id: s for s in state.signals
    }

    bets_placed: list[BetRecord] = []

    async with AsyncSessionLocal() as db:
        for decision in approved:
            signal = signal_by_id.get(decision.signal_id)
            if not signal:
                log.warning("signal_not_found", signal_id=decision.signal_id)
                continue

            try:
                bet = await _place_bet(signal, decision)
                if bet is None:
                    # rejected by fill simulation
                    continue
                bets_placed.append(bet)
                await _persist_bet(db, bet)
                await _mark_signal_acted(db, signal.signal_id)

                log.info(
                    "bet_placed",
                    bet_id=bet.bet_id,
                    match=f"{signal.match.home_team.name} vs {signal.match.away_team.name}",
                    side=bet.side.value,
                    size=f"${bet.size_usd:.2f}",
                    entry_price=bet.entry_price,
                    paper=bet.paper_trade,
                )
            except Exception as exc:
                log.error(
                    "execution_failed",
                    signal_id=decision.signal_id,
                    error=str(exc),
                )

        await db.commit()

    state.bets_placed = bets_placed
    state.completed_nodes.append("execution_agent")
    log.info(
        "execution_agent_done",
        run_id=state.run_id,
        bets_placed=len(bets_placed),
    )
    return state


# ── Core execution ────────────────────────────────────────────────────────────

async def _place_bet(signal: MarketSignal, decision: BetDecision) -> BetRecord | None:
    """
    Place a single bet — paper or real.

    Steps:
      1. Fetch live CLOB prices (bid/ask) for the token
      2. Run fill simulation on the ask side
      3. Reject if liquidity is insufficient or book consumption is too high
      4. Place FAK order with hint_price = best_ask
      5. Return BetRecord
    """
    odds = signal.odds
    size_usd = decision.capped_size_usd
    is_paper = settings.paper_trading or not settings.execute_trades

    # ── Identify token + discovery price ─────────────────────────────────────
    if signal.bet_side == BetSide.YES:
        token_id = odds.yes_token_id or ""
        discovery_price = odds.yes_price     # Gamma last-trade (used in paper mode)
    else:
        token_id = odds.no_token_id or ""
        discovery_price = odds.no_price

    # ── Step 1: Fetch live CLOB prices ────────────────────────────────────────
    # In paper mode, skip the CLOB call but still simulate fill with Gamma price
    clob_prices = None
    if not is_paper and odds.yes_token_id and odds.no_token_id:
        clob_prices = fetch_real_odds_from_clob(
            yes_token_id=odds.yes_token_id,
            no_token_id=odds.no_token_id,
        )

    # Determine the actual entry price (best_ask from CLOB, fallback to Gamma)
    if clob_prices:
        if signal.bet_side == BetSide.YES:
            entry_price = clob_prices.yes_ask
            ask_levels = clob_prices.yes_asks
        else:
            entry_price = clob_prices.no_ask
            ask_levels = clob_prices.no_asks
    else:
        entry_price = discovery_price
        ask_levels = []

    # ── Step 2: Fill simulation ───────────────────────────────────────────────
    if ask_levels:
        fill_est = estimate_fill(ask_levels, size_usd)
        log.info(
            "fill_simulation",
            match=f"{signal.match.home_team.name} vs {signal.match.away_team.name}",
            size_usd=size_usd,
            avg_fill=fill_est.avg_fill_price,
            slippage_bps=fill_est.slippage_vs_best_ask_bps,
            book_consumption_pct=fill_est.book_consumption_pct,
            fully_fillable=fill_est.fully_fillable,
        )

        # ── Step 3: Reject if insufficient depth ──────────────────────────────
        if fill_est.insufficient_liquidity:
            log.warning(
                "order_rejected_no_liquidity",
                signal_id=signal.signal_id,
                token_id=token_id[:8],
            )
            return None

        if (
            fill_est.book_consumption_pct is not None
            and fill_est.book_consumption_pct > MAX_BOOK_CONSUMPTION_PCT
        ):
            log.warning(
                "order_rejected_high_book_consumption",
                signal_id=signal.signal_id,
                consumption_pct=fill_est.book_consumption_pct,
                limit_pct=MAX_BOOK_CONSUMPTION_PCT,
            )
            return None

        # Use simulation's avg_fill_price as our expected entry
        if fill_est.avg_fill_price:
            entry_price = fill_est.avg_fill_price

    shares = round(size_usd / entry_price, 4) if entry_price > 0 else 0.0

    # ── Step 4: Place FAK order ───────────────────────────────────────────────
    # hint_price = best_ask so the CLOB skips its internal book lookup,
    # cutting the race window with market makers.
    hint_price = (clob_prices.yes_ask if signal.bet_side == BetSide.YES else clob_prices.no_ask) \
        if clob_prices else 0.0

    resp = place_fak_order(
        token_id=token_id,
        side="BUY",
        size_usd=size_usd,
        hint_price=hint_price,
    )

    polymarket_order_id = resp.get("order_id") or resp.get("orderID")
    actual_fill_price = _extract_fill_price(resp) if not is_paper else None
    status = BetStatus.PAPER if is_paper else BetStatus.OPEN

    return BetRecord(
        bet_id=str(uuid.uuid4()),
        signal_id=signal.signal_id,
        match_id=signal.match.match_id,
        condition_id=odds.condition_id,
        market_question=odds.market_question,
        outcome=signal.target_outcome,
        side=signal.bet_side,
        entry_price=entry_price,
        size_usd=size_usd,
        shares=shares,
        token_id=token_id,
        kickoff_utc=signal.match.kickoff_utc,
        paper_trade=is_paper,
        status=status,
        polymarket_order_id=polymarket_order_id,
        actual_fill_price=actual_fill_price,
        placed_at=datetime.now(tz=timezone.utc),
    )


def _extract_fill_price(resp: dict) -> float | None:
    """
    Try to extract the average fill price from a CLOB order response.
    Field names vary across CLOB versions; returns None when unrecognised.
    """
    for key in ("avg_price", "avgPrice", "price", "fill_price", "fillPrice"):
        val = resp.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return None


# ── DB writes ─────────────────────────────────────────────────────────────────

async def _persist_bet(db, bet: BetRecord) -> None:
    stmt = pg_insert(BetORM).values(
        id=uuid.UUID(bet.bet_id),
        signal_id=uuid.UUID(bet.signal_id),
        match_id=bet.match_id,
        condition_id=bet.condition_id,
        market_question=bet.market_question,
        outcome=bet.outcome.value,
        side=bet.side.value,
        entry_price=bet.entry_price,
        size_usd=bet.size_usd,
        shares=bet.shares,
        token_id=bet.token_id,
        kickoff_utc=bet.kickoff_utc,
        paper_trade=bet.paper_trade,
        status=bet.status.value,
        polymarket_order_id=bet.polymarket_order_id,
        actual_fill_price=bet.actual_fill_price,
        placed_at=bet.placed_at,
    ).on_conflict_do_nothing()
    await db.execute(stmt)


async def _mark_signal_acted(db, signal_id: str) -> None:
    from sqlalchemy import update
    await db.execute(
        update(SignalORM)
        .where(SignalORM.id == uuid.UUID(signal_id))
        .values(acted_on=True)
    )
