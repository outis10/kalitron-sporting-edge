"""
Tests for OddsAnalyzer — EV calculations and signal generation.
No DB, no network calls.
"""
import pytest
from sporting_edge.agents.odds_analyzer import calculate_ev, _evaluate_opportunity, _classify_strength
from sporting_edge.models.schemas import (
    MatchOdds, ModelPrediction, Outcome, OutcomeProbabilities,
    SignalStrength, BetSide,
)
from datetime import datetime, timezone


def test_ev_positive_when_model_higher():
    """EV should be positive when model probability exceeds market."""
    ev = calculate_ev(model_prob=0.60, market_price=0.45)
    assert ev > 0
    assert abs(ev - (0.60 / 0.45 - 1)) < 0.001


def test_ev_negative_when_market_higher():
    """EV should be negative when market is more confident than model."""
    ev = calculate_ev(model_prob=0.35, market_price=0.55)
    assert ev < 0


def test_ev_zero_market_price_returns_negative_one():
    ev = calculate_ev(model_prob=0.5, market_price=0.0)
    assert ev == -1.0


def test_classify_strength():
    assert _classify_strength(0.20) == SignalStrength.STRONG
    assert _classify_strength(0.10) == SignalStrength.MODERATE
    assert _classify_strength(0.06) == SignalStrength.WEAK


def _make_odds(yes_price: float, liquidity: float = 50_000, outcome=Outcome.HOME):
    return MatchOdds(
        condition_id="test-cond-001",
        market_question="Will Club America win?",
        match_id="fixtures-12345",
        outcome=outcome,
        yes_price=yes_price,
        no_price=round(1 - yes_price, 2),
        volume_24h=10_000,
        liquidity=liquidity,
        fetched_at=datetime.now(tz=timezone.utc),
    )


def _make_prediction(home: float = 0.55, draw: float = 0.25, away: float = 0.20, conf: float = 0.75):
    return ModelPrediction(
        match_id="fixtures-12345",
        probabilities=OutcomeProbabilities(home=home, draw=draw, away=away, confidence=conf),
        model_version="v1",
    )


def test_evaluate_opportunity_finds_yes_signal(sample_match):
    """When model > market on YES side, we should get a YES signal."""
    odds = _make_odds(yes_price=0.42)    # market says 42% for home win
    pred = _make_prediction(home=0.58)   # we say 58%

    signal = _evaluate_opportunity(sample_match, pred, odds)
    assert signal is not None
    assert signal.bet_side == BetSide.YES
    assert signal.expected_value > 0


def test_evaluate_opportunity_finds_no_signal(sample_match):
    """When market overestimates YES probability, we should get a NO signal."""
    odds = _make_odds(yes_price=0.75)    # market says 75% for home win
    pred = _make_prediction(home=0.40)   # we say only 40%

    signal = _evaluate_opportunity(sample_match, pred, odds)
    # NO side: model says 60% chance home does NOT win, market gives 25%
    # EV_no = 0.60/0.25 - 1 = 1.4 → huge edge
    assert signal is not None
    assert signal.bet_side == BetSide.NO


def test_evaluate_opportunity_rejects_low_liquidity(sample_match):
    odds = _make_odds(yes_price=0.45, liquidity=1_000)  # too little liquidity
    pred = _make_prediction(home=0.65)

    signal = _evaluate_opportunity(sample_match, pred, odds)
    assert signal is None


def test_evaluate_opportunity_rejects_low_ev(sample_match):
    """Market and model agree → no signal."""
    odds = _make_odds(yes_price=0.55)
    pred = _make_prediction(home=0.56)   # almost no edge

    signal = _evaluate_opportunity(sample_match, pred, odds)
    assert signal is None
