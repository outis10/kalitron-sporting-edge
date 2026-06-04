"""
Portfolio / position endpoints — view bets, P&L, performance.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from sporting_edge.db.models import BetORM, DailyPerformanceORM
from sporting_edge.db.session import get_db

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("/bets")
async def list_bets(
    paper: Optional[bool] = None,
    status: Optional[str] = None,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List all bets (paper and/or live)."""
    stmt = select(BetORM).order_by(desc(BetORM.placed_at)).limit(limit)
    if paper is not None:
        stmt = stmt.where(BetORM.paper_trade == paper)
    if status:
        stmt = stmt.where(BetORM.status == status)

    result = await db.execute(stmt)
    bets = result.scalars().all()

    return [
        {
            "id": str(b.id),
            "signal_id": str(b.signal_id),
            "match_id": b.match_id,
            "market_question": b.market_question,
            "outcome": b.outcome,
            "side": b.side,
            "entry_price": b.entry_price,
            "size_usd": b.size_usd,
            "shares": b.shares,
            "paper_trade": b.paper_trade,
            "status": b.status,
            "pnl_usd": b.pnl_usd,
            "placed_at": b.placed_at.isoformat(),
            "settled_at": b.settled_at.isoformat() if b.settled_at else None,
        }
        for b in bets
    ]


@router.get("/performance")
async def get_performance(
    paper: bool = True,
    days: int = Query(30, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Return daily performance records."""
    result = await db.execute(
        select(DailyPerformanceORM)
        .where(DailyPerformanceORM.is_paper == paper)
        .order_by(desc(DailyPerformanceORM.date))
        .limit(days)
    )
    rows = result.scalars().all()

    return [
        {
            "date": r.date.isoformat(),
            "is_paper": r.is_paper,
            "bets_placed": r.bets_placed,
            "bets_won": r.bets_won,
            "bets_lost": r.bets_lost,
            "gross_pnl_usd": r.gross_pnl_usd,
            "bankroll_end_usd": r.bankroll_end_usd,
            "roi_pct": r.roi_pct,
        }
        for r in rows
    ]


@router.patch("/bets/{bet_id}/settle")
async def settle_bet(
    bet_id: str,
    settlement_price: float,
    db: AsyncSession = Depends(get_db),
):
    """
    Manually settle a bet (for paper trading or if Polymarket resolution is delayed).
    settlement_price: 1.0 = YES resolved, 0.0 = NO resolved
    """
    from datetime import datetime, timezone
    from sqlalchemy import update
    import uuid

    result = await db.execute(
        select(BetORM).where(BetORM.id == uuid.UUID(bet_id))
    )
    bet = result.scalar_one_or_none()
    if not bet:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Bet not found")

    # P&L calculation
    if bet.side == "YES":
        pnl = (settlement_price - bet.entry_price) * bet.shares
    else:
        pnl = ((1 - settlement_price) - bet.entry_price) * bet.shares

    new_status = "won" if pnl > 0 else "lost"

    await db.execute(
        update(BetORM)
        .where(BetORM.id == uuid.UUID(bet_id))
        .values(
            status=new_status,
            settlement_price=settlement_price,
            pnl_usd=round(pnl, 4),
            settled_at=datetime.now(tz=timezone.utc),
        )
    )

    return {"bet_id": bet_id, "status": new_status, "pnl_usd": round(pnl, 4)}
