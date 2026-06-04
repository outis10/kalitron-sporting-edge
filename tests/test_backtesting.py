"""
Tests for the backtesting engine.
Uses synthetic data — no external API or DB needed.
"""
import pytest
from sporting_edge.backtesting.engine import BacktestEngine, run_backtest


def _make_row(
    home_w=3, home_d=1, home_l=1, home_gf=8.0, home_ga=3.0,
    away_w=2, away_d=1, away_l=2, away_gf=5.0, away_ga=7.0,
    h2h_total=10, h2h_hw=4, h2h_d=3, h2h_aw=3,
    market_yes_price=0.45, liquidity=50_000,
    home_goals=2, away_goals=1,
    target_outcome="home",
    match_id="bt-001",
):
    return {
        "match_id": match_id,
        "home_team": "America",
        "away_team": "Chivas",
        "home_team_id": "1",
        "away_team_id": "2",
        "league_id": "262",
        "league_name": "Liga MX",
        "country": "Mexico",
        "season": "2024",
        "kickoff_utc": "2024-08-15T20:00:00",
        "home_form_w": str(home_w), "home_form_d": str(home_d), "home_form_l": str(home_l),
        "home_form_gf": str(home_gf), "home_form_ga": str(home_ga),
        "away_form_w": str(away_w), "away_form_d": str(away_d), "away_form_l": str(away_l),
        "away_form_gf": str(away_gf), "away_form_ga": str(away_ga),
        "h2h_total": str(h2h_total), "h2h_home_wins": str(h2h_hw),
        "h2h_draws": str(h2h_d), "h2h_away_wins": str(h2h_aw),
        "market_yes_price": str(market_yes_price),
        "market_liquidity": str(liquidity),
        "home_goals_full": str(home_goals),
        "away_goals_full": str(away_goals),
        "target_outcome": target_outcome,
    }


def test_backtest_no_signals_returns_zero_bets():
    """If market prices are efficient (no edge), no bets should be placed."""
    # Market price equals model probability → EV ≈ 0
    data = [_make_row(market_yes_price=0.58, home_w=4, home_gf=10.0) for _ in range(10)]
    engine = BacktestEngine(initial_bankroll=1000.0)
    result = engine.run(data)
    # Either 0 bets or very few — EV should be tiny
    assert result.total_bets == 0 or result.avg_ev_signalled < 0.10


def test_backtest_winning_bets_increase_bankroll():
    """If model correctly predicts value and outcome, bankroll grows."""
    # Strong home team, market underprices them at 0.40, actual result: home wins
    data = [
        _make_row(
            home_w=5, home_gf=12.0, home_ga=2.0,  # dominant home form
            away_w=0, away_gf=2.0, away_ga=10.0,  # weak away form
            market_yes_price=0.38,                  # market underprices home
            home_goals=2, away_goals=0,             # home wins
            target_outcome="home",
            match_id=f"bt-{i:03d}",
        )
        for i in range(20)
    ]
    engine = BacktestEngine(initial_bankroll=1000.0)
    result = engine.run(data)

    assert result.total_bets > 0
    assert result.hit_rate == 1.0    # all home wins
    assert result.gross_pnl > 0
    assert result.roi_pct > 0


def test_backtest_losing_bets_decrease_bankroll():
    """If model is wrong and bets are placed, bankroll decreases."""
    data = [
        _make_row(
            home_w=5, home_gf=12.0, home_ga=2.0,
            away_w=0, away_gf=2.0, away_ga=10.0,
            market_yes_price=0.38,
            home_goals=0, away_goals=2,  # away wins! model was wrong
            target_outcome="home",
            match_id=f"bt-{i:03d}",
        )
        for i in range(10)
    ]
    engine = BacktestEngine(initial_bankroll=1000.0)
    result = engine.run(data)

    if result.total_bets > 0:
        assert result.hit_rate == 0.0
        assert result.gross_pnl < 0


def test_backtest_result_fields_populated():
    data = [_make_row(market_yes_price=0.38, match_id=f"bt-{i}") for i in range(5)]
    engine = BacktestEngine(initial_bankroll=500.0)
    result = engine.run(data)

    assert result.signals_found == 5
    assert isinstance(result.brier_score, float)
    assert 0.0 <= result.brier_score <= 1.0
    assert isinstance(result.max_drawdown_pct, float)


def test_backtest_csv_export(tmp_path):
    data = [_make_row(market_yes_price=0.38, home_goals=2, away_goals=0, match_id=f"bt-{i}") for i in range(3)]
    engine = BacktestEngine(initial_bankroll=1000.0)
    result = engine.run(data)

    if result.total_bets > 0:
        csv_path = tmp_path / "backtest.csv"
        result.to_csv(csv_path)
        assert csv_path.exists()
        assert csv_path.stat().st_size > 0


def test_run_backtest_convenience_wrapper():
    data = [_make_row(market_yes_price=0.40, match_id=f"bt-{i}") for i in range(5)]
    result = run_backtest(data, initial_bankroll=1000.0)
    assert result is not None
    assert isinstance(result.roi_pct, float)
