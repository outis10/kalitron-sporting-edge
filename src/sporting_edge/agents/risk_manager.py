"""
RiskManager Agent
=================
Applies position sizing and portfolio-level risk controls to raw signals.

Fixes applied vs original (Gap 7):
  - Global order cooldown: min N seconds between any two orders
  - Per-event cooldown: min 60s between orders on the same condition_id
  - Guard records: in-memory log of approved bets this session (with rollback)
  - Session drawdown circuit breaker (not just daily loss)
  - Blocked signal tracking: rejected signals logged for counterfactual analysis

Kelly Criterion:
  f* = (b*p - q) / b   where b = (1/market_price) - 1
  Applied at max_kelly_fraction (Quarter Kelly by default).
  Then hard-capped at max_bet_pct_bankroll (2%).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.db.models import BetORM, MatchORM
from sporting_edge.db.session import AsyncSessionLocal
from sporting_edge.models.schemas import (
    AgentState,
    BetDecision,
    MarketSignal,
)

log = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_BETS_PER_LEAGUE_PER_DAY = 3
GLOBAL_COOLDOWN_SECONDS = 5          # min gap between any two orders
PER_EVENT_COOLDOWN_SECONDS = 120     # min gap between orders on same condition
GUARD_RECORD_TTL_HOURS = 48          # purge guard records older than this
MIN_ORDER_SPEND_USD = 1.0            # Polymarket CLOB minimum: $1 spend
MIN_ORDER_SHARES = 5                 # Polymarket CLOB minimum: 5 shares for limit orders


# ── Guard record ──────────────────────────────────────────────────────────────

@dataclass
class OrderGuardRecord:
    """Tracks an order placed this session for cooldown and exposure checks."""
    condition_id: str
    league_id: int
    match_id: str
    notional_usd: float
    placed_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


# ── Session-level state (persisted in-process across scheduler runs) ──────────

class _GuardState:
    """
    In-memory order guard state.
    Resets on process restart — that's acceptable; DB is the source of truth
    for multi-day stats, this exists purely for intra-session cooldowns.
    """
    def __init__(self) -> None:
        self.records: list[OrderGuardRecord] = []
        self.last_order_at: Optional[datetime] = None

    def clean(self) -> None:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=GUARD_RECORD_TTL_HOURS)
        self.records = [r for r in self.records if r.placed_at >= cutoff]

    def register(self, record: OrderGuardRecord) -> None:
        self.records.append(record)
        self.last_order_at = record.placed_at

    def rollback(self, condition_id: str, placed_at: datetime) -> None:
        self.records = [
            r for r in self.records
            if not (r.condition_id == condition_id and r.placed_at == placed_at)
        ]


_guard = _GuardState()


# ── LangGraph node ────────────────────────────────────────────────────────────

async def risk_manager_node(state: AgentState) -> AgentState:
    """LangGraph node: approve/size/reject each signal."""
    log.info(
        "risk_manager_start",
        run_id=state.run_id,
        signals=len(state.signals),
    )

    async with AsyncSessionLocal() as db:
        daily_loss = await _get_daily_loss(db)
        session_pnl = await _get_session_pnl(db)
        open_condition_ids = await _get_open_condition_ids(db)
        league_bets_today = await _get_league_bets_today(db)

    bankroll = settings.bankroll_usd
    now = datetime.now(tz=timezone.utc)
    _guard.clean()

    decisions: list[BetDecision] = []
    sorted_signals = sorted(state.signals, key=lambda s: s.expected_value, reverse=True)

    # Merge DB open positions + session guards for duplicate check
    processed_conditions: set[str] = set(open_condition_ids)
    # Add conditions already approved in this run
    processed_conditions.update(r.condition_id for r in _guard.records)

    league_count: dict[int, int] = defaultdict(int, league_bets_today)

    for signal in sorted_signals:
        decision = _evaluate_signal(
            signal=signal,
            bankroll=bankroll,
            daily_loss=daily_loss,
            session_pnl=session_pnl,
            processed_conditions=processed_conditions,
            league_count=league_count,
            now=now,
        )
        decisions.append(decision)

        if decision.approved:
            processed_conditions.add(signal.odds.condition_id)
            league_count[signal.match.league.id] += 1
            bankroll -= decision.capped_size_usd

            # Register guard record so subsequent signals in this run respect cooldown
            _guard.register(OrderGuardRecord(
                condition_id=signal.odds.condition_id,
                league_id=signal.match.league.id,
                match_id=signal.match.match_id,
                notional_usd=decision.capped_size_usd,
                placed_at=now,
            ))

            log.info(
                "bet_approved",
                match=f"{signal.match.home_team.name} vs {signal.match.away_team.name}",
                outcome=signal.target_outcome.value,
                ev=f"{signal.expected_value:.1%}",
                size=f"${decision.capped_size_usd:.2f}",
                kelly=f"{decision.kelly_fraction:.3f}",
            )
        else:
            log.debug(
                "bet_rejected",
                signal_id=signal.signal_id,
                reason=decision.rejection_reason,
                match=f"{signal.match.home_team.name} vs {signal.match.away_team.name}",
            )

    state.decisions = decisions
    state.completed_nodes.append("risk_manager")

    approved = sum(1 for d in decisions if d.approved)
    log.info(
        "risk_manager_done",
        run_id=state.run_id,
        approved=approved,
        rejected=len(decisions) - approved,
    )
    return state


# ── Core evaluation logic (pure — testable without DB) ───────────────────────

def _evaluate_signal(
    signal: MarketSignal,
    bankroll: float,
    daily_loss: float,
    session_pnl: float,
    processed_conditions: set[str],
    league_count: dict[int, int],
    now: datetime,
) -> BetDecision:
    base = BetDecision(
        signal_id=signal.signal_id,
        approved=False,
        current_bankroll_usd=bankroll,
        daily_loss_so_far=daily_loss,
    )

    # ── Guard: daily loss limit ───────────────────────────────────────────────
    if daily_loss <= -settings.daily_loss_limit_usd:
        base.rejection_reason = f"daily_loss_limit_reached (${daily_loss:.2f})"
        return base

    # ── Guard: session drawdown circuit breaker ───────────────────────────────
    # Stops trading if session P&L drops below 50% of starting bankroll
    session_drawdown_limit = -(bankroll * 0.50)
    if session_pnl <= session_drawdown_limit:
        base.rejection_reason = (
            f"session_drawdown_circuit_breaker "
            f"(session_pnl=${session_pnl:.2f}, limit=${session_drawdown_limit:.2f})"
        )
        return base

    # ── Guard: global order cooldown ──────────────────────────────────────────
    if _guard.last_order_at is not None:
        elapsed = (now - _guard.last_order_at).total_seconds()
        if elapsed < GLOBAL_COOLDOWN_SECONDS:
            base.rejection_reason = (
                f"global_cooldown_active ({elapsed:.1f}s < {GLOBAL_COOLDOWN_SECONDS}s)"
            )
            return base

    # ── Guard: per-event cooldown ─────────────────────────────────────────────
    cid = signal.odds.condition_id
    recent_same = [
        r for r in _guard.records
        if r.condition_id == cid
        and (now - r.placed_at).total_seconds() < PER_EVENT_COOLDOWN_SECONDS
    ]
    if recent_same:
        base.rejection_reason = (
            f"per_event_cooldown_active (condition_id={cid[:12]}...)"
        )
        return base

    # ── Guard: duplicate condition_id (open position or this run) ────────────
    if cid in processed_conditions:
        base.rejection_reason = "duplicate_condition_id"
        return base

    # ── Guard: league correlation ─────────────────────────────────────────────
    league_id = signal.match.league.id
    bets_in_league = league_count.get(league_id, 0)
    if bets_in_league >= MAX_BETS_PER_LEAGUE_PER_DAY:
        base.rejection_reason = (
            f"league_correlation_limit ({bets_in_league} bets in league {league_id})"
        )
        return base

    # ── Kelly sizing ──────────────────────────────────────────────────────────
    k = kelly_fraction(
        model_prob=signal.model_probability,
        market_price=signal.market_probability,
    )
    if k <= 0:
        base.rejection_reason = "negative_kelly_fraction"
        return base

    fractional_k = k * settings.max_kelly_fraction
    kelly_size = fractional_k * bankroll
    max_size = bankroll * settings.max_bet_pct_bankroll
    capped_size = min(kelly_size, max_size)

    # ── Guard: Polymarket minimums ($1 spend, 5 shares) ──────────────────────
    if capped_size < MIN_ORDER_SPEND_USD:
        base.rejection_reason = f"below_min_spend (${capped_size:.2f} < ${MIN_ORDER_SPEND_USD})"
        return base

    estimated_shares = capped_size / signal.market_probability
    if estimated_shares < MIN_ORDER_SHARES:
        base.rejection_reason = (
            f"below_min_shares ({estimated_shares:.1f} < {MIN_ORDER_SHARES} shares, "
            f"need ${MIN_ORDER_SHARES * signal.market_probability:.2f} min)"
        )
        return base

    base.approved = True
    base.kelly_fraction = fractional_k
    base.recommended_size_usd = round(kelly_size, 2)
    base.capped_size_usd = round(capped_size, 2)
    return base


# ── Kelly formula ─────────────────────────────────────────────────────────────

def kelly_fraction(model_prob: float, market_price: float) -> float:
    """
    f* = (b*p - q) / b
    b = (1/market_price) - 1  (net fractional odds)
    Returns 0.0 when there's no edge.
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0
    b = (1.0 / market_price) - 1.0
    p = model_prob
    q = 1.0 - p
    return max(0.0, (b * p - q) / b)


