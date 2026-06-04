"""
Backtesting Engine
==================
Replays historical matches through the prediction and EV pipeline
to validate model performance before going live.

Input data format (CSV or list of dicts):
  - match_id, home_team, away_team, league_id, kickoff_utc
  - home_goals_full, away_goals_full            (actual result)
  - home_form_w, home_form_d, home_form_l       (last 5 match record)
  - home_form_gf, home_form_ga                  (goals for/against in last 5)
  - away_form_w, away_form_d, away_form_l
  - away_form_gf, away_form_ga
  - h2h_home_wins, h2h_draws, h2h_away_wins, h2h_total
  - market_yes_price                            (Polymarket YES price at kickoff)
  - market_liquidity
  - target_outcome                              (home/draw/away — what the market is for)

The engine:
  1. Runs ModelPredictor.predict_match() on each row
  2. Runs OddsAnalyzer.calculate_ev() with market_yes_price
  3. Runs RiskManager.kelly_fraction() for sizing
  4. Simulates bet outcome based on actual result
  5. Aggregates metrics
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from sporting_edge.agents.model_predictor import predict_match
from sporting_edge.agents.odds_analyzer import calculate_ev
from sporting_edge.agents.risk_manager import kelly_fraction
from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.models.schemas import (
    HeadToHead,
    League,
    Match,
    MatchOdds,
    MatchResult,
    MatchStatus,
    Outcome,
    Team,
    TeamForm,
)

log = get_logger(__name__)


# ── Result container ──────────────────────────────────────────────────────────

@dataclass
class BetSimulation:
    match_id: str
    home_team: str
    away_team: str
    target_outcome: Outcome
    model_prob: float
    market_prob: float
    ev: float
    kelly: float
    size_usd: float
    entry_price: float
    actual_outcome: Outcome
    won: bool
    pnl_usd: float
    bankroll_after: float


@dataclass
class BacktestResult:
    total_bets: int = 0
    winning_bets: int = 0
    total_wagered: float = 0.0
    gross_pnl: float = 0.0
    final_bankroll: float = 0.0
    roi_pct: float = 0.0
    hit_rate: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    brier_score: float = 0.0         # calibration: lower = better
    avg_ev_signalled: float = 0.0
    avg_kelly: float = 0.0
    bets: list[BetSimulation] = field(default_factory=list)
    signals_found: int = 0
    signals_skipped_ev: int = 0
    signals_skipped_confidence: int = 0

    def print_report(self) -> None:
        """Pretty-print the backtest summary to stdout."""
        from rich.console import Console
        from rich.table import Table

        console = Console()
        console.print("\n[bold cyan]═══ BACKTEST REPORT ═══[/bold cyan]\n")

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Metric", style="dim")
        table.add_column("Value", style="bold")

        rows = [
            ("Total Bets Simulated", str(self.total_bets)),
            ("Signals Found", str(self.signals_found)),
            ("Signals Skipped (EV)", str(self.signals_skipped_ev)),
            ("Signals Skipped (Confidence)", str(self.signals_skipped_confidence)),
            ("Hit Rate", f"{self.hit_rate:.1%}"),
            ("ROI", f"{self.roi_pct:.2f}%"),
            ("Gross P&L", f"${self.gross_pnl:.2f}"),
            ("Total Wagered", f"${self.total_wagered:.2f}"),
            ("Final Bankroll", f"${self.final_bankroll:.2f}"),
            ("Max Drawdown", f"{self.max_drawdown_pct:.1f}%"),
            ("Sharpe Ratio", f"{self.sharpe_ratio:.3f}"),
            ("Brier Score (calibration)", f"{self.brier_score:.4f}"),
            ("Avg EV Signalled", f"{self.avg_ev_signalled:.1%}"),
            ("Avg Kelly Fraction", f"{self.avg_kelly:.4f}"),
        ]

        for metric, value in rows:
            color = "green" if ("ROI" in metric or "P&L" in metric) and float(value.replace("$", "").replace("%", "")) > 0 else "white"
            table.add_row(metric, f"[{color}]{value}[/{color}]")

        console.print(table)

    def to_csv(self, path: str | Path) -> None:
        """Export individual bet simulations to CSV."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "match_id", "home_team", "away_team", "target_outcome",
                    "model_prob", "market_prob", "ev", "kelly", "size_usd",
                    "entry_price", "actual_outcome", "won", "pnl_usd", "bankroll_after",
                ],
            )
            writer.writeheader()
            for b in self.bets:
                writer.writerow({
                    "match_id": b.match_id,
                    "home_team": b.home_team,
                    "away_team": b.away_team,
                    "target_outcome": b.target_outcome.value,
                    "model_prob": round(b.model_prob, 4),
                    "market_prob": round(b.market_prob, 4),
                    "ev": round(b.ev, 4),
                    "kelly": round(b.kelly, 4),
                    "size_usd": round(b.size_usd, 2),
                    "entry_price": round(b.entry_price, 4),
                    "actual_outcome": b.actual_outcome.value,
                    "won": b.won,
                    "pnl_usd": round(b.pnl_usd, 2),
                    "bankroll_after": round(b.bankroll_after, 2),
                })

        log.info("backtest_csv_exported", path=str(path), rows=len(self.bets))


