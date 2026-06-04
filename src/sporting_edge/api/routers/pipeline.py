"""
Pipeline control endpoints — trigger runs manually, view run history.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.graph.orchestrator import run_pipeline
from sporting_edge.models.schemas import AgentState

router = APIRouter(prefix="/pipeline", tags=["pipeline"])
log = get_logger(__name__)

# Simple in-memory run history (last 20 runs)
_run_history: list[dict] = []


class TriggerRequest(BaseModel):
    league_ids: list[int] | None = None
    triggered_by: str = "api"


class RunSummary(BaseModel):
    run_id: str
    triggered_by: str
    matches: int
    signals: int
    bets_placed: int
    completed_nodes: list[str]
    error: str | None


@router.post("/trigger", response_model=RunSummary)
async def trigger_pipeline(req: TriggerRequest, background_tasks: BackgroundTasks):
    """
    Manually trigger a full pipeline run.
    Runs in background — returns immediately with run_id.
    """
    initial_state = AgentState(
        target_league_ids=req.league_ids or settings.active_league_ids,
        triggered_by=req.triggered_by,
    )

    async def _run():
        state = await run_pipeline(
            league_ids=req.league_ids,
            triggered_by=req.triggered_by,
        )
        summary = _state_to_summary(state)
        _run_history.append(summary)
        if len(_run_history) > 20:
            _run_history.pop(0)

    background_tasks.add_task(_run)

    return RunSummary(
        run_id=initial_state.run_id,
        triggered_by=req.triggered_by,
        matches=0,
        signals=0,
        bets_placed=0,
        completed_nodes=[],
        error=None,
    )


@router.post("/trigger/sync", response_model=RunSummary)
async def trigger_pipeline_sync(req: TriggerRequest):
    """
    Synchronous pipeline trigger — waits for completion.
    Use for testing; may timeout for large league lists.
    """
    state = await run_pipeline(
        league_ids=req.league_ids,
        triggered_by=req.triggered_by,
    )
    summary = _state_to_summary(state)
    _run_history.append(summary)
    return summary


@router.get("/history", response_model=list[RunSummary])
async def get_run_history():
    """Return last 20 pipeline run summaries."""
    return _run_history


def _state_to_summary(state: AgentState) -> dict:
    return {
        "run_id": state.run_id,
        "triggered_by": state.triggered_by,
        "matches": len(state.matches),
        "signals": len(state.signals),
        "bets_placed": len(state.bets_placed),
        "completed_nodes": state.completed_nodes,
        "error": state.error,
    }