# ── Guard state access (for ExecutionAgent rollback on failed orders) ─────────

def rollback_guard(condition_id: str, placed_at: datetime) -> None:
    """Remove a guard record if the order failed after approval."""
    _guard.rollback(condition_id, placed_at)


# ── DB queries ────────────────────────────────────────────────────────────────

def _today_start() -> datetime:
    return datetime.now(tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


async def _get_daily_loss(db: AsyncSession) -> float:
    """Sum of today's settled P&L (negative = net loss)."""
    result = await db.execute(
        select(func.coalesce(func.sum(BetORM.pnl_usd), 0.0)).where(
            BetORM.settled_at >= _today_start(),
            BetORM.pnl_usd.is_not(None),
        )
    )
    return float(result.scalar())


async def _get_session_pnl(db: AsyncSession) -> float:
    """
    Sum of ALL unsettled open-bet unrealised P&L this session.
    Simple proxy: sum of (current_value - cost_basis) for open bets.
    We don't have real-time mark-to-market here, so we use 0 for open bets
    and actual pnl for settled bets today.
    """
    # Use the same daily scope — session = today
    result = await db.execute(
        select(func.coalesce(func.sum(BetORM.pnl_usd), 0.0)).where(
            BetORM.placed_at >= _today_start(),
            BetORM.pnl_usd.is_not(None),
        )
    )
    return float(result.scalar())


async def _get_open_condition_ids(db: AsyncSession) -> set[str]:
    """Condition IDs of bets currently open or pending."""
    result = await db.execute(
        select(BetORM.condition_id).where(BetORM.status.in_(["pending", "open", "paper"]))
    )
    return {row[0] for row in result.fetchall()}


async def _get_league_bets_today(db: AsyncSession) -> dict[int, int]:
    """Count of bets placed today per league_id."""
    result = await db.execute(
        select(MatchORM.league_id, func.count(BetORM.id))
        .join(MatchORM, BetORM.match_id == MatchORM.id)
        .where(BetORM.placed_at >= _today_start())
        .group_by(MatchORM.league_id)
    )
    return {row[0]: row[1] for row in result.fetchall()}
