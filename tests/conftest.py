"""Shared test fixtures."""
import pytest
from datetime import datetime, timezone

from sporting_edge.models.schemas import (
    HeadToHead, League, Match, MatchStatus, Team, TeamForm,
)


@pytest.fixture
def sample_league():
    return League(id=262, name="Liga MX", country="Mexico", season=2024)


@pytest.fixture
def america_form():
    return TeamForm(
        team_id=1,
        team_name="Club America",
        matches_played=5,
        wins=3,
        draws=1,
        losses=1,
        goals_scored=9.0,
        goals_conceded=4.0,
    )


@pytest.fixture
def chivas_form():
    return TeamForm(
        team_id=2,
        team_name="Chivas Guadalajara",
        matches_played=5,
        wins=2,
        draws=1,
        losses=2,
        goals_scored=6.0,
        goals_conceded=7.0,
    )


@pytest.fixture
def h2h_balanced():
    return HeadToHead(
        home_team_id=1,
        away_team_id=2,
        total_matches=10,
        home_wins=4,
        draws=3,
        away_wins=3,
        home_goals=14.0,
        away_goals=11.0,
    )


@pytest.fixture
def sample_match(sample_league, america_form, chivas_form, h2h_balanced):
    return Match(
        match_id="fixtures-12345",
        league=sample_league,
        home_team=Team(id=1, name="Club America"),
        away_team=Team(id=2, name="Chivas Guadalajara"),
        kickoff_utc=datetime(2025, 5, 1, 20, 0, tzinfo=timezone.utc),
        status=MatchStatus.SCHEDULED,
        home_form=america_form,
        away_form=chivas_form,
        h2h=h2h_balanced,
    )
