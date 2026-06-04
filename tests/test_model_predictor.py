"""
Tests for ModelPredictor — the core probability engine.
No DB, no API calls needed.
"""
import pytest
from sporting_edge.agents.model_predictor import (
    predict_match,
    _poisson_matrix,
    _estimate_expected_goals,
    LeaguePrior,
)
from sporting_edge.agents.risk_manager import kelly_fraction
from sporting_edge.models.schemas import Match, Outcome


def test_poisson_matrix_sums_to_one():
    """P(home win) + P(draw) + P(away win) must equal 1."""
    p_home, p_draw, p_away = _poisson_matrix(1.4, 1.1)
    assert abs(p_home + p_draw + p_away - 1.0) < 0.001


def test_poisson_matrix_home_favoured_when_higher_lambda():
    """Higher home lambda → higher home win probability."""
    p_home_str, _, p_away_str = _poisson_matrix(2.5, 0.8)
    p_home_even, _, p_away_even = _poisson_matrix(1.4, 1.4)
    assert p_home_str > p_away_str
    assert abs(p_home_even - p_away_even) < 0.02   # symmetric


def test_predict_match_returns_valid_probs(sample_match):
    """Full prediction pipeline returns probabilities summing to 1."""
    pred = predict_match(sample_match)
    probs = pred.probabilities
    total = probs.home + probs.draw + probs.away
    assert abs(total - 1.0) < 0.01
    assert 0 <= probs.confidence <= 1


def test_predict_match_home_favoured_with_strong_home_form(sample_match):
    """America (strong form at home) should be predicted to win more often."""
    pred = predict_match(sample_match)
    # America averages 1.8 goals/game, Chivas 1.2 — home should win most often
    assert pred.probabilities.home > pred.probabilities.away


def test_predict_match_no_form_uses_prior():
    """Prediction works even when form data is missing (uses league prior)."""
    from datetime import datetime, timezone
    from sporting_edge.models.schemas import League, Team

    match = Match(
        match_id="test-no-form",
        league=League(id=262, name="Liga MX", country="Mexico", season=2024),
        home_team=Team(id=10, name="Team A"),
        away_team=Team(id=11, name="Team B"),
        kickoff_utc=datetime(2025, 5, 1, 20, tzinfo=timezone.utc),
        home_form=None,
        away_form=None,
        h2h=None,
    )
    pred = predict_match(match)
    assert pred.probabilities.confidence < 0.7    # penalised for missing data
    assert pred.probabilities.home > 0
    assert pred.probabilities.draw > 0
    assert pred.probabilities.away > 0


def test_predict_match_factors_logged(sample_match):
    """factors_used should include xG and form descriptions."""
    pred = predict_match(sample_match)
    assert len(pred.factors_used) > 0
    assert any("home_form" in f for f in pred.factors_used)
    assert any("away_form" in f for f in pred.factors_used)


def test_predict_match_reasoning_non_empty(sample_match):
    pred = predict_match(sample_match)
    assert len(pred.reasoning) > 20
