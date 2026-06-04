"""
Tests for lineup-based model adjustments and two-stage position close logic.
No DB, no network calls.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from sporting_edge.agents.model_predictor import (
    LineupAdjustment,
    OutcomeProbabilities,
    ModelPrediction,
    _extract_position,
    _lineup_adjustments,
    adjust_prediction_for_lineups,
)
from sporting_edge.agents.position_manager import _should_close


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_pred(home=0.50, draw=0.25, away=0.25, conf=0.75, version="v1"):
    return ModelPrediction(
        match_id="fix-001",
        probabilities=OutcomeProbabilities(home=home, draw=draw, away=away, confidence=conf),
        model_version=version,
    )


def _make_bet(
    minutes_to_kickoff: int = 120,
    lineup_checked: bool = False,
    status: str = "paper",
):
    now = datetime.now(tz=timezone.utc)
    bet = MagicMock()
    bet.id = uuid.uuid4()
    bet.kickoff_utc = now + timedelta(minutes=minutes_to_kickoff)
    bet.lineup_checked = lineup_checked
    bet.status = status
    bet.entry_price = 0.45
    bet.shares = 10.0
    return bet


FULL_HOME_XI = [
    "Alisson (G)", "Alexander-Arnold (D)", "Matip (D)", "van Dijk (D)", "Robertson (D)",
    "Fabinho (M)", "Henderson (M)", "Thiago (M)", "Salah (F)", "Firmino (F)", "Mane (F)",
]
FULL_AWAY_XI = [
    "De Gea (G)", "Wan-Bissaka (D)", "Lindelof (D)", "Maguire (D)", "Shaw (D)",
    "McTominay (M)", "Fred (M)", "Bruno (M)", "Rashford (F)", "Martial (F)", "Greenwood (F)",
]
# 11 players, all midfielders/defenders — no position that counts as forward
NO_FORWARD_XI = [
    "Alisson (G)", "Alexander-Arnold (D)", "Matip (D)", "van Dijk (D)", "Robertson (D)",
    "Fabinho (M)", "Henderson (M)", "Thiago (M)", "Oxlade (M)", "Milner (M)", "Jones (M)",
]


# ── _extract_position ─────────────────────────────────────────────────────────

def test_extract_position_gk():
    assert _extract_position("Alisson (G)") == "G"


def test_extract_position_defender():
    assert _extract_position("van Dijk (D)") == "D"


def test_extract_position_forward():
    assert _extract_position("Salah (F)") == "F"


def test_extract_position_no_bracket():
    assert _extract_position("Unknown Player") == "?"


def test_extract_position_uppercase():
    assert _extract_position("Player (fw)") == "FW"


# ── _lineup_adjustments ───────────────────────────────────────────────────────

def test_full_lineups_no_adjustment():
    lineups = {"home": FULL_HOME_XI, "away": FULL_AWAY_XI}
    adj = _lineup_adjustments(lineups)
    assert not adj.has_adjustments
    assert adj.home_attack == 1.0
    assert adj.away_defence == 1.0


def test_missing_home_gk_penalises_home_defence():
    no_gk = [p for p in FULL_HOME_XI if "(G)" not in p]
    adj = _lineup_adjustments({"home": no_gk, "away": FULL_AWAY_XI})
    assert adj.home_defence < 1.0
    assert adj.home_defence == pytest.approx(0.85)
    assert "home_no_gk" in adj.factors


def test_missing_away_gk_penalises_away_defence():
    no_gk = [p for p in FULL_AWAY_XI if "(G)" not in p]
    adj = _lineup_adjustments({"home": FULL_HOME_XI, "away": no_gk})
    assert adj.away_defence == pytest.approx(0.85)
    assert "away_no_gk" in adj.factors


def test_no_home_forwards_penalises_home_attack():
    adj = _lineup_adjustments({"home": NO_FORWARD_XI, "away": FULL_AWAY_XI})
    assert adj.home_attack == pytest.approx(0.90)
    assert "home_no_forwards" in adj.factors


def test_incomplete_lineup_penalises_both_directions():
    short = FULL_HOME_XI[:8]  # only 8 players
    adj = _lineup_adjustments({"home": short, "away": FULL_AWAY_XI})
    assert adj.home_attack < 1.0
    assert adj.home_defence < 1.0
    assert any("home_lineup_incomplete" in f for f in adj.factors)


def test_empty_lineup_produces_no_adjustment():
    """If lineup not published yet (empty list), no adjustment."""
    adj = _lineup_adjustments({"home": [], "away": []})
    assert not adj.has_adjustments


# ── adjust_prediction_for_lineups ─────────────────────────────────────────────

def test_no_adjustment_returns_same_prediction():
    pred = _make_pred()
    lineups = {"home": FULL_HOME_XI, "away": FULL_AWAY_XI}
    updated = adjust_prediction_for_lineups(pred, lineups)
    assert updated is pred  # same object — no changes


def test_home_gk_missing_reduces_home_win_probability():
    pred = _make_pred(home=0.50, draw=0.25, away=0.25)
    no_gk = [p for p in FULL_HOME_XI if "(G)" not in p]
    updated = adjust_prediction_for_lineups(pred, {"home": no_gk, "away": FULL_AWAY_XI})
    # home_defence *= 0.85 → home net strength decreases → away win more likely
    assert updated.probabilities.home < pred.probabilities.home
    assert updated.probabilities.away > pred.probabilities.away


def test_probabilities_still_sum_to_one_after_adjustment():
    pred = _make_pred(home=0.50, draw=0.25, away=0.25)
    no_gk = [p for p in FULL_HOME_XI if "(G)" not in p]
    updated = adjust_prediction_for_lineups(pred, {"home": no_gk, "away": FULL_AWAY_XI})
    total = updated.probabilities.home + updated.probabilities.draw + updated.probabilities.away
    assert abs(total - 1.0) < 0.01


def test_confidence_reduced_after_adjustment():
    pred = _make_pred(conf=0.80)
    no_gk = [p for p in FULL_HOME_XI if "(G)" not in p]
    updated = adjust_prediction_for_lineups(pred, {"home": no_gk, "away": FULL_AWAY_XI})
    assert updated.probabilities.confidence < pred.probabilities.confidence


def test_model_version_updated():
    pred = _make_pred()
    no_gk = [p for p in FULL_HOME_XI if "(G)" not in p]
    updated = adjust_prediction_for_lineups(pred, {"home": no_gk, "away": FULL_AWAY_XI})
    assert "+lineup" in updated.model_version


# ── _should_close two-stage logic ─────────────────────────────────────────────

def _with_stages(fn, force=30, lineup=65):
    """Run fn with patched stage settings."""
    import sporting_edge.agents.position_manager as mod
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        mod.settings, "force_close_minutes_before_kickoff", force
    ), __import__("unittest.mock", fromlist=["patch"]).patch.object(
        mod.settings, "lineup_check_minutes_before_kickoff", lineup
    ):
        return fn()


def test_should_close_returns_price_check_when_far_from_kickoff():
    bet = _make_bet(minutes_to_kickoff=120)
    result = _with_stages(lambda: _should_close(bet, datetime.now(tz=timezone.utc)))
    assert result == "price_check"


def test_should_close_returns_lineup_check_in_stage1_window():
    bet = _make_bet(minutes_to_kickoff=60, lineup_checked=False)
    result = _with_stages(lambda: _should_close(bet, datetime.now(tz=timezone.utc)))
    assert result == "lineup_check"


def test_should_close_skips_lineup_check_if_already_done():
    bet = _make_bet(minutes_to_kickoff=60, lineup_checked=True)
    result = _with_stages(lambda: _should_close(bet, datetime.now(tz=timezone.utc)))
    assert result == "price_check"


def test_should_close_returns_kickoff_in_stage2_window():
    bet = _make_bet(minutes_to_kickoff=20, lineup_checked=True)
    result = _with_stages(lambda: _should_close(bet, datetime.now(tz=timezone.utc)))
    assert result == "kickoff"


def test_should_close_returns_kickoff_even_if_lineup_not_checked():
    """Stage 2 always force-closes regardless of lineup_checked."""
    bet = _make_bet(minutes_to_kickoff=15, lineup_checked=False)
    result = _with_stages(lambda: _should_close(bet, datetime.now(tz=timezone.utc)))
    assert result == "kickoff"


def test_should_close_no_kickoff_utc_falls_through_to_price_check():
    bet = _make_bet(minutes_to_kickoff=60)
    bet.kickoff_utc = None
    result = _with_stages(lambda: _should_close(bet, datetime.now(tz=timezone.utc)))
    assert result == "price_check"
