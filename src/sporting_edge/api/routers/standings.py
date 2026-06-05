"""
Standings endpoint — group-stage tables for active leagues.
Primarily useful for World Cup and Euros context during group phase.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from sporting_edge.models.schemas import GroupStanding
from sporting_edge.tools.football_api import FootballAPIClient

router = APIRouter(prefix="/standings", tags=["standings"])


@router.get("", response_model=list[GroupStanding])
async def get_standings(
    league: int = Query(1, description="API-Football league ID (default: 1 = World Cup)"),
    season: int = Query(2026, description="Season year"),
):
    """
    Return group standings for a league/season.

    Example: GET /standings?league=1&season=2026
    Returns all groups (A-H for World Cup) sorted by group then rank.
    """
    async with FootballAPIClient() as client:
        return await client.get_standings(league_id=league, season=season)
