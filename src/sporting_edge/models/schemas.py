"""
Core Pydantic domain models shared across all agents.
These are in-memory data structures — see db/models.py for ORM tables.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# ── Enums ────────────────────────────────────────────────────────────────────

class MatchStatus(str, Enum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    FINISHED = "finished"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"


class Outcome(str, Enum):
    HOME = "home"
    DRAW = "draw"
    AWAY = "away"


class BetSide(str, Enum):
    YES = "YES"
    NO = "NO"


class BetStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    WON = "won"
    LOST = "lost"
    VOID = "void"
    PAPER = "paper"


class SignalStrength(str, Enum):
    STRONG = "strong"       # EV > 15%
    MODERATE = "moderate"   # EV 8-15%
    WEAK = "weak"           # EV 5-8%


# ── Football Domain ──────────────────────────────────────────────────────────

class League(BaseModel):
    id: int
    name: str
    country: str
    season: int


class Team(BaseModel):
    id: int
    name: str
    logo_url: Optional[str] = None


class TeamForm(BaseModel):
    """Last-N match results for a team."""
    team_id: int
    team_name: str
    matches_played: int
    wins: int
    draws: int
    losses: int
    goals_scored: float = Field(description="Total goals scored")
    goals_conceded: float = Field(description="Total goals conceded")
    xg_for: Optional[float] = Field(None, description="Expected goals for (if available)")
    xg_against: Optional[float] = Field(None, description="Expected goals against")

    @property
    def win_rate(self) -> float:
        if self.matches_played == 0:
            return 0.0
        return self.wins / self.matches_played

    @property
    def avg_goals_scored(self) -> float:
        if self.matches_played == 0:
            return 0.0
        return self.goals_scored / self.matches_played

    @property
    def avg_goals_conceded(self) -> float:
        if self.matches_played == 0:
            return 0.0
        return self.goals_conceded / self.matches_played


class HeadToHead(BaseModel):
    """Historical H2H record between two teams."""
    home_team_id: int
    away_team_id: int
    total_matches: int
    home_wins: int
    draws: int
    away_wins: int
    home_goals: float
    away_goals: float


class Match(BaseModel):
    """A football match — canonical data structure."""
    match_id: str = Field(description="API-Football fixture ID as string")
    league: League
    home_team: Team
    away_team: Team
    kickoff_utc: datetime
    status: MatchStatus = MatchStatus.SCHEDULED
    venue: Optional[str] = None

    # Populated after data collection
    home_form: Optional[TeamForm] = None
    away_form: Optional[TeamForm] = None
    h2h: Optional[HeadToHead] = None
    home_injuries: list[str] = Field(default_factory=list)
    away_injuries: list[str] = Field(default_factory=list)
    home_lineup: list[str] = Field(default_factory=list)   # starting XI (name + pos)
    away_lineup: list[str] = Field(default_factory=list)

    # Populated after match finishes
    result: Optional[MatchResult] = None

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class MatchResult(BaseModel):
    """Final score / outcome."""
    home_goals: int
    away_goals: int

    @property
    def outcome(self) -> Outcome:
        if self.home_goals > self.away_goals:
            return Outcome.HOME
        elif self.home_goals < self.away_goals:
            return Outcome.AWAY
        return Outcome.DRAW


# ── Market / Odds Domain ─────────────────────────────────────────────────────

class MatchOdds(BaseModel):
    """Polymarket prices for a specific match outcome."""
    condition_id: str = Field(description="Polymarket condition_id")
    market_question: str
    match_id: str
    outcome: Outcome
    yes_price: float = Field(ge=0.0, le=1.0, description="YES token price (= implied prob)")
    no_price: float = Field(ge=0.0, le=1.0, description="NO token price")
    volume_24h: float = 0.0
    liquidity: float = 0.0
    yes_token_id: Optional[str] = None
    no_token_id: Optional[str] = None
    fetched_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def implied_probability(self) -> float:
        """Market-implied probability that YES resolves."""
        return self.yes_price

    @property
    def overround(self) -> float:
        """How much vig the market has (1.0 = fair)."""
        return self.yes_price + self.no_price


# ── Prediction Domain ────────────────────────────────────────────────────────

class OutcomeProbabilities(BaseModel):
    """Model output: probability distribution over 1X2."""
    home: float = Field(ge=0.0, le=1.0)
    draw: float = Field(ge=0.0, le=1.0)
    away: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0, description="Model confidence in these probs")

    @model_validator(mode="after")
    def probs_sum_to_one(self) -> "OutcomeProbabilities":
        total = self.home + self.draw + self.away
        if abs(total - 1.0) > 0.02:
            # Normalise instead of raising
            self.home /= total
            self.draw /= total
            self.away /= total
        return self

    def for_outcome(self, outcome: Outcome) -> float:
        return {"home": self.home, "draw": self.draw, "away": self.away}[outcome.value]


class ModelPrediction(BaseModel):
    """Full model output for a match."""
    match_id: str
    probabilities: OutcomeProbabilities
    model_version: str = "v1-dixon-coles"
    factors_used: list[str] = Field(default_factory=list)
    reasoning: str = ""
    predicted_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


# ── Signal & Decision Domain ─────────────────────────────────────────────────

class MarketSignal(BaseModel):
    """Identified value opportunity (EV > threshold)."""
    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    match: Match
    odds: MatchOdds
    prediction: ModelPrediction
    target_outcome: Outcome
    bet_side: BetSide              # usually YES

    # EV calculation
    model_probability: float       # our estimated probability
    market_probability: float      # market implied probability
    expected_value: float          # EV = (model_prob * (1/market_prob)) - 1
    edge: float                    # model_prob - market_prob

    # CLOB prices at signal-detection time (None when CLOB unavailable or tokens missing)
    clob_bid: Optional[float] = None
    clob_ask: Optional[float] = None
    estimated_fill_price: Optional[float] = None
    book_liquidity_usd: Optional[float] = None

    # Meta
    signal_strength: SignalStrength
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @model_validator(mode="after")
    def classify_strength(self) -> "MarketSignal":
        ev_pct = self.expected_value * 100
        if ev_pct >= 15:
            self.signal_strength = SignalStrength.STRONG
        elif ev_pct >= 8:
            self.signal_strength = SignalStrength.MODERATE
        else:
            self.signal_strength = SignalStrength.WEAK
        return self

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class BetDecision(BaseModel):
    """RiskManager output: approved bet with sizing."""
    signal_id: str
    approved: bool
    rejection_reason: Optional[str] = None

    # Sizing (populated if approved)
    kelly_fraction: float = 0.0
    recommended_size_usd: float = 0.0
    capped_size_usd: float = 0.0   # after max-bet cap

    # Context
    current_bankroll_usd: float = 0.0
    daily_loss_so_far: float = 0.0


class BetRecord(BaseModel):
    """Persisted record of an executed or paper bet."""
    bet_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    signal_id: str
    match_id: str
    condition_id: str
    market_question: str
    outcome: Outcome
    side: BetSide
    entry_price: float
    size_usd: float
    shares: float
    token_id: Optional[str] = None       # Polymarket YES/NO token ID (for closing)
    kickoff_utc: Optional[datetime] = None  # Match kickoff (for force-close logic)
    paper_trade: bool = True
    status: BetStatus = BetStatus.PAPER
    polymarket_order_id: Optional[str] = None
    actual_fill_price: Optional[float] = None  # real execution price from CLOB response
    placed_at: datetime = Field(default_factory=datetime.utcnow)
    settled_at: Optional[datetime] = None
    pnl_usd: Optional[float] = None

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


# ── LangGraph Shared State ────────────────────────────────────────────────────

class AgentState(BaseModel):
    """
    Shared state passed between agents in the LangGraph orchestration graph.
    Each agent reads relevant fields and writes its outputs back.
    """
    # Input
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    triggered_by: str = "scheduler"    # "scheduler" | "manual" | "api"
    target_league_ids: list[int] = Field(default_factory=list)

    # Stage outputs
    matches: list[Match] = Field(default_factory=list)
    odds: list[MatchOdds] = Field(default_factory=list)
    predictions: list[ModelPrediction] = Field(default_factory=list)
    signals: list[MarketSignal] = Field(default_factory=list)
    decisions: list[BetDecision] = Field(default_factory=list)
    bets_placed: list[BetRecord] = Field(default_factory=list)

    # Control flow
    error: Optional[str] = None
    next_node: Optional[str] = None
    completed_nodes: list[str] = Field(default_factory=list)

    # Runtime metadata
    started_at: datetime = Field(default_factory=datetime.utcnow)
    messages: list[dict[str, Any]] = Field(default_factory=list)

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {datetime: lambda v: v.isoformat()}
