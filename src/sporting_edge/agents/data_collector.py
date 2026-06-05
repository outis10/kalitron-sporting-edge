"""
DataCollector Agent
===================
LangGraph node that:
  1. Fetches upcoming fixtures for configured leagues via API-Football
  2. Enriches each match with team form (last 5), H2H, injuries
  3. Looks up corresponding Polymarket markets
  4. Persists everything to PostgreSQL
  5. Returns enriched matches + odds in AgentState
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.db.models import LeagueORM, MarketOddsORM, MatchORM, TeamORM
from sporting_edge.db.session import AsyncSessionLocal
from sporting_edge.models.schemas import (
    AgentState,
    Match,
    MatchOdds,
    MatchStatus,
    Outcome,
)
from sporting_edge.tools.football_api import FootballAPIClient
from sporting_edge.tools.polymarket_tools import GammaClient, parse_market_to_odds

log = get_logger(__name__)

# Default season for club leagues.
# FIXME(paid-api): Revert to dynamic season once we upgrade to a paid API-Football plan.
#   Free tier only covers 2022–2024. When upgrading:
#   1. Uncomment the dynamic season logic below
#   2. Remove the date-shifting block in collect_data() (~15 lines below)
#   3. The dynamic logic:
#        today = date.today()
#        DEFAULT_SEASON = today.year if today.month >= 8 else today.year - 1
DEFAULT_SEASON = 2024

# Per-league season overrides — add here when a competition uses a different year.
# International tournaments run on the calendar year, not the Aug-May club season.
SEASON_OVERRIDE: dict[int, int] = {
    1: 2026,  # FIFA World Cup 2026
}


def _season_for(league_id: int) -> int:
    """Return the correct API-Football season for a given league."""
    return SEASON_OVERRIDE.get(league_id, DEFAULT_SEASON)


# How many days ahead to look for fixtures
LOOKAHEAD_DAYS = 7


async def data_collector_node(state: AgentState) -> AgentState:
    """
    LangGraph node entry point.
    Mutates and returns AgentState with `matches` and `odds` populated.
    """
    log.info("data_collector_start", run_id=state.run_id)

    league_ids = state.target_league_ids or settings.active_league_ids

    try:
        matches, odds = await collect_data(league_ids)
        state.matches = matches
        state.odds = odds
        state.completed_nodes.append("data_collector")
        log.info(
            "data_collector_done",
            run_id=state.run_id,
            matches=len(matches),
            odds=len(odds),
        )
    except Exception as exc:
        log.error("data_collector_error", run_id=state.run_id, error=str(exc))
        state.error = f"DataCollector failed: {exc}"

    return state


async def collect_data(league_ids: list[int]) -> tuple[list[Match], list[MatchOdds]]:
    """
    Main collection pipeline — called by the node and also directly in tests.
    Returns (enriched_matches, market_odds).
    """
    today = date.today()
    to_date = today + timedelta(days=LOOKAHEAD_DAYS)

    # Date-shifting for club leagues using historical season (free-tier workaround).
    # World Cup (league=1) uses season=2026 which is current — no shifting needed.
    if DEFAULT_SEASON < today.year - 1:
        try:
            from_date = date(DEFAULT_SEASON + 1, today.month, today.day)
        except ValueError:
            from_date = date(DEFAULT_SEASON + 1, today.month, 28)
        to_date = from_date + timedelta(days=LOOKAHEAD_DAYS)
    else:
        from_date = today

    async with FootballAPIClient() as football_client, AsyncSessionLocal() as db:

        # ── Step 1: Fetch fixtures ────────────────────────────────────────────
        all_matches: list[Match] = []
        for league_id in league_ids:
            try:
                season = _season_for(league_id)
                # World Cup uses real current dates; club leagues use shifted dates
                league_from = today if season >= today.year else from_date
                league_to = today + timedelta(days=LOOKAHEAD_DAYS) if season >= today.year else to_date
                matches = await football_client.get_fixtures(
                    league_id=league_id,
                    season=season,
                    from_date=league_from,
                    to_date=league_to,
                )
                all_matches.extend(matches)
            except Exception as exc:
                log.error("fixtures_fetch_error", league_id=league_id, error=str(exc))

        log.info("fixtures_total", count=len(all_matches))

        if not all_matches:
            return [], []

        # ── Step 2: Persist teams & leagues ──────────────────────────────────
        await _upsert_teams_and_leagues(db, all_matches)

        # ── Step 3: Persist fixture skeletons ─────────────────────────────────
        await _upsert_matches(db, all_matches)

        # ── Step 4: Enrich with form + H2H (concurrent per match) ────────────
        enriched = await _enrich_matches(football_client, all_matches)

        # ── Step 5: Find Polymarket markets ───────────────────────────────────
        all_odds: list[MatchOdds] = []
        gamma = GammaClient()
        for match in enriched:
            odds = await _find_match_odds(gamma, match)
            all_odds.extend(odds)

        # ── Step 6: Persist odds ───────────────────────────────────────────────
        if all_odds:
            await _upsert_odds(db, all_odds)

        await db.commit()

    return enriched, all_odds


# ── Private helpers ──────────────────────────────────────────────────────────

async def _upsert_teams_and_leagues(db: AsyncSession, matches: list[Match]) -> None:
    """Insert or ignore leagues and teams."""
    leagues_seen: set[int] = set()
    teams_seen: set[int] = set()

    for match in matches:
        if match.league.id not in leagues_seen:
            leagues_seen.add(match.league.id)
            stmt = pg_insert(LeagueORM).values(
                id=match.league.id,
                name=match.league.name,
                country=match.league.country,
                season=match.league.season,
            ).on_conflict_do_update(
                index_elements=["id"],
                set_={"name": match.league.name, "season": match.league.season},
            )
            await db.execute(stmt)

        for team in (match.home_team, match.away_team):
            if team.id not in teams_seen:
                teams_seen.add(team.id)
                stmt = pg_insert(TeamORM).values(
                    id=team.id,
                    name=team.name,
                    logo_url=team.logo_url,
                ).on_conflict_do_update(
                    index_elements=["id"],
                    set_={"name": team.name, "logo_url": team.logo_url},
                )
                await db.execute(stmt)


async def _upsert_matches(db: AsyncSession, matches: list[Match]) -> None:
    """Insert or update match rows."""
    for match in matches:
        stmt = pg_insert(MatchORM).values(
            id=match.match_id,
            league_id=match.league.id,
            home_team_id=match.home_team.id,
            away_team_id=match.away_team.id,
            kickoff_utc=match.kickoff_utc,
            status=match.status.value,
            venue=match.venue,
        ).on_conflict_do_update(
            index_elements=["id"],
            set_={"status": match.status.value, "venue": match.venue},
        )
        await db.execute(stmt)


async def _enrich_matches(
    client: FootballAPIClient,
    matches: list[Match],
) -> list[Match]:
    """Fetch form + H2H for all matches sequentially to respect free-tier rate limit (10 req/min)."""
    # FIXME(paid-api): restore concurrent enrichment with Semaphore(5) when on paid plan

    async def enrich_one(match: Match) -> Match:
            try:
                # Sequential to avoid rate-limit; 3 requests per match
                home_form = await client.get_team_statistics(
                    match.home_team.id, match.league.id, _season_for(match.league.id)
                )
                await asyncio.sleep(7)  # ~8-9 req/min max
                away_form = await client.get_team_statistics(
                    match.away_team.id, match.league.id, _season_for(match.league.id)
                )
                await asyncio.sleep(7)
                h2h = await client.get_head_to_head(match.home_team.id, match.away_team.id)
                await asyncio.sleep(7)
                if home_form:
                    home_form.team_name = match.home_team.name
                if away_form:
                    away_form.team_name = match.away_team.name

                match.home_form = home_form
                match.away_form = away_form
                match.h2h = h2h

                # Lineups are only available ~1h before kickoff — fetch only when close
                now_utc = datetime.now(tz=timezone.utc)
                kickoff = match.kickoff_utc
                if kickoff.tzinfo is None:
                    kickoff = kickoff.replace(tzinfo=timezone.utc)
                minutes_to_kick = (kickoff - now_utc).total_seconds() / 60

                if 0 < minutes_to_kick <= 120:
                    lineups = await client.get_lineups(int(match.match_id))
                    match.home_lineup = lineups.get("home", [])
                    match.away_lineup = lineups.get("away", [])
                    if match.home_lineup:
                        log.info(
                            "lineups_fetched",
                            match_id=match.match_id,
                            home_count=len(match.home_lineup),
                            away_count=len(match.away_lineup),
                        )

            except Exception as exc:
                log.warning(
                    "enrich_match_failed",
                    match_id=match.match_id,
                    error=str(exc),
                )
            return match

    # Sequential to respect free-tier rate limit
    # FIXME(paid-api): restore asyncio.gather(*[enrich_one(m) for m in matches])
    enriched = []
    for m in matches:
        enriched.append(await enrich_one(m))
    return enriched


async def _find_match_odds(gamma: GammaClient, match: Match) -> list[MatchOdds]:
    """Search Polymarket for markets related to this match."""
    raw_markets = await gamma.find_match_markets(
        home_team=match.home_team.name,
        away_team=match.away_team.name,
    )

    odds_list: list[MatchOdds] = []
    for market in raw_markets:
        # Try to classify the outcome this market covers
        question = market.get("question", "").lower()
        if "win" in question or match.home_team.name.lower() in question:
            # Could be home win or away win — heuristic classification
            if match.home_team.name.lower() in question:
                outcome = Outcome.HOME
            elif match.away_team.name.lower() in question:
                outcome = Outcome.AWAY
            else:
                outcome = Outcome.HOME  # default guess
        elif "draw" in question:
            outcome = Outcome.DRAW
        else:
            outcome = Outcome.HOME  # fallback

        parsed = parse_market_to_odds(market, match.match_id, outcome)
        if parsed:
            odds_list.append(parsed)

    return odds_list


async def _upsert_odds(db: AsyncSession, odds_list: list[MatchOdds]) -> None:
    """Persist market odds snapshots (append-only by condition_id + timestamp)."""
    for odds in odds_list:
        stmt = pg_insert(MarketOddsORM).values(
            match_id=odds.match_id,
            condition_id=odds.condition_id,
            market_question=odds.market_question,
            outcome=odds.outcome.value,
            yes_price=odds.yes_price,
            no_price=odds.no_price,
            volume_24h=odds.volume_24h,
            liquidity=odds.liquidity,
            yes_token_id=odds.yes_token_id,
            no_token_id=odds.no_token_id,
            fetched_at=odds.fetched_at,
        ).on_conflict_do_nothing()
        await db.execute(stmt)
