"""
Market monitoring endpoints — view signals, matches, odds.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from sporting_edge.db.models import MarketOddsORM, MatchORM, SignalORM
from sporting_edge.db.session import get_db

router = APIRouter(prefix="/markets", tags=["markets"])


@router.get("/matches")
async def list_matches(
    league_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List upcoming/recent matches."""
    stmt = select(MatchORM).order_by(desc(MatchORM.kickoff_utc)).limit(limit)
    if league_id:
        stmt = stmt.where(MatchORM.league_id == league_id)
    if status:
        stmt = stmt.where(MatchORM.status == status)

    result = await db.execute(stmt)
    matches = result.scalars().all()

    return [
        {
            "id": m.id,
            "league_id": m.league_id,
            "home_team_id": m.home_team_id,
            "away_team_id": m.away_team_id,
            "kickoff_utc": m.kickoff_utc.isoformat(),
            "status": m.status,
            "venue": m.venue,
            "home_goals": m.home_goals,
            "away_goals": m.away_goals,
            "result_outcome": m.result_outcome,
        }
        for m in matches
    ]


@router.get("/signals")
async def list_signals(
    acted_on: Optional[bool] = None,
    strength: Optional[str] = None,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List value signals found by OddsAnalyzer."""
    stmt = select(SignalORM).order_by(desc(SignalORM.created_at)).limit(limit)
    if acted_on is not None:
        stmt = stmt.where(SignalORM.acted_on == acted_on)
    if strength:
        stmt = stmt.where(SignalORM.signal_strength == strength)

    result = await db.execute(stmt)
    signals = result.scalars().all()

    return [
        {
            "id": str(s.id),
            "match_id": s.match_id,
            "condition_id": s.condition_id,
            "target_outcome": s.target_outcome,
            "bet_side": s.bet_side,
            "model_probability": s.model_probability,
            "market_probability": s.market_probability,
            "expected_value": s.expected_value,
            "edge": s.edge,
            "signal_strength": s.signal_strength,
            "acted_on": s.acted_on,
            "created_at": s.created_at.isoformat(),
        }
        for s in signals
    ]


@router.get("/odds/{match_id}")
async def get_match_odds(match_id: str, db: AsyncSession = Depends(get_db)):
    """Get latest odds snapshots for a specific match."""
    result = await db.execute(
        select(MarketOddsORM)
        .where(MarketOddsORM.match_id == match_id)
        .order_by(desc(MarketOddsORM.fetched_at))
        .limit(20)
    )
    odds_list = result.scalars().all()

    return [
        {
            "id": str(o.id),
            "condition_id": o.condition_id,
            "market_question": o.market_question,
            "outcome": o.outcome,
            "yes_price": o.yes_price,
            "no_price": o.no_price,
            "liquidity": o.liquidity,
            "volume_24h": o.volume_24h,
            "fetched_at": o.fetched_at.isoformat(),
        }
        for o in odds_list
    ]
