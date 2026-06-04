"""
Tests for OddsAnalyzer — EV calculations and signal generation.
No DB, no network calls.
"""
import pytest
from unittest.mock import patch
from sporting_edge.agents.odds_analyzer import (
    calculate_ev, _evaluate_opportunity, _classify_strength, _ev_threshold,
)
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


# ── EV threshold selection ────────────────────────────────────────────────────

def test_ev_threshold_paper_always_uses_base():
    """In paper trading mode the base threshold applies regardless of liquidity."""
    import sporting_edge.agents.odds_analyzer as mod
    with patch.object(mod.settings, "paper_trading", True), \
         patch.object(mod.settings, "min_ev_threshold", 0.05):
        assert _ev_threshold(liquidity=3_000) == 0.05
        assert _ev_threshold(liquidity=50_000) == 0.05


def test_ev_threshold_live_normal_liquidity():
    """In live mode with normal liquidity, use min_ev_threshold_live."""
    import sporting_edge.agents.odds_analyzer as mod
    with patch.object(mod.settings, "paper_trading", False), \
         patch.object(mod.settings, "min_ev_threshold_live", 0.08), \
         patch.object(mod.settings, "min_ev_threshold_low_liquidity", 0.12), \
         patch.object(mod.settings, "low_liquidity_threshold", 10_000):
        assert _ev_threshold(liquidity=50_000) == 0.08


def test_ev_threshold_live_low_liquidity():
    """In live mode with low liquidity, use min_ev_threshold_low_liquidity."""
    import sporting_edge.agents.odds_analyzer as mod
    with patch.object(mod.settings, "paper_trading", False), \
         patch.object(mod.settings, "min_ev_threshold_live", 0.08), \
         patch.object(mod.settings, "min_ev_threshold_low_liquidity", 0.12), \
         patch.object(mod.settings, "low_liquidity_threshold", 10_000):
        assert _ev_threshold(liquidity=7_000) == 0.12


def test_signal_accepted_paper_rejected_live(sample_match):
    """EV=6% passes paper threshold (5%) but is rejected by live threshold (8%)."""
    # yes_price=0.47, model=0.50 → EV = 0.50/0.47 - 1 ≈ 6.4%
    # Probabilities must sum to 1.0 so OutcomeProbabilities doesn't renormalize home
    odds = _make_odds(yes_price=0.47, liquidity=50_000)
    pred = _make_prediction(home=0.50, draw=0.28, away=0.22)  # sums to 1.0 → home stays 0.50

    import sporting_edge.agents.odds_analyzer as mod

    with patch.object(mod.settings, "paper_trading", True), \
         patch.object(mod.settings, "min_ev_threshold", 0.05):
        signal_paper = _evaluate_opportunity(sample_match, pred, odds)
        assert signal_paper is not None, "Should generate signal in paper mode"

    with patch.object(mod.settings, "paper_trading", False), \
         patch.object(mod.settings, "min_ev_threshold_live", 0.08), \
         patch.object(mod.settings, "min_ev_threshold_low_liquidity", 0.12), \
         patch.object(mod.settings, "low_liquidity_threshold", 10_000):
        signal_live = _evaluate_opportunity(sample_match, pred, odds)
        assert signal_live is None, "Should reject signal in live mode (EV < 8%)"


def test_signal_accepted_live_with_sufficient_ev(sample_match):
    """EV=10% passes both paper and live thresholds."""
    # yes_price=0.42, model=0.55 → EV ≈ 30.9% — well above both thresholds
    odds = _make_odds(yes_price=0.42, liquidity=50_000)
    pred = _make_prediction(home=0.55)

    import sporting_edge.agents.odds_analyzer as mod
    with patch.object(mod.settings, "paper_trading", False), \
         patch.object(mod.settings, "min_ev_threshold_live", 0.08), \
         patch.object(mod.settings, "min_ev_threshold_low_liquidity", 0.12), \
         patch.object(mod.settings, "low_liquidity_threshold", 10_000):
        signal = _evaluate_opportunity(sample_match, pred, odds)
        assert signal is not None


def test_signal_low_liquidity_live_requires_higher_ev(sample_match):
    """In live mode, markets with $7k liquidity require EV >= 12%."""
    # yes_price=0.44, model=0.50 → EV ≈ 13.6% — passes 12% bar
    odds_pass = _make_odds(yes_price=0.44, liquidity=7_000)
    # yes_price=0.47, model=0.50 → EV ≈ 6.4% — below 12% bar
    odds_fail = _make_odds(yes_price=0.47, liquidity=7_000)
    pred = _make_prediction(home=0.50)

    import sporting_edge.agents.odds_analyzer as mod
    with patch.object(mod.settings, "paper_trading", False), \
         patch.object(mod.settings, "min_ev_threshold_live", 0.08), \
         patch.object(mod.settings, "min_ev_threshold_low_liquidity", 0.12), \
         patch.object(mod.settings, "low_liquidity_threshold", 10_000):
        assert _evaluate_opportunity(sample_match, pred, odds_pass) is not None
        assert _evaluate_opportunity(sample_match, pred, odds_fail) is None
