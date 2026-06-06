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

    # Outright WC scan — one-shot, no DB required
    outright_p = sub.add_parser(
        "outright",
        help="Scan Polymarket WC 2026 outright markets and show signals (no DB needed)",
    )
    outright_p.add_argument(
        "--execute",
        action="store_true",
        help="Place paper bets for signals found (requires DB running)",
    )
    outright_p.add_argument(
        "--min-liquidity",
        type=float,
        default=1_000.0,
        help="Minimum market liquidity to consider (default: $1000)",
    )

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

    # Export resolved matches from DB as a backtest-ready CSV
    export_p = sub.add_parser(
        "export-backtest",
        help="Export resolved matches from DB to a CSV ready for enrich-backtest + backtest",
    )
    export_p.add_argument("--output", default="data/backtest_export.csv", help="Output CSV path")
    export_p.add_argument("--league", help="Comma-separated league IDs to filter (e.g. 39,140)")
    export_p.add_argument("--since", help="ISO date lower bound, e.g. 2025-01-01 (default: 90 days ago)")
    export_p.add_argument("--until", help="ISO date upper bound (default: today)")

    # Enrich backtest CSV with real Polymarket historical prices
    enrich_p = sub.add_parser(
        "enrich-backtest",
        help="Fetch real Polymarket prices for backtest rows (fills market_yes_price from CLOB history)",
    )
    enrich_p.add_argument("file", help="Input CSV with yes_token_id + kickoff_utc columns")
    enrich_p.add_argument("--output", help="Output CSV path (default: <file>_enriched.csv)")
    enrich_p.add_argument(
        "--interval",
        default="1m",
        choices=["1m", "1h", "6h", "1d"],
        help="Candle interval for price lookup (default: 1m)",
    )
    enrich_p.add_argument(
        "--window",
        type=int,
        default=60,
        help="Minutes before kickoff to search for price (default: 60)",
    )
    enrich_p.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-fetch price even if market_yes_price is already set",
    )

    # Inject mock market odds for testing (no real Polymarket needed)
    mock_p = sub.add_parser("mock-odds", help="Inject fake Polymarket odds to test full pipeline")
    mock_p.add_argument("--ev", type=float, default=0.12, help="Target EV discount (default 0.12 = 12%%)")
    mock_p.add_argument("--kickoff-minutes", type=int, default=240,
                        help="Minutes until mock kickoff (default 240=4h). Use 70 to demo full cycle in ~40 min")

    args = parser.parse_args()

    if args.command == "outright":
        asyncio.run(_run_outright_scan(args.min_liquidity, args.execute))

    elif args.command == "run":
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
        asyncio.run(_apply_migrations())

    elif args.command == "export-backtest":
        league_ids = [int(x) for x in args.league.split(",")] if args.league else None
        asyncio.run(_export_backtest(args.output, league_ids, args.since, args.until))

    elif args.command == "enrich-backtest":
        from pathlib import Path
        output = args.output or str(Path(args.file).with_stem(Path(args.file).stem + "_enriched"))
        asyncio.run(_enrich_backtest(args.file, output, args.interval, args.window, args.overwrite))

    elif args.command == "mock-odds":
        asyncio.run(_inject_mock_odds(args.ev, args.kickoff_minutes))

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