# ── Engine ────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Replays historical match data through the model pipeline.

    Usage:
        engine = BacktestEngine(initial_bankroll=1000.0)
        result = engine.run(data)
        result.print_report()
    """

    def __init__(
        self,
        initial_bankroll: float = 1000.0,
        min_ev: float | None = None,
        min_confidence: float | None = None,
        max_kelly_fraction: float | None = None,
        max_bet_pct: float | None = None,
    ):
        self.initial_bankroll = initial_bankroll
        self.min_ev = min_ev or settings.min_ev_threshold
        self.min_confidence = min_confidence or settings.min_model_confidence
        self.max_kelly_fraction = max_kelly_fraction or settings.max_kelly_fraction
        self.max_bet_pct = max_bet_pct or settings.max_bet_pct_bankroll

    def run(self, data: list[dict[str, Any]]) -> BacktestResult:
        """
        Run the backtest on a list of match dicts.
        Each dict must match the input format described in module docstring.
        """
        result = BacktestResult(final_bankroll=self.initial_bankroll)
        bankroll = self.initial_bankroll
        bankroll_curve: list[float] = [bankroll]

        # For Brier score (calibration)
        probs: list[float] = []
        outcomes: list[float] = []

        for row in data:
            match, actual_outcome = _parse_row(row)
            target_outcome = Outcome(row["target_outcome"])
            market_yes_price = float(row["market_yes_price"])
            market_liquidity = float(row.get("market_liquidity", 10_000))

            result.signals_found += 1

            # Skip illiquid markets
            if market_liquidity < settings.min_market_liquidity:
                result.signals_skipped_ev += 1
                continue

            # Model prediction
            pred = predict_match(match)
            confidence = pred.probabilities.confidence
            model_prob = pred.probabilities.for_outcome(target_outcome)

            # Track calibration
            actually_won = 1.0 if actual_outcome == target_outcome else 0.0
            probs.append(model_prob)
            outcomes.append(actually_won)

            # Confidence filter
            if confidence < self.min_confidence:
                result.signals_skipped_confidence += 1
                continue

            # EV filter
            ev = calculate_ev(model_prob, market_yes_price)
            if ev < self.min_ev:
                result.signals_skipped_ev += 1
                continue

            # Kelly sizing
            k = kelly_fraction(model_prob, market_yes_price)
            k_adjusted = k * self.max_kelly_fraction
            max_bet = bankroll * self.max_bet_pct
            size_usd = min(k_adjusted * bankroll, max_bet)

            if size_usd < 1.0:
                continue

            # Simulate outcome
            shares = size_usd / market_yes_price
            won = actual_outcome == target_outcome
            if won:
                pnl = (1.0 - market_yes_price) * shares   # profit if YES resolves
            else:
                pnl = -size_usd                             # lose stake

            bankroll += pnl
            bankroll_curve.append(bankroll)

            sim = BetSimulation(
                match_id=match.match_id,
                home_team=match.home_team.name,
                away_team=match.away_team.name,
                target_outcome=target_outcome,
                model_prob=model_prob,
                market_prob=market_yes_price,
                ev=ev,
                kelly=k_adjusted,
                size_usd=size_usd,
                entry_price=market_yes_price,
                actual_outcome=actual_outcome,
                won=won,
                pnl_usd=pnl,
                bankroll_after=bankroll,
            )
            result.bets.append(sim)
            result.total_bets += 1
            result.total_wagered += size_usd
            if won:
                result.winning_bets += 1

        # ── Aggregate metrics ─────────────────────────────────────────────────
        result.final_bankroll = bankroll
        result.gross_pnl = bankroll - self.initial_bankroll
        result.roi_pct = (result.gross_pnl / self.initial_bankroll) * 100
        result.hit_rate = (
            result.winning_bets / result.total_bets if result.total_bets > 0 else 0.0
        )
        result.avg_ev_signalled = (
            float(np.mean([b.ev for b in result.bets])) if result.bets else 0.0
        )
        result.avg_kelly = (
            float(np.mean([b.kelly for b in result.bets])) if result.bets else 0.0
        )

        # Max drawdown
        result.max_drawdown_pct = _max_drawdown(bankroll_curve)

        # Sharpe ratio (daily returns approximation)
        pnl_series = [b.pnl_usd for b in result.bets]
        result.sharpe_ratio = _sharpe(pnl_series)

        # Brier score (calibration)
        if probs:
            result.brier_score = float(
                np.mean([(p - o) ** 2 for p, o in zip(probs, outcomes)])
            )

        return result


# ── Metrics helpers ───────────────────────────────────────────────────────────

def _max_drawdown(curve: list[float]) -> float:
    """Maximum peak-to-trough drawdown in percent."""
    if len(curve) < 2:
        return 0.0
    peak = curve[0]
    max_dd = 0.0
    for val in curve[1:]:
        if val > peak:
            peak = val
        dd = (peak - val) / peak * 100
        max_dd = max(max_dd, dd)
    return max_dd


def _sharpe(returns: list[float], risk_free: float = 0.0) -> float:
    """Simplified Sharpe ratio from a list of per-bet P&L values."""
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns)
    mean = arr.mean() - risk_free
    std = arr.std()
    if std == 0:
        return 0.0
    return float(mean / std * np.sqrt(252))   # annualised (252 trading days)


# ── Row parser ────────────────────────────────────────────────────────────────

def _parse_row(row: dict[str, Any]) -> tuple[Match, Outcome]:
    """Convert a flat dict row into (Match, actual_outcome)."""
    home_goals = int(row.get("home_goals_full", 0))
    away_goals = int(row.get("away_goals_full", 0))
    actual_outcome = (
        Outcome.HOME if home_goals > away_goals
        else Outcome.AWAY if away_goals > home_goals
        else Outcome.DRAW
    )

    home_form = TeamForm(
        team_id=int(row.get("home_team_id", 0)),
        team_name=row.get("home_team", ""),
        matches_played=int(row.get("home_form_w", 0)) + int(row.get("home_form_d", 0)) + int(row.get("home_form_l", 0)),
        wins=int(row.get("home_form_w", 0)),
        draws=int(row.get("home_form_d", 0)),
        losses=int(row.get("home_form_l", 0)),
        goals_scored=float(row.get("home_form_gf", 0)),
        goals_conceded=float(row.get("home_form_ga", 0)),
    )

    away_form = TeamForm(
        team_id=int(row.get("away_team_id", 0)),
        team_name=row.get("away_team", ""),
        matches_played=int(row.get("away_form_w", 0)) + int(row.get("away_form_d", 0)) + int(row.get("away_form_l", 0)),
        wins=int(row.get("away_form_w", 0)),
        draws=int(row.get("away_form_d", 0)),
        losses=int(row.get("away_form_l", 0)),
        goals_scored=float(row.get("away_form_gf", 0)),
        goals_conceded=float(row.get("away_form_ga", 0)),
    )

    h2h = HeadToHead(
        home_team_id=int(row.get("home_team_id", 0)),
        away_team_id=int(row.get("away_team_id", 0)),
        total_matches=int(row.get("h2h_total", 0)),
        home_wins=int(row.get("h2h_home_wins", 0)),
        draws=int(row.get("h2h_draws", 0)),
        away_wins=int(row.get("h2h_away_wins", 0)),
        home_goals=0.0,
        away_goals=0.0,
    )

    match = Match(
        match_id=str(row.get("match_id", "backtest-0")),
        league=League(
            id=int(row.get("league_id", 262)),
            name=row.get("league_name", "Liga MX"),
            country=row.get("country", "Mexico"),
            season=int(row.get("season", 2024)),
        ),
        home_team=Team(id=int(row.get("home_team_id", 1)), name=row.get("home_team", "Home")),
        away_team=Team(id=int(row.get("away_team_id", 2)), name=row.get("away_team", "Away")),
        kickoff_utc=datetime.fromisoformat(row.get("kickoff_utc", "2024-01-01T20:00:00")),
        status=MatchStatus.FINISHED,
        home_form=home_form,
        away_form=away_form,
        h2h=h2h,
        result=MatchResult(home_goals=home_goals, away_goals=away_goals),
    )

    return match, actual_outcome


# ── Convenience wrapper ────────────────────────────────────────────────────────

def run_backtest(
    data: list[dict[str, Any]] | str | Path,
    initial_bankroll: float = 1000.0,
    **kwargs,
) -> BacktestResult:
    """
    Load data and run backtest.
    data can be: list of dicts, path to CSV, or path to JSON file.
    """
    if isinstance(data, (str, Path)):
        path = Path(data)
        if path.suffix == ".csv":
            with open(path) as f:
                rows = list(csv.DictReader(f))
        elif path.suffix == ".json":
            with open(path) as f:
                rows = json.load(f)
        else:
            raise ValueError(f"Unsupported file format: {path.suffix}")
    else:
        rows = data

    engine = BacktestEngine(initial_bankroll=initial_bankroll, **kwargs)
    return engine.run(rows)
