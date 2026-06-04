"""
OddsAnalyzer Agent
==================
Compares model probabilities vs Polymarket implied probabilities.
Outputs MarketSignal list for every market where EV > threshold.

EV formula:
  EV = (p_model / p_market) - 1
  e.g. p_model=0.55, p_market=0.45 → EV = 0.55/0.45 - 1 = +22.2%

Filters applied (all must pass):
  1. Liquidity >= MIN_MARKET_LIQUIDITY
  2. Model confidence >= MIN_MODEL_CONFIDENCE
  3. EV >= MIN_EV_THRESHOLD
  4. Spread (yes_price + no_price) < 1.05  (tight market)
  5. Kickoff >= 30 min from now (avoid stale lines)
"""
from __future__ import annotations

from datetime import datetime, timezone

from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.db.models import SignalORM
from sporting_edge.db.session import AsyncSessionLocal
from sporting_edge.models.schemas import (
    AgentState,
    BetSide,
    MarketSignal,
    Match,
    MatchOdds,
    ModelPrediction,
    Outcome,
    SignalStrength,
)

log = get_logger(__name__)

# Max spread: YES + NO should sum close to 1.0 (no-vig = 1.0)
MAX_SPREAD = 1.06

# Betting window relative to kickoff:
#   MIN: avoid stale lines and in-play market (model no longer valid post-kickoff)
#   MAX: avoid illiquid markets days out (Polymarket liquidity builds 24-48h before)
MIN_MINUTES_TO_KICKOFF = 30
MAX_HOURS_TO_KICKOFF = 48


async def odds_analyzer_node(state: AgentState) -> AgentState:
    """LangGraph node: scan matches+odds+predictions, emit signals."""
    log.info(
        "odds_analyzer_start",
        run_id=state.run_id,
        matches=len(state.matches),
        odds_snapshots=len(state.odds),
        predictions=len(state.predictions),
    )

    # Build lookup maps
    pred_by_match: dict[str, ModelPrediction] = {
        p.match_id: p for p in state.predictions
    }
    match_by_id: dict[str, Match] = {
        m.match_id: m for m in state.matches
    }
    odds_by_match: dict[str, list[MatchOdds]] = {}
    for odds in state.odds:
        odds_by_match.setdefault(odds.match_id, []).append(odds)

    signals: list[MarketSignal] = []

    async with AsyncSessionLocal() as db:
        for match_id, match in match_by_id.items():
            pred = pred_by_match.get(match_id)
            if not pred:
                continue

            if pred.probabilities.confidence < settings.min_model_confidence:
                log.debug(
                    "low_confidence_skip",
                    match_id=match_id,
                    confidence=pred.probabilities.confidence,
                )
                continue

            match_odds = odds_by_match.get(match_id, [])
            for odds in match_odds:
                signal = _evaluate_opportunity(match, pred, odds)
                if signal:
                    signals.append(signal)
                    await _persist_signal(db, signal)
                    log.info(
                        "signal_found",
                        match=f"{match.home_team.name} vs {match.away_team.name}",
                        outcome=signal.target_outcome.value,
                        ev=f"{signal.expected_value:.1%}",
                        strength=signal.signal_strength.value,
                    )

        await db.commit()

    state.signals = signals
    state.completed_nodes.append("odds_analyzer")
    log.info("odds_analyzer_done", run_id=state.run_id, signals=len(signals))
    return state


# ── Core EV logic (pure — easy to unit test) ─────────────────────────────────

