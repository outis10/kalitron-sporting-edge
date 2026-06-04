"""
Tests for CLV Tracker — price capture and CLV calculation logic.
No DB, no network calls.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sporting_edge.agents.clv_tracker import (
    CLV_WINDOW_MINUTES,
    _get_closing_price,
    _minutes_to_kickoff,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_bet(
    token_id: str = "abc123",
    entry_price: float = 0.45,
    kickoff_offset_minutes: int = 70,
    closing_price: float | None = None,
    status: str = "paper",
):
    """Build a minimal BetORM-like mock."""
    now = datetime.now(tz=timezone.utc)
    bet = MagicMock()
    bet.id = uuid.uuid4()
    bet.token_id = token_id
    bet.entry_price = entry_price
    bet.kickoff_utc = now + timedelta(minutes=kickoff_offset_minutes)
    bet.closing_price = closing_price
    bet.status = status
    return bet


# ── _minutes_to_kickoff ───────────────────────────────────────────────────────

def test_minutes_to_kickoff_returns_correct_value():
    now = datetime.now(tz=timezone.utc)
    bet = _make_bet(kickoff_offset_minutes=70)
    minutes = _minutes_to_kickoff(bet, now)
    assert 69 < minutes < 71


def test_minutes_to_kickoff_handles_naive_datetime():
    now = datetime.now(tz=timezone.utc)
    bet = _make_bet(kickoff_offset_minutes=45)
    # Strip timezone to test naive datetime handling
    bet.kickoff_utc = bet.kickoff_utc.replace(tzinfo=None)
    minutes = _minutes_to_kickoff(bet, now)
    assert 44 < minutes < 46


# ── _get_closing_price ────────────────────────────────────────────────────────

def test_get_closing_price_uses_streamer_when_fresh():
    """If streamer has a fresh book, use best_ask from it."""
    snap = MagicMock()
    snap.best_ask = 0.52

    mock_streamer = MagicMock()
    mock_streamer.get_cached_book.return_value = snap

    bet = _make_bet(token_id="tok001")
    with patch("sporting_edge.agents.clv_tracker.get_streamer", return_value=mock_streamer):
        price = _get_closing_price(bet)

    assert price == 0.52
    mock_streamer.get_cached_book.assert_called_once_with("tok001", max_age_seconds=30.0)


def test_get_closing_price_falls_back_to_rest_when_cache_stale():
    """If streamer cache is stale (None), fall back to REST fetch."""
    mock_streamer = MagicMock()
    mock_streamer.get_cached_book.return_value = None  # cache miss

    bet = _make_bet(token_id="tok002")
    with patch("sporting_edge.agents.clv_tracker.get_streamer", return_value=mock_streamer), \
         patch("sporting_edge.agents.clv_tracker.fetch_token_best_ask", return_value=0.55) as mock_rest:
        price = _get_closing_price(bet)

    assert price == 0.55
    mock_rest.assert_called_once_with("tok002")


def test_get_closing_price_falls_back_to_rest_when_no_streamer():
    """If no streamer is available at all, use REST."""
    bet = _make_bet(token_id="tok003")
    with patch("sporting_edge.agents.clv_tracker.get_streamer", return_value=None), \
         patch("sporting_edge.agents.clv_tracker.fetch_token_best_ask", return_value=0.48) as mock_rest:
        price = _get_closing_price(bet)

    assert price == 0.48
    mock_rest.assert_called_once_with("tok003")


def test_get_closing_price_returns_none_when_no_token_id():
    bet = _make_bet(token_id=None)
    with patch("sporting_edge.agents.clv_tracker.get_streamer", return_value=None):
        price = _get_closing_price(bet)
    assert price is None


def test_get_closing_price_returns_none_when_rest_fails():
    bet = _make_bet(token_id="tok004")
    with patch("sporting_edge.agents.clv_tracker.get_streamer", return_value=None), \
         patch("sporting_edge.agents.clv_tracker.fetch_token_best_ask", return_value=None):
        price = _get_closing_price(bet)
    assert price is None


# ── CLV calculation ───────────────────────────────────────────────────────────

def test_clv_positive_when_closing_above_entry():
    """If market moved up, CLV > 0 — we entered cheaper."""
    entry = 0.45
    closing = 0.52
    clv = round(closing - entry, 6)
    assert clv > 0
    assert abs(clv - 0.07) < 0.0001


def test_clv_negative_when_closing_below_entry():
    """If market moved down, CLV < 0 — we paid more than market ended up."""
    entry = 0.55
    closing = 0.48
    clv = round(closing - entry, 6)
    assert clv < 0


def test_clv_zero_when_no_movement():
    entry = 0.50
    closing = 0.50
    clv = round(closing - entry, 6)
    assert clv == 0.0


# ── Window constant ───────────────────────────────────────────────────────────

def test_clv_window_is_reasonable():
    """Sanity check: window should be between 60 and 120 min."""
    assert 60 <= CLV_WINDOW_MINUTES <= 120
