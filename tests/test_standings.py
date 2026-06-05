"""
Tests for GroupStanding schema and get_standings() response parsing.
No real API calls — uses mock response data.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from sporting_edge.models.schemas import GroupStanding
from sporting_edge.tools.football_api import FootballAPIClient


# Minimal API-Football /standings response for World Cup (2 groups, 2 teams each)
_MOCK_STANDINGS_RESPONSE = {
    "response": [{
        "league": {
            "standings": [
                [   # Group A
                    {
                        "rank": 1, "group": "Group A",
                        "team": {"id": 6, "name": "Brazil"},
                        "points": 6,
                        "all": {"played": 2, "win": 2, "draw": 0, "lose": 0,
                                "goals": {"for": 5, "against": 1}},
                        "goalsDiff": 4,
                        "form": "WW",
                    },
                    {
                        "rank": 2, "group": "Group A",
                        "team": {"id": 10, "name": "Mexico"},
                        "points": 3,
                        "all": {"played": 2, "win": 1, "draw": 0, "lose": 1,
                                "goals": {"for": 2, "against": 3}},
                        "goalsDiff": -1,
                        "form": "WL",
                    },
                ],
                [   # Group B
                    {
                        "rank": 1, "group": "Group B",
                        "team": {"id": 7, "name": "France"},
                        "points": 4,
                        "all": {"played": 2, "win": 1, "draw": 1, "lose": 0,
                                "goals": {"for": 3, "against": 1}},
                        "goalsDiff": 2,
                        "form": "WD",
                    },
                ],
            ]
        }
    }]
}


@pytest.mark.asyncio
async def test_get_standings_parses_groups():
    """get_standings() returns one GroupStanding per team across all groups."""
    client = FootballAPIClient.__new__(FootballAPIClient)
    client._get = AsyncMock(return_value=_MOCK_STANDINGS_RESPONSE)

    result = await client.get_standings(league_id=1, season=2026)

    assert len(result) == 3
    assert all(isinstance(r, GroupStanding) for r in result)


@pytest.mark.asyncio
async def test_get_standings_brazil_values():
    """Brazil row is parsed correctly."""
    client = FootballAPIClient.__new__(FootballAPIClient)
    client._get = AsyncMock(return_value=_MOCK_STANDINGS_RESPONSE)

    result = await client.get_standings(league_id=1, season=2026)
    brazil = next(r for r in result if r.team_name == "Brazil")

    assert brazil.group == "Group A"
    assert brazil.rank == 1
    assert brazil.points == 6
    assert brazil.won == 2
    assert brazil.lost == 0
    assert brazil.goals_for == 5
    assert brazil.goal_diff == 4
    assert brazil.form == "WW"


@pytest.mark.asyncio
async def test_get_standings_empty_response():
    """Returns empty list when API returns no data."""
    client = FootballAPIClient.__new__(FootballAPIClient)
    client._get = AsyncMock(return_value={"response": []})

    result = await client.get_standings(league_id=1, season=2026)
    assert result == []


def test_group_standing_schema_fields():
    """GroupStanding accepts all required fields."""
    s = GroupStanding(
        group="Group A", rank=1, team_id=6, team_name="Brazil",
        points=6, played=2, won=2, drawn=0, lost=0,
        goals_for=5, goals_against=1, goal_diff=4, form="WW",
    )
    assert s.team_name == "Brazil"
    assert s.form == "WW"


def test_group_standing_form_optional():
    """form field is optional — some teams have no recent games yet."""
    s = GroupStanding(
        group="Group A", rank=1, team_id=6, team_name="Brazil",
        points=0, played=0, won=0, drawn=0, lost=0,
        goals_for=0, goals_against=0, goal_diff=0,
    )
    assert s.form is None
