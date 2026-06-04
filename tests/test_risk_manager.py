"""
Tests for RiskManager — Kelly criterion and bet approval logic.
"""
import pytest
from sporting_edge.agents.risk_manager import kelly_fraction, _evaluate_signal
from sporting_edge.models.schemas import (
    BetSide, MarketSignal, MatchOdds, ModelPrediction, Outcome,
    OutcomeProbabilities, SignalStrength,
)
from datetime import datetime, timezone


def test_kelly_positive_edge():
    """Kelly fraction should be positive when model probability exceeds market."""
    k = kelly_fraction(model_prob=0.60, market_price=0.45)
    assert k > 0


def test_kelly_negative_edge_returns_zero():
    """Kelly should return 0 (not negative) when there's no edge."""
    k = kelly_fraction(model_prob=0.40, market_price=0.55)
    assert k == 0.0


def test_kelly_formula_correctness():
    """Verify the formula: f = (b*p - q) / b."""
    p = 0.60
    market_price = 0.45
    b = (1 / market_price) - 1
    q = 1 - p
    expected = (b * p - q) / b

    assert abs(kelly_fraction(p, market_price) - expected) < 0.001


def test_kelly_boundary_price():
    assert kelly_fraction(0.5, 0.0) == 0.0
    assert kelly_fraction(0.5, 1.0) == 0.0


def _make_signal(ev: float = 0.15, model_prob: float = 0.60, market_prob: float = 0.45):
    from sporting_edge.models.schemas import League, Match, Team, MatchStatus
    match = Match(
        match_id="test-001",
        league=League(id=262, name="Liga MX", country="Mexico", season=2024),
        home_team=Team(id=1, name="America"),
        away_team=Team(id=2, name="Chivas"),
        kickoff_utc=datetime(2025, 5, 1, 20, tzinfo=timezone.utc),
        status=MatchStatus.SCHEDULED,
    )
    odds = MatchOdds(
        condition_id="cond-001",
        market_question="Will America win?",
        match_id="test-001",
        outcome=Outcome.HOME,
        yes_price=market_prob,
        no_price=1 - market_prob,
        liquidity=50_000,
        fetched_at=datetime.now(tz=timezone.utc),
    )
    pred = ModelPrediction(
        match_id="test-001",
        probabilities=OutcomeProbabilities(home=model_prob, draw=0.25, away=0.15, confidence=0.75),
        model_version="v1",
    )
    return MarketSignal(
        match=match,
        odds=odds,
        prediction=pred,
        target_outcome=Outcome.HOME,
        bet_side=BetSide.YES,
        model_probability=model_prob,
        market_probability=market_prob,
        expected_value=ev,
        edge=model_prob - market_prob,
        signal_strength=SignalStrength.STRONG,
    )


def _now():
    return datetime.now(tz=timezone.utc)


def test_signal_approved_with_sufficient_edge():
    signal = _make_signal(ev=0.20, model_prob=0.65, market_prob=0.45)
    decision = _evaluate_signal(
        signal=signal,
        bankroll=1000.0,
        daily_loss=0.0,
        session_pnl=0.0,
        processed_conditions=set(),
        league_count={},
        now=_now(),
    )
    assert decision.approved
    assert decision.capped_size_usd > 0
    assert decision.capped_size_usd <= 1000.0 * 0.02  # max 2% cap


def test_signal_rejected_daily_loss_limit():
    signal = _make_signal()
    decision = _evaluate_signal(
        signal=signal,
        bankroll=1000.0,
        daily_loss=-60.0,   # exceeds $50 limit
        session_pnl=0.0,
        processed_conditions=set(),
        league_count={},
        now=_now(),
    )
    assert not decision.approved
    assert "daily_loss" in decision.rejection_reason


def test_signal_rejected_session_drawdown():
    signal = _make_signal()
    decision = _evaluate_signal(
        signal=signal,
        bankroll=1000.0,
        daily_loss=0.0,
        session_pnl=-600.0,   # > 50% drawdown of $1000 bankroll
        processed_conditions=set(),
        league_count={},
        now=_now(),
    )
    assert not decision.approved
    assert "drawdown" in decision.rejection_reason


def test_signal_rejected_duplicate_condition():
    signal = _make_signal()
    decision = _evaluate_signal(
        signal=signal,
        bankroll=1000.0,
        daily_loss=0.0,
        session_pnl=0.0,
        processed_conditions={"cond-001"},  # already processed
        league_count={},
        now=_now(),
    )
    assert not decision.approved
    assert "duplicate" in decision.rejection_reason


def test_signal_rejected_league_correlation():
    signal = _make_signal()
    decision = _evaluate_signal(
        signal=signal,
        bankroll=1000.0,
        daily_loss=0.0,
        session_pnl=0.0,
        processed_conditions=set(),
        league_count={262: 3},   # already 3 bets in Liga MX
        now=_now(),
    )
    assert not decision.approved
    assert "league_correlation" in decision.rejection_reason


def test_kelly_size_capped_at_max_bet():
    """Even with very high Kelly, bet should not exceed 2% of bankroll."""
    signal = _make_signal(ev=2.0, model_prob=0.99, market_prob=0.01)
    decision = _evaluate_signal(
        signal=signal,
        bankroll=10_000.0,
        daily_loss=0.0,
        session_pnl=0.0,
        processed_conditions=set(),
        league_count={},
        now=_now(),
    )
    assert decision.approved
    assert decision.capped_size_usd <= 10_000 * 0.02 + 0.01  # max 2% + float tolerance
