"""
CLI entry point — `sporting-edge <command>`
"""
from __future__ import annotations

import asyncio
import sys


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Kalitron Sporting Edge CLI")
    sub = parser.add_subparsers(dest="command")

    # Run pipeline once
    run_p = sub.add_parser("run", help="Run the full pipeline once")
    run_p.add_argument("--leagues", help="Comma-separated league IDs", default=None)
    run_p.add_argument(
        "--from-db",
        action="store_true",
        help="Skip DataCollector API calls — use matches+odds already in DB (for dry-run / off-season testing)",
    )

    # Backtest
    bt_p = sub.add_parser("backtest", help="Run backtesting on a CSV file")
    bt_p.add_argument("file", help="Path to CSV or JSON data file")
    bt_p.add_argument("--bankroll", type=float, default=1000.0)
    bt_p.add_argument("--output", help="Output CSV path for bet simulations")

    # Start API server
    sub.add_parser("serve", help="Start the FastAPI monitoring server")

    # Apply DB migrations
    sub.add_parser("migrate", help="Apply database migrations")

    # Inject mock market odds for testing (no real Polymarket needed)
    mock_p = sub.add_parser("mock-odds", help="Inject fake Polymarket odds to test full pipeline")
    mock_p.add_argument("--ev", type=float, default=0.12, help="Target EV discount (default 0.12 = 12%%)")

    args = parser.parse_args()

    if args.command == "run":
        league_ids = None
        if args.leagues:
            league_ids = [int(x) for x in args.leagues.split(",")]
        if getattr(args, "from_db", False):
            asyncio.run(_run_pipeline_from_db(league_ids))
        else:
            asyncio.run(_run_pipeline(league_ids))

    elif args.command == "backtest":
        from sporting_edge.backtesting.engine import run_backtest
        result = run_backtest(args.file, initial_bankroll=args.bankroll)
        result.print_report()
        if args.output:
            result.to_csv(args.output)

    elif args.command == "serve":
        import uvicorn
        from sporting_edge.config import settings
        uvicorn.run(
            "sporting_edge.api.main:app",
            host=settings.api_host,
            port=settings.api_port,
            reload=(settings.environment == "development"),
        )

    elif args.command == "migrate":
        _apply_migrations()

    elif args.command == "mock-odds":
        asyncio.run(_inject_mock_odds(args.ev))

    else:
        parser.print_help()


async def _run_pipeline(league_ids):
    from sporting_edge.config.logging import configure_logging
    configure_logging()
    from sporting_edge.graph.orchestrator import run_pipeline
    state = await run_pipeline(league_ids=league_ids, triggered_by="cli")
    print(f"\n✅ Pipeline complete — {len(state.signals)} signals, {len(state.bets_placed)} bets placed")
    if state.error:
        print(f"⚠️  Error: {state.error}")


