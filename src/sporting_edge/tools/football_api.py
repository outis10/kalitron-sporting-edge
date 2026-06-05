"""
API-Football v3 async client.
Docs: https://www.api-football.com/documentation-v3

All methods return typed Pydantic models; raw JSON never leaks out.
Rate limit: 100 calls/day on free tier, 7500/day on pro.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.models.schemas import (
    GroupStanding,
    HeadToHead,
    League,
    Match,
    MatchStatus,
    Team,
    TeamForm,
)

log = get_logger(__name__)

# Status mapping from API-Football to our enum
_STATUS_MAP: dict[str, MatchStatus] = {
    "TBD": MatchStatus.SCHEDULED,
    "NS":  MatchStatus.SCHEDULED,
    "1H":  MatchStatus.LIVE,
    "HT":  MatchStatus.LIVE,
    "2H":  MatchStatus.LIVE,
    "ET":  MatchStatus.LIVE,
    "P":   MatchStatus.LIVE,
    "FT":  MatchStatus.FINISHED,
    "AET": MatchStatus.FINISHED,
    "PEN": MatchStatus.FINISHED,
    "BT":  MatchStatus.LIVE,
    "SUSP":MatchStatus.POSTPONED,
    "INT": MatchStatus.LIVE,
    "PST": MatchStatus.POSTPONED,
    "CANC":MatchStatus.CANCELLED,
    "ABD": MatchStatus.CANCELLED,
    "AWD": MatchStatus.FINISHED,
    "WO":  MatchStatus.FINISHED,
}


class FootballAPIClient:
    """Async HTTP client for API-Football v3."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self._api_key = api_key or settings.api_football_key
        self._base_url = (base_url or settings.api_football_base_url).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "FootballAPIClient":
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "x-rapidapi-key": self._api_key,
                "x-rapidapi-host": "v3.football.api-sports.io",
            },
            timeout=15.0,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()

    # ── Low-level request ────────────────────────────────────────────────────

    async def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET with automatic retry on transient errors."""
        assert self._client, "Use as async context manager"

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        ):
            with attempt:
                resp = await self._client.get(endpoint, params=params)
                resp.raise_for_status()
                data = resp.json()

                # Log remaining quota so operators can spot exhaustion early
                remaining = resp.headers.get("x-ratelimit-requests-remaining")
                limit = resp.headers.get("x-ratelimit-requests-limit")
                if remaining is not None:
                    log.debug(
                        "api_football_quota",
                        remaining=remaining,
                        limit=limit,
                        endpoint=endpoint,
                    )
                    if int(remaining) <= 10:
                        log.warning(
                            "api_football_quota_low",
                            remaining=remaining,
                            limit=limit,
                        )

                errors = data.get("errors")
                if errors and errors != [] and errors != {}:
                    log.warning("api_football_error", endpoint=endpoint, errors=errors)
                    # Token/rate-limit errors — don't retry
                    if "rateLimit" in str(errors) or "token" in str(errors).lower():
                        raise ValueError(f"API-Football error: {errors}")

                return data

    # ── Public methods ────────────────────────────────────────────────────────

    async def get_fixtures(
        self,
        league_id: int,
        season: int,
        from_date: date | None = None,
        to_date: date | None = None,
        next_n: int | None = None,
    ) -> list[Match]:
        """
        Fetch fixtures for a league/season.
        Provide either (from_date, to_date) or next_n.
        """
        params: dict[str, Any] = {"league": league_id, "season": season}

        if next_n:
            params["next"] = next_n
        else:
            today = date.today()
            params["from"] = (from_date or today).isoformat()
            params["to"] = (to_date or (today + timedelta(days=7))).isoformat()

        data = await self._get("/fixtures", params)
        fixtures = data.get("response", [])

        log.info("fixtures_fetched", league_id=league_id, count=len(fixtures))
        return [self._parse_fixture(f) for f in fixtures]

    async def get_team_statistics(
        self,
        team_id: int,
        league_id: int,
        season: int,
        last_n: int = 5,  # kept for API compatibility; used only for form string fallback
    ) -> TeamForm | None:
        """
        Season-to-date aggregated stats for a team via /teams/statistics.

        The API returns totals + averages in a single call — far more efficient
        than fetching individual fixtures. xG is not available on this endpoint;
        it requires per-fixture /fixtures/statistics calls (higher-tier plans only).
        """
        params = {
            "team": team_id,
            "league": league_id,
            "season": season,
        }
        data = await self._get("/teams/statistics", params)
        resp = data.get("response")

        if not resp:
            return None

        fixtures_block = resp.get("fixtures", {})
        goals_block = resp.get("goals", {})
        team_name = resp.get("team", {}).get("name", "")

        played = fixtures_block.get("played", {}).get("total", 0)
        wins   = fixtures_block.get("wins",   {}).get("total", 0)
        draws  = fixtures_block.get("draws",  {}).get("total", 0)
        losses = fixtures_block.get("loses",  {}).get("total", 0)  # API uses "loses"

        goals_for     = goals_block.get("for",     {}).get("total", {}).get("total", 0) or 0
        goals_against = goals_block.get("against", {}).get("total", {}).get("total", 0) or 0

        return TeamForm(
            team_id=team_id,
            team_name=team_name,
            matches_played=int(played),
            wins=int(wins),
            draws=int(draws),
            losses=int(losses),
            goals_scored=float(goals_for),
            goals_conceded=float(goals_against),
            xg_for=None,     # requires separate /fixtures/statistics per match (pro tier)
            xg_against=None,
        )

    async def get_head_to_head(
        self, home_team_id: int, away_team_id: int, last_n: int = 10
    ) -> HeadToHead:
        """Historical H2H record."""
        params: dict[str, Any] = {
            "h2h": f"{home_team_id}-{away_team_id}",
        }
        # "last" param requires paid plan — omit on free tier
        # FIXME(paid-api): uncomment when upgraded
        # params["last"] = last_n
        data = await self._get("/fixtures/headtohead", params)
        fixtures = data.get("response", [])

        home_wins = draws = away_wins = 0
        home_goals = away_goals = 0.0

        for fix in fixtures:
            teams = fix.get("teams", {})
            goals = fix.get("goals", {})

            match_home_id = teams.get("home", {}).get("id")
            g_home = goals.get("home") or 0
            g_away = goals.get("away") or 0

            if match_home_id == home_team_id:
                home_goals += g_home
                away_goals += g_away
                if g_home > g_away:
                    home_wins += 1
                elif g_home == g_away:
                    draws += 1
                else:
                    away_wins += 1
            else:
                home_goals += g_away
                away_goals += g_home
                if g_away > g_home:
                    home_wins += 1
                elif g_away == g_home:
                    draws += 1
                else:
                    away_wins += 1

        return HeadToHead(
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            total_matches=len(fixtures),
            home_wins=home_wins,
            draws=draws,
            away_wins=away_wins,
            home_goals=home_goals,
            away_goals=away_goals,
        )

    async def get_lineups(self, fixture_id: int) -> dict[str, list[str]]:
        """
        Fetch confirmed starting XI for both teams.
        Returns {"home": [player_names], "away": [player_names]}.
        Lineups are typically published ~1h before kickoff.

        v3 response: [{team: {id, name}, startXI: [{player: {id, name, pos}}]}]
        """
        data = await self._get("/fixtures/lineups", {"fixture": fixture_id})
        lineups = data.get("response", [])

        result: dict[str, list[str]] = {"home": [], "away": []}
        if len(lineups) < 2:
            return result

        # API returns home team first, away second
        for i, side in enumerate(("home", "away")):
            if i >= len(lineups):
                break
            starting = lineups[i].get("startXI", [])
            result[side] = [
                f"{p['player']['name']} ({p['player'].get('pos', '?')})"
                for p in starting
                if p.get("player", {}).get("name")
            ]

        return result

    async def get_standings(self, league_id: int, season: int) -> list[GroupStanding]:
        """
        Fetch group standings for a league/season.

        Returns all teams sorted by group then rank. For knockout-only competitions
        (UCL final stages) the response may be empty.

        v3 response: [{league: {standings: [[{rank, team, points, ...}]]}}]
        Each inner list is one group; outer list has multiple groups.
        """
        data = await self._get("/standings", {"league": league_id, "season": season})
        resp = data.get("response", [])
        if not resp:
            return []

        standings: list[GroupStanding] = []
        league_block = resp[0].get("league", {}) if resp else {}
        groups = league_block.get("standings", [])

        for group_list in groups:
            for row in group_list:
                team = row.get("team", {})
                all_stats = row.get("all", {})
                goals = all_stats.get("goals", {})
                standings.append(GroupStanding(
                    group=row.get("group", ""),
                    rank=row.get("rank", 0),
                    team_id=team.get("id", 0),
                    team_name=team.get("name", ""),
                    points=row.get("points", 0),
                    played=all_stats.get("played", 0),
                    won=all_stats.get("win", 0),
                    drawn=all_stats.get("draw", 0),
                    lost=all_stats.get("lose", 0),
                    goals_for=goals.get("for", 0),
                    goals_against=goals.get("against", 0),
                    goal_diff=row.get("goalsDiff", 0),
                    form=row.get("form"),
                ))

        log.info("standings_fetched", league_id=league_id, teams=len(standings))
        return standings

    async def get_injuries(self, fixture_id: int) -> list[str]:
        """Return list of injured player names for a fixture."""
        params = {"fixture": fixture_id}
        data = await self._get("/injuries", params)
        injuries = data.get("response", [])

        # v3 response: {player: {id, name, photo, type, reason}, team: {id, name, logo}}
        return [
            (
                f"{inj['player']['name']} ({inj['team']['name']})"
                f" — {inj['player'].get('type', 'injury')}"
                + (f": {inj['player']['reason']}" if inj['player'].get('reason') else "")
            )
            for inj in injuries
        ]

    # ── Parsers ───────────────────────────────────────────────────────────────

    def _parse_fixture(self, raw: dict[str, Any]) -> Match:
        fix = raw.get("fixture", {})
        league_data = raw.get("league", {})
        teams_data = raw.get("teams", {})

        kickoff_ts = fix.get("timestamp")
        if kickoff_ts:
            kickoff_utc = datetime.fromtimestamp(kickoff_ts, tz=timezone.utc)
        else:
            kickoff_utc = datetime.utcnow()

        status_short = fix.get("status", {}).get("short", "NS")
        status = _STATUS_MAP.get(status_short, MatchStatus.SCHEDULED)

        return Match(
            match_id=str(fix.get("id", "")),
            league=League(
                id=league_data.get("id", 0),
                name=league_data.get("name", ""),
                country=league_data.get("country", ""),
                season=league_data.get("season", 0),
            ),
            home_team=Team(
                id=teams_data.get("home", {}).get("id", 0),
                name=teams_data.get("home", {}).get("name", ""),
                logo_url=teams_data.get("home", {}).get("logo"),
            ),
            away_team=Team(
                id=teams_data.get("away", {}).get("id", 0),
                name=teams_data.get("away", {}).get("name", ""),
                logo_url=teams_data.get("away", {}).get("logo"),
            ),
            kickoff_utc=kickoff_utc,
            status=status,
            venue=fix.get("venue", {}).get("name"),
        )


# ── Standalone function for easy import ─────────────────────────────────────

async def fetch_upcoming_matches(
    league_ids: list[int],
    season: int,
    days_ahead: int = 7,
) -> list[Match]:
    """Convenience wrapper used by DataCollector agent."""
    today = date.today()
    to_date = today + timedelta(days=days_ahead)
    all_matches: list[Match] = []

    async with FootballAPIClient() as client:
        tasks = [
            client.get_fixtures(
                league_id=lid,
                season=season,
                from_date=today,
                to_date=to_date,
            )
            for lid in league_ids
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for lid, result in zip(league_ids, results):
        if isinstance(result, Exception):
            log.error("fetch_fixtures_failed", league_id=lid, error=str(result))
        else:
            all_matches.extend(result)

    return all_matches
