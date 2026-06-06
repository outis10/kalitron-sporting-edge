"""
SQLAlchemy ORM models — maps to PostgreSQL tables.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer,
    String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ── Leagues & Teams ──────────────────────────────────────────────────────────

class LeagueORM(Base):
    __tablename__ = "leagues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # API-Football ID
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    country: Mapped[str] = mapped_column(String(80), nullable=False)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    matches: Mapped[list[MatchORM]] = relationship("MatchORM", back_populates="league")


class TeamORM(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)   # API-Football ID
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    country: Mapped[str] = mapped_column(String(80), default="")
    logo_url: Mapped[str | None] = mapped_column(String(500))

    home_matches: Mapped[list[MatchORM]] = relationship(
        "MatchORM", foreign_keys="MatchORM.home_team_id", back_populates="home_team"
    )
    away_matches: Mapped[list[MatchORM]] = relationship(
        "MatchORM", foreign_keys="MatchORM.away_team_id", back_populates="away_team"
    )


# ── Matches ──────────────────────────────────────────────────────────────────

class MatchORM(Base):
    __tablename__ = "matches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # fixture ID string
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), nullable=False)
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    kickoff_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    venue: Mapped[str | None] = mapped_column(String(200))

    # Result (populated after match finishes)
    home_goals: Mapped[int | None] = mapped_column(Integer)
    away_goals: Mapped[int | None] = mapped_column(Integer)
    result_outcome: Mapped[str | None] = mapped_column(String(10))  # home/draw/away

    # Enrichment flags
    lineups_available: Mapped[bool] = mapped_column(Boolean, default=False)
    injuries_fetched: Mapped[bool] = mapped_column(Boolean, default=False)

    # Raw API payload (JSONB for easy querying)
    raw_fixture_json: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    league: Mapped[LeagueORM] = relationship("LeagueORM", back_populates="matches")
    home_team: Mapped[TeamORM] = relationship(
        "TeamORM", foreign_keys=[home_team_id], back_populates="home_matches"
    )
    away_team: Mapped[TeamORM] = relationship(
        "TeamORM", foreign_keys=[away_team_id], back_populates="away_matches"
    )
    odds: Mapped[list[MarketOddsORM]] = relationship("MarketOddsORM", back_populates="match")
    predictions: Mapped[list[PredictionORM]] = relationship(
        "PredictionORM", back_populates="match"
    )


# ── Markets & Odds ────────────────────────────────────────────────────────────

class MarketOddsORM(Base):
    """Snapshot of Polymarket prices for a match outcome market."""
    __tablename__ = "market_odds"
    __table_args__ = (
        UniqueConstraint("condition_id", "fetched_at", name="uq_market_snapshot"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id"), nullable=False)
    condition_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    market_question: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(String(10), nullable=False)  # home/draw/away

    yes_price: Mapped[float] = mapped_column(Float, nullable=False)
    no_price: Mapped[float] = mapped_column(Float, nullable=False)
    volume_24h: Mapped[float] = mapped_column(Float, default=0.0)
    liquidity: Mapped[float] = mapped_column(Float, default=0.0)
    yes_token_id: Mapped[str | None] = mapped_column(String(128))
    no_token_id: Mapped[str | None] = mapped_column(String(128))

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    match: Mapped[MatchORM] = relationship("MatchORM", back_populates="odds")


# ── Predictions ───────────────────────────────────────────────────────────────

class PredictionORM(Base):
    """Model prediction for a match."""
    __tablename__ = "predictions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id"), nullable=False)
    model_version: Mapped[str] = mapped_column(String(50), nullable=False)

    prob_home: Mapped[float] = mapped_column(Float, nullable=False)
    prob_draw: Mapped[float] = mapped_column(Float, nullable=False)
    prob_away: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    factors_used: Mapped[list | None] = mapped_column(JSONB)
    reasoning: Mapped[str | None] = mapped_column(Text)

    predicted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    match: Mapped[MatchORM] = relationship("MatchORM", back_populates="predictions")


# ── Signals ───────────────────────────────────────────────────────────────────

class SignalORM(Base):
    """Identified EV > threshold opportunity."""
    __tablename__ = "signals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id"), nullable=False)
    condition_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_outcome: Mapped[str] = mapped_column(String(10), nullable=False)
    bet_side: Mapped[str] = mapped_column(String(5), nullable=False)  # YES/NO

    model_probability: Mapped[float] = mapped_column(Float, nullable=False)
    market_probability: Mapped[float] = mapped_column(Float, nullable=False)
    expected_value: Mapped[float] = mapped_column(Float, nullable=False)
    edge: Mapped[float] = mapped_column(Float, nullable=False)
    signal_strength: Mapped[str] = mapped_column(String(10), nullable=False)

    # CLOB prices at signal-detection time (populated when tokens are available)
    clob_bid: Mapped[float | None] = mapped_column(Float)
    clob_ask: Mapped[float | None] = mapped_column(Float)
    estimated_fill_price: Mapped[float | None] = mapped_column(Float)
    book_liquidity_usd: Mapped[float | None] = mapped_column(Float)

    acted_on: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ── Bets ─────────────────────────────────────────────────────────────────────

class BetORM(Base):
    """Every bet placed — paper or real."""
    __tablename__ = "bets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    signal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id"), nullable=False)
    condition_id: Mapped[str] = mapped_column(String(128), nullable=False)
    market_question: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(String(10), nullable=False)
    side: Mapped[str] = mapped_column(String(5), nullable=False)

    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    size_usd: Mapped[float] = mapped_column(Float, nullable=False)
    shares: Mapped[float] = mapped_column(Float, nullable=False)

    # Token ID of the shares we hold (needed to place the closing SELL order)
    token_id: Mapped[str | None] = mapped_column(String(128), index=True)
    # Kickoff time denormalised here so position_manager can filter without join
    kickoff_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    paper_trade: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    polymarket_order_id: Mapped[str | None] = mapped_column(String(128))
    # Populated when position is closed before resolution (take-profit / stop-loss / pre-kickoff)
    close_price: Mapped[float | None] = mapped_column(Float)
    close_reason: Mapped[str | None] = mapped_column(String(40))
    # Set True once the lineup check (stage 1) has run for this bet
    lineup_checked: Mapped[bool] = mapped_column(Boolean, default=False)

    # 'match' (1X2 pre-match) | 'outright' (tournament winner)
    bet_type: Mapped[str] = mapped_column(String(20), nullable=False, default="match")

    # Settlement tracking
    settlement_source: Mapped[str | None] = mapped_column(String(20))  # api_football | polymarket | both

    # Execution quality
    actual_fill_price: Mapped[float | None] = mapped_column(Float)  # real fill from CLOB
    # CLV tracking (populated by clv_tracker job ~10min before kickoff)
    closing_price: Mapped[float | None] = mapped_column(Float)
    clv: Mapped[float | None] = mapped_column(Float)  # closing_price - entry_price

    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pnl_usd: Mapped[float | None] = mapped_column(Float)
    settlement_price: Mapped[float | None] = mapped_column(Float)


# ── Performance ───────────────────────────────────────────────────────────────

class DailyPerformanceORM(Base):
    """Aggregated daily P&L — written by ReportAgent."""
    __tablename__ = "daily_performance"
    __table_args__ = (UniqueConstraint("date", "is_paper", name="uq_daily_perf"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_paper: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    bets_placed: Mapped[int] = mapped_column(Integer, default=0)
    bets_won: Mapped[int] = mapped_column(Integer, default=0)
    bets_lost: Mapped[int] = mapped_column(Integer, default=0)
    gross_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    bankroll_end_usd: Mapped[float] = mapped_column(Float, default=0.0)
    roi_pct: Mapped[float] = mapped_column(Float, default=0.0)
    model_accuracy: Mapped[float | None] = mapped_column(Float)
    avg_ev_signalled: Mapped[float | None] = mapped_column(Float)