async def _run_outright_scan(min_liquidity: float = 1_000.0, execute: bool = False) -> None:
    """
    One-shot outright scan: fetch WC markets, run analyzer, print signals.
    No DB required unless --execute is passed.
    """
    from sporting_edge.agents.outright_collector import collect_outright_markets
    from sporting_edge.agents.outright_analyzer import analyze_outright_markets
    from sporting_edge.config import settings

    print("\n🏆 Outright WC 2026 — Market Scan\n")

    markets = await collect_outright_markets(min_liquidity=min_liquidity)
    if not markets:
        print("❌ No WC outright markets found on Polymarket.")
        print("   Check that the World Cup markets are active at polymarket.com/fifa-world-cup")
        return

    print(f"Found {len(markets)} markets  (min liquidity: ${min_liquidity:,.0f})\n")
    print(f"{'Team':<22} {'Market%':>8} {'Liquidity':>12} {'Vol24h':>10}")
    print("─" * 56)
    for m in markets:
        print(f"{m.team_name:<22} {m.yes_price:>7.1%} ${m.liquidity:>10,.0f} ${m.volume_24h:>8,.0f}")

    signals = analyze_outright_markets(markets, trigger="proactive")

    print(f"\n{'─'*56}")
    print(f"EV threshold: {settings.outright_ev_threshold:.0%} | "
          f"Max positions: {settings.outright_max_positions} | "
          f"Max bet: {settings.outright_max_bet_pct:.0%} bankroll\n")

    if not signals:
        print("No signals above EV threshold.")
        print("  → Market prices are close to FIFA ranking model — no edge detected.")
        return

    print(f"{'':─<56}")
    print(f"  {len(signals)} SIGNAL(S) FOUND\n")
    print(f"{'Team':<22} {'ModelP':>7} {'MktP':>7} {'EV':>8} {'Size':>8} {'Trigger':>9}")
    print("─" * 65)
    for s in signals:
        print(
            f"{s.market.team_name:<22} "
            f"{s.model_probability:>6.1%} "
            f"{s.market_probability:>6.1%} "
            f"{s.expected_value:>7.1%} "
            f"${s.size_usd:>6.2f} "
            f"  {s.trigger}"
        )

    if execute:
        print(f"\nPlacing paper bets for {len(signals)} signal(s)...")
        from sporting_edge.graph.outright_pipeline import execute_outright_signals
        placed = await execute_outright_signals(signals)
        print(f"✅ {placed} bet(s) placed (paper mode: {settings.paper_trading})")
    else:
        print(f"\nRun with --execute to place paper bets for these signals.")


