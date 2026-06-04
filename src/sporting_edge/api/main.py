"""
FastAPI application entry point.
Also manages the APScheduler background jobs.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sporting_edge.agents.bet_settler import run_bet_settler
from sporting_edge.agents.clv_tracker import run_clv_tracker
from sporting_edge.agents.position_manager import run_position_manager
from sporting_edge.api.routers import markets, pipeline, positions
from sporting_edge.config import settings
from sporting_edge.config.logging import configure_logging, get_logger
from sporting_edge.graph.orchestrator import run_pipeline
from sporting_edge.tools.polymarket_streamer import init_streamer, get_streamer

configure_logging()
log = get_logger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start scheduler and WebSocket streamer; stop on shutdown."""
    log.info(
        "sporting_edge_startup",
        environment=settings.environment,
        paper_trading=settings.paper_trading,
        leagues=settings.active_league_ids,
    )

    # Signal pipeline — runs every N minutes to find new bets
    scheduler.add_job(
        _scheduled_run,
        trigger="interval",
        minutes=settings.signal_scan_minutes,
        id="pipeline_run",
        replace_existing=True,
    )
    # Position manager — monitors open positions for TP/SL/force-close
    scheduler.add_job(
        _scheduled_position_check,
        trigger="interval",
        minutes=settings.position_check_minutes,
        id="position_check",
        replace_existing=True,
    )
    # Bet settler — resolves bets after match ends using API-Football results
    scheduler.add_job(
        _scheduled_settle,
        trigger="interval",
        minutes=30,
        id="bet_settler",
        replace_existing=True,
    )
    # CLV tracker — captures closing price ~70-90 min before kickoff
    scheduler.add_job(
        _scheduled_clv_capture,
        trigger="interval",
        minutes=settings.position_check_minutes,  # same cadence as position check (5 min)
        id="clv_tracker",
        replace_existing=True,
    )
    scheduler.start()
    log.info(
        "scheduler_started",
        signal_interval_min=settings.signal_scan_minutes,
        position_interval_min=settings.position_check_minutes,
        settler_interval_min=30,
        clv_interval_min=settings.position_check_minutes,
    )

    # Start Polymarket WebSocket streamer in the background.
    # It begins with an empty asset list; DataCollector adds token_ids
    # after discovering markets via Gamma API.
    streamer = init_streamer(asset_ids=[])
    streamer_task = asyncio.create_task(streamer.start())
    log.info("polymarket_streamer_started")

    yield

    # Graceful shutdown
    streamer_task.cancel()
    await streamer.stop()
    scheduler.shutdown(wait=False)
    log.info("sporting_edge_shutdown")


async def _scheduled_run() -> None:
    """Called by APScheduler — runs the full signal pipeline."""
    try:
        await run_pipeline(triggered_by="scheduler")
    except Exception as exc:
        log.error("scheduled_run_failed", error=str(exc))


async def _scheduled_position_check() -> None:
    """Called by APScheduler — checks open positions for TP/SL/force-close."""
    try:
        await run_position_manager()
    except Exception as exc:
        log.error("position_check_failed", error=str(exc))


async def _scheduled_settle() -> None:
    """Called by APScheduler — settles bets for finished matches."""
    try:
        await run_bet_settler()
    except Exception as exc:
        log.error("bet_settler_failed", error=str(exc))


async def _scheduled_clv_capture() -> None:
    """Called by APScheduler — captures closing price for bets near kickoff."""
    try:
        await run_clv_tracker()
    except Exception as exc:
        log.error("clv_tracker_failed", error=str(exc))


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Kalitron Sporting Edge",
    description="Football value betting system for Polymarket",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(pipeline.router)
app.include_router(markets.router)
app.include_router(positions.router)


@app.get("/health")
async def health():
    streamer = get_streamer()
    ws_health = streamer.health() if streamer else {"connected": False}
    return {
        "status": "ok",
        "paper_trading": settings.paper_trading,
        "execute_trades": settings.execute_trades,
        "leagues": settings.active_league_ids,
        "environment": settings.environment,
        "websocket": ws_health,
    }


@app.get("/config")
async def get_config():
    """Return non-sensitive configuration for debugging."""
    return {
        "bankroll_usd": settings.bankroll_usd,
        "max_kelly_fraction": settings.max_kelly_fraction,
        "max_bet_pct_bankroll": settings.max_bet_pct_bankroll,
        "min_ev_threshold": settings.min_ev_threshold,
        "min_model_confidence": settings.min_model_confidence,
        "min_market_liquidity": settings.min_market_liquidity,
        "daily_loss_limit_usd": settings.daily_loss_limit_usd,
        "active_leagues": settings.active_league_ids,
        "signal_scan_minutes": settings.signal_scan_minutes,
    }
