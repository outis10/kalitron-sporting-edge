"""
ReportAgent
===========
Sends Telegram notifications and writes daily performance summaries.
Runs as the final node in the LangGraph pipeline.

Telegram messages are optional (disabled if TELEGRAM_BOT_TOKEN not set).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.db.models import BetORM, DailyPerformanceORM
from sporting_edge.db.session import AsyncSessionLocal
from sporting_edge.models.schemas import AgentState, BetRecord, MarketSignal

log = get_logger(__name__)


async def report_agent_node(state: AgentState) -> AgentState:
    """LangGraph node: notify + write daily performance."""
    log.info("report_agent_start", run_id=state.run_id)

    async with AsyncSessionLocal() as db:
        await _write_daily_performance(db)
        await db.commit()

    if state.signals:
        await _notify_signals(state.signals)

    if state.bets_placed:
        await _notify_bets(state.bets_placed)

    state.completed_nodes.append("report_agent")
    log.info("report_agent_done", run_id=state.run_id)
    return state


# ── Telegram ──────────────────────────────────────────────────────────────────

async def _notify_signals(signals: list[MarketSignal]) -> None:
    """Send a Telegram message for each new strong/moderate signal."""
    if not settings.notifications_enabled:
        return

    filtered = [s for s in signals if s.signal_strength.value in ("strong", "moderate")]
    if not filtered:
        return

    lines = ["*🎯 New Value Signals*\n"]
    for sig in filtered[:5]:   # cap at 5 to avoid message spam
        emoji = "🔥" if sig.signal_strength.value == "strong" else "📊"
        lines.append(
            f"{emoji} *{sig.match.home_team.name} vs {sig.match.away_team.name}*\n"
            f"   Outcome: `{sig.target_outcome.value.upper()}` ({sig.bet_side.value})\n"
            f"   EV: `{sig.expected_value:.1%}` | Edge: `{sig.edge:.1%}`\n"
            f"   Model: `{sig.model_probability:.1%}` vs Market: `{sig.market_probability:.1%}`\n"
            f"   Kickoff: `{sig.match.kickoff_utc.strftime('%Y-%m-%d %H:%M')} UTC`\n"
        )

    await _send_telegram("\n".join(lines))


async def _notify_bets(bets: list[BetRecord]) -> None:
    """Send a Telegram summary of bets placed in this run."""
    if not settings.notifications_enabled:
        return

    mode = "PAPER" if bets[0].paper_trade else "LIVE"
    lines = [f"*💰 Bets Placed [{mode}]*\n"]
    total = 0.0

    for bet in bets:
        lines.append(
            f"• `{bet.market_question[:60]}` — ${bet.size_usd:.2f} @ {bet.entry_price:.2f}\n"
        )
        total += bet.size_usd

    lines.append(f"\n_Total exposure: ${total:.2f}_")
    await _send_telegram("\n".join(lines))


async def _send_telegram(text: str) -> None:
    """Fire-and-forget Telegram message."""
    try:
        from telegram import Bot
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            parse_mode="Markdown",
        )
    except Exception as exc:
        log.warning("telegram_send_failed", error=str(exc))


# ── Daily performance ─────────────────────────────────────────────────────────

async def _write_daily_performance(db) -> None:
    """Aggregate today's bets and upsert into daily_performance."""
    today_start = datetime.now(tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    for is_paper in (True, False):
        result = await db.execute(
            select(
                func.count(BetORM.id),
                func.count(BetORM.id).filter(BetORM.status == "won"),
                func.count(BetORM.id).filter(BetORM.status == "lost"),
                func.coalesce(func.sum(BetORM.pnl_usd), 0.0),
                func.coalesce(func.sum(BetORM.size_usd), 0.0),
            ).where(
                BetORM.placed_at >= today_start,
                BetORM.paper_trade == is_paper,
            )
        )
        row = result.fetchone()
        if not row or row[0] == 0:
            continue

        total, won, lost, pnl, invested = row
        roi = (pnl / invested * 100) if invested > 0 else 0.0

        stmt = pg_insert(DailyPerformanceORM).values(
            date=today_start.date(),
            is_paper=is_paper,
            bets_placed=total,
            bets_won=won,
            bets_lost=lost,
            gross_pnl_usd=float(pnl),
            bankroll_end_usd=settings.bankroll_usd + float(pnl),
            roi_pct=float(roi),
        ).on_conflict_do_update(
            constraint="uq_daily_perf",
            set_={
                "bets_placed": total,
                "bets_won": won,
                "bets_lost": lost,
                "gross_pnl_usd": float(pnl),
                "bankroll_end_usd": settings.bankroll_usd + float(pnl),
                "roi_pct": float(roi),
            },
        )
        await db.execute(stmt)
