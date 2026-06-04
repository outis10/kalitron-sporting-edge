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
    Seed mock Polymarket odds into the DB so the full pipeline can be tested
    without a real Polymarket API key or live markets.

    For each match that has a prediction:
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
    from sporting_edge.db.models import MarketOddsORM, MatchORM, PredictionORM
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
            print("No predictions found — run 'sporting-edge run' first.")
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


def _apply_migrations():
    import subprocess

    migration_files = [
        "migrations/versions/001_initial_schema.sql",
        "migrations/versions/002_position_manager.sql",
    ]

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