async def _export_backtest(
    output_path: str,
    league_ids: list[int] | None = None,
    since: str | None = None,
    until: str | None = None,
) -> None:
    """
    Query resolved matches from the DB and write a CSV ready for enrich-backtest.

    Joins: matches + teams + leagues + market_odds (latest snapshot per outcome).
    Form data columns are set to 0 — market_yes_price is a placeholder that
    enrich-backtest will replace with real CLOB historical prices.

    Skips matches without result_outcome or without a yes_token_id in market_odds.
    """
    import csv
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    from sqlalchemy import select

    from sporting_edge.config.logging import configure_logging
    from sporting_edge.db.models import LeagueORM, MarketOddsORM, MatchORM, TeamORM
    from sporting_edge.db.session import AsyncSessionLocal

    configure_logging()

    now = datetime.now(tz=timezone.utc)
    since_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc) if since else now - timedelta(days=90)
    until_dt = datetime.fromisoformat(until).replace(tzinfo=timezone.utc) if until else now

    async with AsyncSessionLocal() as db:
        # Load resolved matches in the date window
        q = select(MatchORM).where(
            MatchORM.result_outcome.is_not(None),
            MatchORM.home_goals.is_not(None),
            MatchORM.kickoff_utc >= since_dt,
            MatchORM.kickoff_utc <= until_dt,
        )
        if league_ids:
            q = q.where(MatchORM.league_id.in_(league_ids))

        match_rows = list((await db.execute(q)).scalars().all())

        if not match_rows:
            print("❌ No resolved matches found in DB for the given filters.")
            print(f"   Window: {since_dt.date()} → {until_dt.date()}")
            if league_ids:
                print(f"   Leagues: {league_ids}")
            return

        match_ids = [m.id for m in match_rows]

        # Load all market odds for these matches (latest snapshot per outcome)
        odds_rows = list(
            (await db.execute(
                select(MarketOddsORM)
                .where(
                    MarketOddsORM.match_id.in_(match_ids),
                    MarketOddsORM.yes_token_id.is_not(None),
                )
                .order_by(MarketOddsORM.match_id, MarketOddsORM.outcome, MarketOddsORM.fetched_at.desc())
            )).scalars().all()
        )

        # Keep only the latest snapshot per (match_id, outcome)
        seen: set[tuple] = set()
        latest_odds: list[MarketOddsORM] = []
        for o in odds_rows:
            key = (o.match_id, o.outcome)
            if key not in seen:
                seen.add(key)
                latest_odds.append(o)

        # Build lookup maps
        odds_by_match: dict[str, list[MarketOddsORM]] = {}
        for o in latest_odds:
            odds_by_match.setdefault(o.match_id, []).append(o)

        team_ids = {m.home_team_id for m in match_rows} | {m.away_team_id for m in match_rows}
        teams = {
            t.id: t for t in (
                await db.execute(select(TeamORM).where(TeamORM.id.in_(team_ids)))
            ).scalars().all()
        }

        league_ids_found = {m.league_id for m in match_rows}
        leagues = {
            lg.id: lg for lg in (
                await db.execute(select(LeagueORM).where(LeagueORM.id.in_(league_ids_found)))
            ).scalars().all()
        }

    # Build CSV rows
    FIELDNAMES = [
        "match_id", "home_team", "away_team", "home_team_id", "away_team_id",
        "league_id", "league_name", "season", "kickoff_utc",
        "home_goals_full", "away_goals_full", "target_outcome",
        # Form data — not in DB, defaulted to 0; enrich manually or accept lower confidence
        "home_form_w", "home_form_d", "home_form_l", "home_form_gf", "home_form_ga",
        "away_form_w", "away_form_d", "away_form_l", "away_form_gf", "away_form_ga",
        "h2h_home_wins", "h2h_draws", "h2h_away_wins", "h2h_total",
        # Market data
        "market_yes_price", "market_liquidity", "yes_token_id", "no_token_id", "condition_id",
    ]

    csv_rows = []
    skipped_no_odds = 0

    for match in match_rows:
        odds_list = odds_by_match.get(match.id, [])
        if not odds_list:
            skipped_no_odds += 1
            continue

        home = teams.get(match.home_team_id)
        away = teams.get(match.away_team_id)
        league = leagues.get(match.league_id)

        for odds in odds_list:
            csv_rows.append({
                "match_id": match.id,
                "home_team": home.name if home else match.home_team_id,
                "away_team": away.name if away else match.away_team_id,
                "home_team_id": match.home_team_id,
                "away_team_id": match.away_team_id,
                "league_id": match.league_id,
                "league_name": league.name if league else "",
                "season": league.season if league else "",
                "kickoff_utc": match.kickoff_utc.isoformat(),
                "home_goals_full": match.home_goals,
                "away_goals_full": match.away_goals,
                "target_outcome": odds.outcome,
                # Form — defaulted to 0
                "home_form_w": 0, "home_form_d": 0, "home_form_l": 0,
                "home_form_gf": 0, "home_form_ga": 0,
                "away_form_w": 0, "away_form_d": 0, "away_form_l": 0,
                "away_form_gf": 0, "away_form_ga": 0,
                "h2h_home_wins": 0, "h2h_draws": 0, "h2h_away_wins": 0, "h2h_total": 0,
                # Market — yes_price is a placeholder; enrich-backtest replaces it
                "market_yes_price": round(odds.yes_price, 4),
                "market_liquidity": round(odds.liquidity, 2),
                "yes_token_id": odds.yes_token_id or "",
                "no_token_id": odds.no_token_id or "",
                "condition_id": odds.condition_id,
            })

    if not csv_rows:
        print("❌ No exportable rows (all matches lack market odds with yes_token_id).")
        return

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(csv_rows)

    matches_exported = len({r["match_id"] for r in csv_rows})
    print(f"✅ Exported {matches_exported} matches → {len(csv_rows)} rows (home/draw/away)")
    print(f"   Skipped {skipped_no_odds} matches without Polymarket token IDs")
    print(f"   Window: {since_dt.date()} → {until_dt.date()}")
    print(f"   Output: {output_file}")
    print()
    print("Next steps:")
    print(f"  1. sporting-edge enrich-backtest {output_path}   # fetch real kickoff prices")
    print(f"  2. sporting-edge backtest {output_path.replace('.csv', '_enriched.csv')} --bankroll 1000")