def _evaluate_opportunity(
    match: Match,
    pred: ModelPrediction,
    odds: MatchOdds,
) -> MarketSignal | None:
    """
    Evaluate a single (match, odds) pair.
    Returns MarketSignal if profitable, None otherwise.
    """
    # Filter 1: liquidity
    if odds.liquidity < settings.min_market_liquidity:
        return None

    # Filter 2: tight spread (no vig > 6%)
    spread = odds.yes_price + odds.no_price
    if spread > MAX_SPREAD:
        return None

    # Filter 3: kickoff timing — only trade within the liquid pre-match window
    now_utc = datetime.now(tz=timezone.utc)
    minutes_to_kick = (
        match.kickoff_utc.replace(tzinfo=timezone.utc) - now_utc
    ).total_seconds() / 60
    if minutes_to_kick < MIN_MINUTES_TO_KICKOFF:
        return None  # too close / already started → model invalid
    if minutes_to_kick > MAX_HOURS_TO_KICKOFF * 60:
        return None  # too far out → market illiquid, no edge yet

    # Determine bet side and model probability
    outcome = odds.outcome
    market_prob = odds.implied_probability   # = yes_price
    model_prob = pred.probabilities.for_outcome(outcome)
    bet_side = BetSide.YES

    # EV = (p_model / p_market) - 1
    if market_prob <= 0:
        return None
    ev = (model_prob / market_prob) - 1.0

    # Filter 4: EV threshold (mode- and liquidity-aware)
    threshold = _ev_threshold(odds.liquidity)
    if ev < threshold:
        # Check if the NO side has edge (model says outcome is LESS likely)
        # e.g. model says 20% but market prices it at 65% → bet NO
        no_market_prob = odds.no_price
        if no_market_prob <= 0:
            return None

        # For NO: we're betting the outcome doesn't happen
        # Our probability for NO = 1 - model_prob
        model_no_prob = 1.0 - model_prob
        ev_no = (model_no_prob / no_market_prob) - 1.0

        if ev_no < threshold:
            return None

        # Use NO side
        ev = ev_no
        market_prob = no_market_prob
        model_prob = model_no_prob
        bet_side = BetSide.NO

    edge = model_prob - market_prob

    return MarketSignal(
        match=match,
        odds=odds,
        prediction=pred,
        target_outcome=outcome,
        bet_side=bet_side,
        model_probability=model_prob,
        market_probability=market_prob,
        expected_value=ev,
        edge=edge,
        signal_strength=_classify_strength(ev),
    )


def _ev_threshold(liquidity: float) -> float:
    """
    Return the EV threshold for the current trading mode and market liquidity.

    Paper/backtest: always the base threshold (5%) so the full signal
    distribution is visible for research.

    Live: stricter threshold (8%) to absorb real-world frictions (spread,
    slippage, model error). Markets with liquidity between MIN_MARKET_LIQUIDITY
    and LOW_LIQUIDITY_THRESHOLD get an even higher bar (12%).
    """
    if settings.paper_trading:
        return settings.min_ev_threshold
    if liquidity < settings.low_liquidity_threshold:
        return settings.min_ev_threshold_low_liquidity
    return settings.min_ev_threshold_live


def _classify_strength(ev: float) -> SignalStrength:
    if ev >= 0.15:
        return SignalStrength.STRONG
    elif ev >= 0.08:
        return SignalStrength.MODERATE
    return SignalStrength.WEAK


def calculate_ev(model_prob: float, market_price: float) -> float:
    """
    Public utility: EV as fractional return.
    model_prob: our estimated probability (0-1)
    market_price: Polymarket YES price (0-1), equivalent to implied prob
    """
    if market_price <= 0:
        return -1.0
    return (model_prob / market_price) - 1.0


def identify_market_bias(match: Match, odds: MatchOdds, pred: ModelPrediction) -> list[str]:
    """
    Tag potential market inefficiency sources for logging / research.
    These are hypothesis labels — not used in signal generation logic.
    """
    biases: list[str] = []
    outcome = odds.outcome
    model_p = pred.probabilities.for_outcome(outcome)
    market_p = odds.implied_probability

    # Home bias: home team overpriced by fans
    if outcome == Outcome.HOME and market_p > model_p + 0.05:
        biases.append("HOME_BIAS_AGAINST_HOME")

    # Popular team premium (proxy: UCL or large-name leagues)
    if match.league.id == 2:  # UEFA Champions League
        biases.append("POPULAR_TEAM_PREMIUM_RISK")

    # Late line movement window (30-90 min before kickoff)
    now_utc = datetime.now(tz=timezone.utc)
    minutes_to_kick = (
        match.kickoff_utc.replace(tzinfo=timezone.utc) - now_utc
    ).total_seconds() / 60
    if 30 <= minutes_to_kick <= 90:
        biases.append("LATE_LINE_WINDOW")

    return biases


# ── DB persistence ─────────────────────────────────────────────────────────────

async def _persist_signal(db, signal: MarketSignal) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    import uuid

    stmt = pg_insert(SignalORM).values(
        id=uuid.UUID(signal.signal_id),
        match_id=signal.match.match_id,
        condition_id=signal.odds.condition_id,
        target_outcome=signal.target_outcome.value,
        bet_side=signal.bet_side.value,
        model_probability=signal.model_probability,
        market_probability=signal.market_probability,
        expected_value=signal.expected_value,
        edge=signal.edge,
        signal_strength=signal.signal_strength.value,
        acted_on=False,
    ).on_conflict_do_nothing()

    await db.execute(stmt)