async def _inject_mock_odds(ev_discount: float = 0.12) -> None:
    """
    Seed mock Polymarket odds into the DB for end-to-end paper trading tests.

    If no matches/predictions exist yet (off-season or first run), creates
    synthetic fixtures with realistic probabilities so the full pipeline
    can be exercised without real API-Football data or live Polymarket markets.

    For each match with a prediction:
      - Shifts kickoff to 4 hours from now (inside the 30min-48h window)
      - Creates a market_odds row per outcome (home/draw/away) where:
          yes_price = model_prob * (1 - ev_discount)   → intentional underpricing
          no_price  = 1 - yes_price - 0.01             → ~1% vig
          liquidity = $15,000                          → above $5k floor
    """
    import uuid
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select, update

    from sporting_edge.config.logging import configure_logging
    from sporting_edge.db.models import (
        LeagueORM, MarketOddsORM, MatchORM, PredictionORM, TeamORM,
    )
    from sporting_edge.db.session import AsyncSessionLocal

    configure_logging()

    future_kickoff = datetime.now(tz=timezone.utc) + timedelta(hours=4)

    async with AsyncSessionLocal() as db:
        # Load predictions with their matches
        result = await db.execute(
            select(PredictionORM, MatchORM)
            .join(MatchORM, PredictionORM.match_id == MatchORM.id)
        )
        rows = result.all()

        if not rows:
            print("No predictions found — creating synthetic test fixtures...")
            rows = await _seed_test_fixtures(db, future_kickoff)
            if not rows:
                print("❌ Could not create test fixtures. Check DB connection.")
                return

        inserted = 0
        for pred, match in rows:
            # Shift kickoff to 4 hours from now
            await db.execute(
                update(MatchORM)
                .where(MatchORM.id == match.id)
                .values(kickoff_utc=future_kickoff)
            )

            # One market per outcome
            outcomes = [
                ("home", pred.prob_home),
                ("draw", pred.prob_draw),
                ("away", pred.prob_away),
            ]
            for outcome_label, model_prob in outcomes:
                yes_price = round(model_prob * (1 - ev_discount), 4)
                yes_price = max(0.02, min(0.97, yes_price))  # clamp to valid range
                no_price = round(1.0 - yes_price - 0.01, 4)
                no_price = max(0.02, min(0.97, no_price))

                cid = f"MOCK-{match.id}-{outcome_label}"
                tid_yes = f"MOCK-YES-{uuid.uuid4().hex[:16]}"
                tid_no  = f"MOCK-NO-{uuid.uuid4().hex[:16]}"

                from sqlalchemy.dialects.postgresql import insert as pg_insert
                stmt = pg_insert(MarketOddsORM).values(
                    id=uuid.uuid4(),
                    match_id=match.id,
                    condition_id=cid,
                    market_question=f"[MOCK] Will {match.id} {outcome_label} win?",
                    outcome=outcome_label,
                    yes_price=yes_price,
                    no_price=no_price,
                    volume_24h=50_000.0,
                    liquidity=15_000.0,
                    yes_token_id=tid_yes,
                    no_token_id=tid_no,
                    fetched_at=datetime.now(tz=timezone.utc),
                ).on_conflict_do_nothing()
                await db.execute(stmt)
                inserted += 1

        await db.commit()

    print(f"✅ Mock odds injected: {len(rows)} matches × 3 outcomes = {inserted} rows")
    print(f"   Kickoff shifted to {future_kickoff.strftime('%H:%M UTC')} (4h from now)")
    print(f"   EV discount applied: {ev_discount:.0%}")
    print(f"\n   Now run: sporting-edge run")