async def _enrich_backtest(
    input_path: str,
    output_path: str,
    interval: str = "1m",
    window_minutes: int = 60,
    overwrite: bool = False,
) -> None:
    """
    Read a backtest CSV and fill missing market_yes_price values with real
    Polymarket historical prices fetched from the CLOB /prices-history endpoint.

    Required CSV columns: yes_token_id, kickoff_utc
    Populated column:     market_yes_price
    Rows without yes_token_id are written as-is with a warning.
    """
    import csv
    from datetime import datetime
    from pathlib import Path

    from sporting_edge.config.logging import configure_logging
    from sporting_edge.tools.polymarket_tools import fetch_kickoff_price

    configure_logging()

    input_file = Path(input_path)
    if not input_file.exists():
        print(f"❌ File not found: {input_path}")
        return

    with open(input_file, newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("❌ CSV is empty")
        return

    fieldnames = list(rows[0].keys())
    if "market_yes_price" not in fieldnames:
        fieldnames.append("market_yes_price")

    enriched = 0
    skipped = 0
    failed = 0

    print(f"Enriching {len(rows)} rows from {input_file.name}...")

    for i, row in enumerate(rows, 1):
        token_id = row.get("yes_token_id", "").strip()
        kickoff_str = row.get("kickoff_utc", "").strip()
        existing_price = row.get("market_yes_price", "").strip()

        if existing_price and not overwrite:
            skipped += 1
            continue

        if not token_id:
            print(f"  [{i}/{len(rows)}] ⚠️  no yes_token_id — skipping")
            failed += 1
            continue

        if not kickoff_str:
            print(f"  [{i}/{len(rows)}] ⚠️  no kickoff_utc for token {token_id[:8]} — skipping")
            failed += 1
            continue

        try:
            kickoff_utc = datetime.fromisoformat(kickoff_str)
        except ValueError:
            print(f"  [{i}/{len(rows)}] ⚠️  invalid kickoff_utc '{kickoff_str}' — skipping")
            failed += 1
            continue

        price = await fetch_kickoff_price(token_id, kickoff_utc, window_minutes=window_minutes)
        if price is None:
            print(f"  [{i}/{len(rows)}] ❌ no price found for token {token_id[:8]}")
            failed += 1
        else:
            row["market_yes_price"] = round(price, 4)
            enriched += 1
            print(f"  [{i}/{len(rows)}] ✅ {token_id[:8]}... → {price:.4f}")

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n✅ Done — enriched: {enriched}, skipped: {skipped}, failed: {failed}")
    print(f"   Output: {output_file}")


async def _inject_mock_odds(ev_discount: float = 0.12, kickoff_minutes: int = 240) -> None:
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

    future_kickoff = datetime.now(tz=timezone.utc) + timedelta(minutes=kickoff_minutes)

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
    print(f"   Kickoff at {future_kickoff.strftime('%H:%M UTC')} ({kickoff_minutes} min from now)")
    print(f"   EV discount applied: {ev_discount:.0%}")
    print(f"\n   Now run: sporting-edge run --from-db")


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


async def _apply_migrations():
    """Run all SQL migrations via SQLAlchemy — works inside or outside Docker."""
    from pathlib import Path

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    from sporting_edge.config import settings

    # Resolve migrations dir relative to this file: src/sporting_edge/cli.py
    # → go up 3 levels to project root, then into migrations/versions/
    here = Path(__file__).resolve().parent
    migrations_dir = here.parent.parent.parent / "migrations" / "versions"

    # Fallback: /app/migrations inside Docker image
    if not migrations_dir.exists():
        migrations_dir = Path("/app/migrations/versions")

    if not migrations_dir.exists():
        print(f"❌ migrations/versions/ not found (tried {migrations_dir})")
        sys.exit(1)

    migration_files = sorted(migrations_dir.glob("*.sql"))
    if not migration_files:
        print("No migration files found.")
        return

    engine = create_async_engine(settings.database_url, echo=False)
    try:
        async with engine.begin() as conn:
            for path in migration_files:
                sql = path.read_text()
                print(f"Applying: {path.name}")
                # asyncpg rejects multiple statements in one call — split by ';'
                # Strip comment-only lines before checking if a chunk is empty
                for chunk in sql.split(";"):
                    stmt = "\n".join(
                        line for line in chunk.splitlines()
                        if not line.strip().startswith("--")
                    ).strip()
                    if stmt:
                        await conn.execute(sa.text(stmt))
                print(f"✅ {path.name} applied")
    except Exception as exc:
        print(f"❌ Migration failed: {exc}")
        sys.exit(1)
    finally:
        await engine.dispose()
