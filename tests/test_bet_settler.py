"""
Tests for BetSettler — settlement reconciliation logic.
No DB, no network calls.
"""
import uuid
from unittest.mock import MagicMock

import pytest

from sporting_edge.agents.bet_settler import _determine_settlement, _reconcile_settlement


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_bet(outcome: str = "home", side: str = "YES"):
    bet = MagicMock()
    bet.id = uuid.uuid4()
    bet.outcome = outcome
    bet.side = side
    bet.condition_id = "cond-abc-123"
    return bet


def _api(outcome: str, home: int = 1, away: int = 0) -> tuple:
    return (outcome, home, away)


def _poly(resolved: bool = True, winner: str | None = "YES") -> dict:
    return {"resolved": resolved, "winner": winner, "closed": resolved}


# ── _determine_settlement (existing logic, regression tests) ──────────────────

def test_yes_bet_wins_when_outcome_matches():
    assert _determine_settlement("home", "YES", "home") == 1.0


def test_yes_bet_loses_when_outcome_differs():
    assert _determine_settlement("home", "YES", "away") == 0.0


def test_no_bet_wins_when_outcome_differs():
    assert _determine_settlement("home", "NO", "draw") == 1.0


def test_no_bet_loses_when_outcome_matches():
    assert _determine_settlement("home", "NO", "home") == 0.0


# ── _reconcile_settlement ────────────────────────────────────────────────────

def test_both_sources_agree_returns_both_label():
    """API-Football and Polymarket agree → source = 'both'."""
    bet = _make_bet(outcome="home", side="YES")
    # API: home won (home_goals > away_goals), Poly: winner=YES (YES = home won)
    result = _reconcile_settlement(_api("home", 2, 0), _poly(True, "YES"), bet)
    assert result is not None
    price, source = result
    assert price == 1.0
    assert source == "both"


def test_both_sources_agree_loss():
    bet = _make_bet(outcome="home", side="YES")
    result = _reconcile_settlement(_api("away", 0, 1), _poly(True, "NO"), bet)
    assert result is not None
    price, source = result
    assert price == 0.0
    assert source == "both"


def test_sources_conflict_returns_none():
    """API says home won, Polymarket says NO — disagreement → skip."""
    bet = _make_bet(outcome="home", side="YES")
    result = _reconcile_settlement(_api("home", 2, 0), _poly(True, "NO"), bet)
    assert result is None


def test_only_api_football_returns_api_source():
    """Polymarket not resolved yet — settle from API-Football alone."""
    bet = _make_bet(outcome="home", side="YES")
    result = _reconcile_settlement(_api("home", 1, 0), _poly(False, None), bet)
    assert result is not None
    price, source = result
    assert price == 1.0
    assert source == "api_football"


def test_only_polymarket_resolved_returns_poly_source():
    """API-Football no result but Polymarket already resolved."""
    bet = _make_bet(outcome="home", side="YES")
    result = _reconcile_settlement(None, _poly(True, "YES"), bet)
    assert result is not None
    price, source = result
    assert price == 1.0
    assert source == "polymarket"


def test_neither_source_returns_none():
    """No data from either source — skip."""
    bet = _make_bet(outcome="home", side="YES")
    result = _reconcile_settlement(None, None, bet)
    assert result is None


def test_polymarket_none_response_treated_as_no_data():
    bet = _make_bet(outcome="home", side="YES")
    result = _reconcile_settlement(_api("home", 1, 0), None, bet)
    assert result is not None
    _, source = result
    assert source == "api_football"


# ── Polymarket winner → settlement_price mapping ─────────────────────────────

def test_poly_winner_yes_with_yes_bet_is_win():
    bet = _make_bet(outcome="home", side="YES")
    price, _ = _reconcile_settlement(None, _poly(True, "YES"), bet)
    assert price == 1.0


def test_poly_winner_yes_with_no_bet_is_loss():
    bet = _make_bet(outcome="home", side="NO")
    price, _ = _reconcile_settlement(None, _poly(True, "YES"), bet)
    assert price == 0.0


def test_poly_winner_no_with_yes_bet_is_loss():
    bet = _make_bet(outcome="home", side="YES")
    price, _ = _reconcile_settlement(None, _poly(True, "NO"), bet)
    assert price == 0.0


def test_poly_winner_no_with_no_bet_is_win():
    bet = _make_bet(outcome="home", side="NO")
    price, _ = _reconcile_settlement(None, _poly(True, "NO"), bet)
    assert price == 1.0


def test_poly_unresolved_market_provides_no_data():
    """Polymarket not yet resolved counts as no data."""
    bet = _make_bet(outcome="home", side="YES")
    result = _reconcile_settlement(None, _poly(False, None), bet)
    assert result is None
