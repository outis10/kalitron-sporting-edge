"""
CLV Tracker
===========
Captures the closing price of each open bet ~70-90 min before kickoff
and computes Closing Line Value (CLV).

CLV = closing_price - entry_price

  Positive CLV → we entered cheaper than the market's final consensus price.
  This is the primary signal of real edge, detectable with ~50-100 bets
  (far fewer than needed for ROI significance).

Flow (runs every 5 min via APScheduler):
  1. Load open/paper bets whose kickoff is within CLV_WINDOW_MINUTES and
     whose closing_price has not yet been captured (avoids double-write).
  2. For each bet, get the current best ask via:
       a. WebSocket streamer hot cache  (latency: 0ms, max age: 30s)
       b. CLOB REST fallback            (latency: ~17ms)
  3. Store closing_price and clv in BetORM.
  4. Send a Telegram summary when any batch completes.

ADR: docs/adr/ADR-005-clv-metrica-primaria.md
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.db.models import BetORM
from sporting_edge.db.session import AsyncSessionLocal
from sporting_edge.tools.polymarket_streamer import get_streamer
from sporting_edge.tools.polymarket_tools import fetch_token_best_ask

log = get_logger(__name__)

# Capture the closing line when kickoff is within this window.
# 90 min: lineups are published ~60 min before → capture reflects post-lineup market.
CLV_WINDOW_MINUTES = 90


async def run_clv_tracker() -> dict:
    """
    Main entry point — called by APScheduler every 5 min.
    Returns a summary dict for logging/monitoring.
    """
    now = datetime.now(tz=timezone.utc)
    cutoff = now + timedelta(minutes=CLV_WINDOW_MINUTES)

    summary = {
        "checked": 0,
        "captured": 0,
        "skipped_no_price": 0,
        "avg_clv": None,
    }

    async with AsyncSessionLocal() as db:
        bets = await _load_bets_for_clv(db, now, cutoff)
        summary["checked"] = len(bets)

        if not bets:
            log.debug("clv_tracker_nothing_to_capture")
            return summary

        clv_values: list[float] = []

        for bet in bets:
            closing_price = _get_closing_price(bet)
            if closing_price is None:
                log.warning(
                    "clv_closing_price_unavailable",
                    bet_id=str(bet.id)[:8],
                    token_id=(bet.token_id or "")[:8],
                )
                summary["skipped_no_price"] += 1
                continue

            clv = round(closing_price - bet.entry_price, 6)
            clv_values.append(clv)

            await db.execute(
                update(BetORM)
                .where(BetORM.id == bet.id)
                .values(closing_price=closing_price, clv=clv)
            )

            log.info(
                "clv_captured",
                bet_id=str(bet.id)[:8],
                entry=bet.entry_price,
                closing=closing_price,
                clv=f"{clv:+.4f}",
                minutes_to_kick=_minutes_to_kickoff(bet, now),
            )
            summary["captured"] += 1

        await db.commit()

    if clv_values:
        summary["avg_clv"] = round(sum(clv_values) / len(clv_values), 4)
        await _notify_clv_batch(summary, clv_values)

    log.info("clv_tracker_done", **{k: v for k, v in summary.items()})
    return summary


# ── Price resolution ──────────────────────────────────────────────────────────

def _get_closing_price(bet: BetORM) -> float | None:
    """
    Get the current best ask for the bet's token.

    Priority:
      1. WebSocket streamer hot cache  (fresh within 30s)
      2. CLOB REST orderbook
    """
    if not bet.token_id:
        return None

    streamer = get_streamer()
    if streamer:
        snap = streamer.get_cached_book(bet.token_id, max_age_seconds=30.0)
        if snap and snap.best_ask:
            log.debug("clv_price_from_ws", token_id=bet.token_id[:8], ask=snap.best_ask)
            return snap.best_ask

    return fetch_token_best_ask(bet.token_id)


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _load_bets_for_clv(
    db,
    now: datetime,
    cutoff: datetime,
) -> list[BetORM]:
    """
    Open/paper bets whose kickoff is between now and cutoff,
    and whose closing_price has not yet been captured.
    """
    result = await db.execute(
        select(BetORM).where(
            BetORM.status.in_(["open", "paper"]),
            BetORM.kickoff_utc.is_not(None),
            BetORM.kickoff_utc > now,
            BetORM.kickoff_utc <= cutoff,
            BetORM.closing_price.is_(None),
        )
    )
    return list(result.scalars().all())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _minutes_to_kickoff(bet: BetORM, now: datetime) -> float:
    if not bet.kickoff_utc:
        return 0.0
    kickoff = bet.kickoff_utc
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    return (kickoff - now).total_seconds() / 60


# ── Telegram notification ─────────────────────────────────────────────────────

async def _notify_clv_batch(summary: dict, clv_values: list[float]) -> None:
    if not settings.notifications_enabled:
        return

    avg_clv = summary["avg_clv"]
    positive = sum(1 for v in clv_values if v > 0)
    pct_positive = (positive / len(clv_values) * 100) if clv_values else 0
    emoji = "📈" if avg_clv and avg_clv > 0 else "📉"

    text = (
        f"*{emoji} CLV Snapshot*\n\n"
        f"Bets captured: `{summary['captured']}`\n"
        f"Avg CLV: `{avg_clv:+.4f}` ({avg_clv * 100:+.2f}%)\n"
        f"Positive CLV: `{positive}/{len(clv_values)}` ({pct_positive:.0f}%)\n"
        f"Mode: `{'PAPER' if settings.paper_trading else 'LIVE'}`"
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
        log.warning("telegram_clv_notify_failed", error=str(exc))
