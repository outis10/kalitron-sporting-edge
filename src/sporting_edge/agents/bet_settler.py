"""
BetSettler
==========
Automatically settles bets once a match has finished.

Flow (runs every 30 min via APScheduler):
  1. Load all open/paper bets whose kickoff_utc is > 2h ago
     (2h buffer: 90min match + 30min extra for delays / Polymarket resolution)
  2. For each unsettled bet, fetch the fixture result from API-Football
  3. Determine WIN/LOSS based on final score vs bet's outcome + side
  4. Update BetORM: status, pnl_usd, settlement_price, settled_at
  5. Send Telegram notification with session P&L summary

P&L formula (same for YES and NO sides):
  pnl = shares × (settlement_price - entry_price)
  where settlement_price = 1.0 (win) or 0.0 (loss)

  e.g. Bought YES Bayern at 0.45, Bayern wins:
       pnl = shares × (1.0 - 0.45) = shares × 0.55 ✅

  e.g. Bought YES Bayern at 0.45, Bayern loses:
       pnl = shares × (0.0 - 0.45) = -shares × 0.45 ❌
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.db.models import BetORM
from sporting_edge.db.session import AsyncSessionLocal
from sporting_edge.models.schemas import MatchStatus, Outcome
from sporting_edge.tools.football_api import FootballAPIClient
from sporting_edge.tools.polymarket_tools import get_polymarket_market_resolution

log = get_logger(__name__)

# How long after kickoff we wait before attempting settlement
# (covers 90min match + 30min Polymarket resolution lag)
SETTLE_DELAY_HOURS = 2


async def run_bet_settler() -> dict:
    """
    Main entry point — called by APScheduler every 30 minutes.
    Returns a summary dict for logging/monitoring.
    """
    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(hours=SETTLE_DELAY_HOURS)

    summary = {"checked": 0, "settled_win": 0, "settled_loss": 0, "skipped": 0, "total_pnl": 0.0}

    async with AsyncSessionLocal() as db:
        bets = await _load_unsettled_bets(db, cutoff)
        summary["checked"] = len(bets)

        if not bets:
            log.debug("bet_settler_nothing_to_settle")
            return summary

        async with FootballAPIClient() as client:
            for bet in bets:
                api_result = await _get_fixture_result(client, bet.match_id)
                poly_result = await get_polymarket_market_resolution(bet.condition_id)

                reconciled = _reconcile_settlement(api_result, poly_result, bet)
                if reconciled is None:
                    summary["skipped"] += 1
                    continue

                settlement_price, source = reconciled
                pnl = round(bet.shares * (settlement_price - bet.entry_price), 4)
                status = "won" if settlement_price == 1.0 else "lost"

                await db.execute(
                    update(BetORM)
                    .where(BetORM.id == bet.id)
                    .values(
                        status=status,
                        settlement_price=settlement_price,
                        settlement_source=source,
                        pnl_usd=pnl,
                        settled_at=now,
                    )
                )

                summary["total_pnl"] += pnl
                summary[f"settled_{status}"] = summary.get(f"settled_{status}", 0) + 1

                actual_outcome = api_result[0] if api_result else "unknown"
                score = f"{api_result[1]}-{api_result[2]}" if api_result else "n/a"
                log.info(
                    "bet_settled",
                    bet_id=str(bet.id)[:8],
                    match_id=bet.match_id,
                    outcome=actual_outcome,
                    score=score,
                    bet_outcome=bet.outcome,
                    bet_side=bet.side,
                    settlement=settlement_price,
                    source=source,
                    pnl=pnl,
                    paper=bet.paper_trade,
                )

        await db.commit()

    # Telegram summary if anything was settled
    settled_total = summary.get("settled_won", 0) + summary.get("settled_lost", 0)
    if settled_total > 0:
        await _notify_settlement(summary)

    log.info("bet_settler_done", **{k: v for k, v in summary.items() if k != "total_pnl"}, total_pnl=f"{summary['total_pnl']:.2f}")
    return summary


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _load_unsettled_bets(db, cutoff: datetime) -> list[BetORM]:
    """Open/paper bets whose kickoff was more than SETTLE_DELAY_HOURS ago."""
    result = await db.execute(
        select(BetORM).where(
            BetORM.status.in_(["open", "paper"]),
            BetORM.kickoff_utc.is_not(None),
            BetORM.kickoff_utc <= cutoff,
        )
    )
    return list(result.scalars().all())


# ── API-Football result fetch ──────────────────────────────────────────────────

async def _get_fixture_result(
    client: FootballAPIClient,
    fixture_id: str,
) -> tuple[str, int, int] | None:
    """
    Returns (actual_outcome, home_goals, away_goals) if match is finished, else None.
    actual_outcome is one of: 'home', 'draw', 'away'
    """
    try:
        data = await client._get("/fixtures", {"id": fixture_id})
        fixtures = data.get("response", [])
        if not fixtures:
            return None

        fix = fixtures[0]
        status_short = fix.get("fixture", {}).get("status", {}).get("short", "")

        # Only settle finished matches
        finished_statuses = {"FT", "AET", "PEN", "AWD", "WO"}
        if status_short not in finished_statuses:
            log.debug("match_not_finished_yet", fixture_id=fixture_id, status=status_short)
            return None

        goals = fix.get("goals", {})
        home_goals = goals.get("home") or 0
        away_goals = goals.get("away") or 0

        if home_goals > away_goals:
            outcome = Outcome.HOME.value
        elif home_goals < away_goals:
            outcome = Outcome.AWAY.value
        else:
            outcome = Outcome.DRAW.value

        return outcome, int(home_goals), int(away_goals)

    except Exception as exc:
        log.warning("fixture_result_fetch_failed", fixture_id=fixture_id, error=str(exc))
        return None


# ── Settlement logic ──────────────────────────────────────────────────────────

def _reconcile_settlement(
    api_result: tuple | None,
    poly_result: dict | None,
    bet: "BetORM",
) -> tuple[float, str] | None:
    """
    Cross-validate API-Football and Polymarket resolution.

    Returns (settlement_price, source) or None if data is insufficient/conflicting.

    Source labels:
      'both'         — both sources agree
      'api_football' — only API-Football has a result
      'polymarket'   — only Polymarket resolved (e.g. API-Football lagging)
      None           — skip this cycle (no data or conflict)
    """
    api_price: float | None = None
    poly_price: float | None = None

    if api_result:
        actual_outcome, _, _ = api_result
        api_price = _determine_settlement(bet.outcome, bet.side, actual_outcome)

    if poly_result and poly_result.get("resolved"):
        winner = poly_result.get("winner")
        if winner == "YES":
            poly_price = 1.0 if bet.side.upper() == "YES" else 0.0
        elif winner == "NO":
            poly_price = 0.0 if bet.side.upper() == "YES" else 1.0

    if api_price is not None and poly_price is not None:
        if api_price != poly_price:
            log.warning(
                "settlement_source_conflict",
                bet_id=str(bet.id)[:8],
                condition_id=bet.condition_id[:16],
                api_price=api_price,
                poly_price=poly_price,
            )
            return None  # disagreement — skip, retry next cycle
        return api_price, "both"

    if poly_price is not None:
        # Polymarket resolved but API-Football hasn't confirmed yet
        return poly_price, "polymarket"

    if api_price is not None:
        return api_price, "api_football"

    return None  # neither source has data


def _determine_settlement(
    bet_outcome: str,   # 'home' | 'draw' | 'away'
    bet_side: str,      # 'YES' | 'NO'
    actual_outcome: str,
) -> float:
    """
    1.0 = bet won (shares worth $1), 0.0 = bet lost (shares worth $0).

    YES bet on HOME → wins if actual_outcome == 'home'
    NO  bet on HOME → wins if actual_outcome != 'home' (draw or away)
    """
    outcome_happened = (bet_outcome == actual_outcome)

    if bet_side.upper() == "YES":
        return 1.0 if outcome_happened else 0.0
    else:  # NO
        return 1.0 if not outcome_happened else 0.0


# ── Telegram notification ─────────────────────────────────────────────────────

async def _notify_settlement(summary: dict) -> None:
    if not settings.notifications_enabled:
        return

    won = summary.get("settled_won", 0)
    lost = summary.get("settled_lost", 0)
    pnl = summary["total_pnl"]
    emoji = "✅" if pnl >= 0 else "❌"

    text = (
        f"*{emoji} Bets Settled*\n\n"
        f"Won: `{won}` | Lost: `{lost}`\n"
        f"Session P&L: `${pnl:+.2f}`"
    )
    await _send_telegram(text)


async def _send_telegram(text: str) -> None:
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