async def _run_pipeline_from_db(league_ids: list[int] | None = None) -> None:
    """
    Run ModelPredictor → OddsAnalyzer → RiskManager → ExecutionAgent → ReportAgent
    using matches and market_odds already stored in the DB.

    Skips DataCollector entirely — useful for off-season dry-run testing after
    running `sporting-edge mock-odds`.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from sporting_edge.config import settings
    from sporting_edge.config.logging import configure_logging
    from sporting_edge.db.models import LeagueORM, MarketOddsORM, MatchORM, TeamORM
    from sporting_edge.db.session import AsyncSessionLocal
    from sporting_edge.models.schemas import (
        AgentState, HeadToHead, League, Match, MatchOdds, MatchStatus,
        Outcome, Team, TeamForm,
    )

    configure_logging()

    target_leagues = league_ids or settings.active_league_ids

    async with AsyncSessionLocal() as db:
        # Load upcoming matches from DB
        now = datetime.now(tz=timezone.utc)
        result = await db.execute(
            select(MatchORM)
            .where(MatchORM.kickoff_utc > now)
            .order_by(MatchORM.kickoff_utc)
        )
        match_rows = list(result.scalars().all())

        if not match_rows:
            print("❌ No upcoming matches in DB. Run 'sporting-edge mock-odds' first.")
            return

        # Build Match schemas from ORM rows
        matches: list[Match] = []
        for row in match_rows:
            home_result = await db.execute(
                select(TeamORM).where(TeamORM.id == row.home_team_id)
            )
            away_result = await db.execute(
                select(TeamORM).where(TeamORM.id == row.away_team_id)
            )
            league_result = await db.execute(
                select(LeagueORM).where(LeagueORM.id == row.league_id)
            )
            home_team = home_result.scalar_one_or_none()
            away_team = away_result.scalar_one_or_none()
            league = league_result.scalar_one_or_none()

            if not home_team or not away_team or not league:
                continue

            # Synthetic form data — needed so ModelPredictor confidence >= 0.60
            # (without form, data_quality=0.25 → confidence=0.45 → rejected by OddsAnalyzer)
            home_form = TeamForm(
                team_id=home_team.id, team_name=home_team.name,
                matches_played=5, wins=2, draws=2, losses=1,
                goals_scored=8.0, goals_conceded=5.0,
            )
            away_form = TeamForm(
                team_id=away_team.id, team_name=away_team.name,
                matches_played=5, wins=2, draws=1, losses=2,
                goals_scored=6.0, goals_conceded=7.0,
            )

            matches.append(Match(
                match_id=row.id,
                league=League(
                    id=league.id, name=league.name,
                    country=league.country, season=league.season,
                ),
                home_team=Team(id=home_team.id, name=home_team.name),
                away_team=Team(id=away_team.id, name=away_team.name),
                kickoff_utc=row.kickoff_utc,
                status=MatchStatus.SCHEDULED,
                home_form=home_form,
                away_form=away_form,
            ))

        # Load corresponding market odds from DB
        match_ids = [m.match_id for m in matches]
        odds_result = await db.execute(
            select(MarketOddsORM).where(MarketOddsORM.match_id.in_(match_ids))
        )
        odds_rows = list(odds_result.scalars().all())

        odds: list[MatchOdds] = [
            MatchOdds(
                condition_id=row.condition_id,
                market_question=row.market_question,
                match_id=row.match_id,
                outcome=Outcome(row.outcome),
                yes_price=row.yes_price,
                no_price=row.no_price,
                volume_24h=row.volume_24h,
                liquidity=row.liquidity,
                yes_token_id=row.yes_token_id,
                no_token_id=row.no_token_id,
                fetched_at=row.fetched_at,
            )
            for row in odds_rows
        ]

    if not odds:
        print("❌ No market odds in DB. Run 'sporting-edge mock-odds' first.")
        return

    print(f"📊 Loaded from DB: {len(matches)} matches, {len(odds)} odds snapshots")

    # Build initial state with DB data and skip data_collector
    initial_state = AgentState(
        target_league_ids=target_leagues,
        triggered_by="cli-from-db",
        matches=matches,
        odds=odds,
        completed_nodes=["data_collector"],  # mark as already done
    )

    from sporting_edge.agents.model_predictor import model_predictor_node
    from sporting_edge.agents.odds_analyzer import odds_analyzer_node
    from sporting_edge.agents.risk_manager import risk_manager_node
    from sporting_edge.agents.execution_agent import execution_agent_node
    from sporting_edge.agents.report_agent import report_agent_node

    state = initial_state
    for node_fn, name in [
        (model_predictor_node, "model_predictor"),
        (odds_analyzer_node,   "odds_analyzer"),
        (risk_manager_node,    "risk_manager"),
        (execution_agent_node, "execution_agent"),
        (report_agent_node,    "report_agent"),
    ]:
        if state.error:
            print(f"⚠️  Stopping at {name}: {state.error}")
            break
        print(f"  → running {name}...")
        state = await node_fn(state)

    print(f"\n✅ Pipeline complete — {len(state.signals)} signals, {len(state.bets_placed)} bets placed (paper)")
    if state.error:
        print(f"⚠️  Error: {state.error}")


async def _seed_test_fixtures(db, kickoff) -> list:
    """
    Create 3 synthetic EPL fixtures with predictions for off-season dry-run testing.
    Returns list of (PredictionORM, MatchORM) tuples.
    """
    import uuid
    from datetime import timezone
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sporting_edge.db.models import LeagueORM, MatchORM, PredictionORM, TeamORM

    # Ensure EPL league row exists
    await db.execute(
        pg_insert(LeagueORM).values(
            id=39, name="Premier League", country="England", season=2024
        ).on_conflict_do_nothing()
    )

    TEST_FIXTURES = [
        dict(id="TEST-001", home_id=33, home="Manchester United",
             away_id=40, away="Liverpool",
             p_home=0.35, p_draw=0.25, p_away=0.40),
        dict(id="TEST-002", home_id=50, home="Manchester City",
             away_id=47, away="Tottenham",
             p_home=0.55, p_draw=0.25, p_away=0.20),
        dict(id="TEST-003", home_id=42, home="Arsenal",
             away_id=49, away="Chelsea",
             p_home=0.40, p_draw=0.30, p_away=0.30),
    ]

    rows = []
    for fix in TEST_FIXTURES:
        for team_id, team_name in ((fix["home_id"], fix["home"]), (fix["away_id"], fix["away"])):
            await db.execute(
                pg_insert(TeamORM).values(id=team_id, name=team_name)
                .on_conflict_do_nothing()
            )

        await db.execute(
            pg_insert(MatchORM).values(
                id=fix["id"], league_id=39,
                home_team_id=fix["home_id"], away_team_id=fix["away_id"],
                kickoff_utc=kickoff, status="scheduled",
            ).on_conflict_do_update(
                index_elements=["id"],
                set_={"kickoff_utc": kickoff},
            )
        )

        pred_id = uuid.uuid4()
        await db.execute(
            pg_insert(PredictionORM).values(
                id=pred_id, match_id=fix["id"],
                model_version="v1-dixon-coles",
                prob_home=fix["p_home"], prob_draw=fix["p_draw"], prob_away=fix["p_away"],
                confidence=0.72,
            ).on_conflict_do_nothing()
        )

        from sqlalchemy import select
        pred = (await db.execute(
            select(PredictionORM).where(PredictionORM.match_id == fix["id"])
        )).scalar_one()
        match = (await db.execute(
            select(MatchORM).where(MatchORM.id == fix["id"])
        )).scalar_one()
        rows.append((pred, match))

    await db.commit()
    print(f"   Created {len(rows)} synthetic EPL fixtures (Man Utd vs Liverpool, City vs Spurs, Arsenal vs Chelsea)")
    return rows


def _apply_migrations():
    import subprocess

    import glob as glob_mod
    migration_files = sorted(
        glob_mod.glob("migrations/versions/*.sql")
    )

    # Detect whether psql is available locally or only inside the Docker container
    local_psql = subprocess.run(["which", "psql"], capture_output=True).returncode == 0

    for migration_file in migration_files:
        if local_psql:
            from sporting_edge.config import settings
            cmd = ["psql", settings.database_url_sync, "-f", migration_file]
        else:
            # Copy file into container and run psql there
            container = _get_db_container()
            if not container:
                print("❌ Could not find running Postgres container. Start it with: docker compose up -d db")
                sys.exit(1)
            copy_result = subprocess.run(
                ["docker", "cp", migration_file, f"{container}:/tmp/migration.sql"],
                capture_output=True, text=True,
            )
            if copy_result.returncode != 0:
                print(f"❌ docker cp failed:\n{copy_result.stderr}")
                sys.exit(1)
            cmd = ["docker", "exec", container,
                   "psql", "-U", "sporting", "-d", "sporting_edge", "-f", "/tmp/migration.sql"]

        print(f"Applying: {migration_file}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ {migration_file} applied")
        else:
            print(f"❌ Migration failed:\n{result.stderr}")
            sys.exit(1)


def _get_db_container() -> str | None:
    """Return the name of the running Postgres container, if any."""
    import subprocess
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    for name in result.stdout.splitlines():
        if "db" in name.lower() or "postgres" in name.lower():
            return name
    return None
