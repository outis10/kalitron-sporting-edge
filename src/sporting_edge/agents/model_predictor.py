"""
ModelPredictor Agent
====================
Implements a Dixon-Coles inspired Poisson model for football match outcomes.

Architecture:
  1. Estimate attack/defence strengths from recent form vs league average
  2. Apply home-advantage factor
  3. Integrate Poisson score matrix → 1X2 probabilities
  4. Adjust for H2H bias (if H2H sample is large enough)
  5. Output OutcomeProbabilities + confidence score

No ML framework needed — pure NumPy/SciPy. Fast, explainable, backtestable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.stats import poisson

from sporting_edge.config.logging import get_logger
from sporting_edge.db.models import PredictionORM
from sporting_edge.db.session import AsyncSessionLocal
from sporting_edge.models.schemas import (
    AgentState,
    HeadToHead,
    Match,
    ModelPrediction,
    Outcome,
    OutcomeProbabilities,
    TeamForm,
)

log = get_logger(__name__)

# ── League-average parameters (priors) ──────────────────────────────────────
# Source: historical averages for Liga MX / UEFA CL
LEAGUE_PRIORS: dict[int, "LeaguePrior"] = {}

GLOBAL_PRIOR = dict(
    avg_home_goals=1.45,
    avg_away_goals=1.10,
    home_advantage=0.15,    # extra expected goals for the home team
)

MAX_GOALS_MATRIX = 10       # Poisson matrix size (goals 0..10)
MIN_CONFIDENCE = 0.45       # floor for confidence score
H2H_WEIGHT_THRESHOLD = 5    # minimum H2H matches to apply H2H adjustment


@dataclass
class LeaguePrior:
    avg_home_goals: float = 1.45
    avg_away_goals: float = 1.10
    home_advantage: float = 0.15


# ── Main agent node ──────────────────────────────────────────────────────────

async def model_predictor_node(state: AgentState) -> AgentState:
    """LangGraph node: predicts outcomes for every match in state.matches."""
    log.info("model_predictor_start", run_id=state.run_id, matches=len(state.matches))

    predictions: list[ModelPrediction] = []

    async with AsyncSessionLocal() as db:
        for match in state.matches:
            try:
                pred = predict_match(match)
                predictions.append(pred)
                await _persist_prediction(db, pred)
            except Exception as exc:
                log.error(
                    "prediction_failed",
                    match_id=match.match_id,
                    error=str(exc),
                )

        await db.commit()

    state.predictions = predictions
    state.completed_nodes.append("model_predictor")
    log.info(
        "model_predictor_done",
        run_id=state.run_id,
        predictions=len(predictions),
    )
    return state


# ── Core prediction logic (pure Python — easy to test) ──────────────────────

def predict_match(match: Match) -> ModelPrediction:
    """
    Run the Poisson model for a single match.
    Returns ModelPrediction with 1X2 probabilities and confidence.
    """
    prior = LEAGUE_PRIORS.get(match.league.id, LeaguePrior())
    factors: list[str] = []

    # ── 1. Estimate expected goals ───────────────────────────────────────────
    home_lambda, away_lambda, data_quality = _estimate_expected_goals(
        match.home_form,
        match.away_form,
        prior,
        factors,
    )

    # ── 2. Build Poisson score matrix ────────────────────────────────────────
    home_probs, draw_prob, away_probs = _poisson_matrix(home_lambda, away_lambda)

    # ── 3. Apply H2H adjustment ──────────────────────────────────────────────
    if match.h2h and match.h2h.total_matches >= H2H_WEIGHT_THRESHOLD:
        home_probs, draw_prob, away_probs = _apply_h2h_adjustment(
            home_probs, draw_prob, away_probs, match.h2h, factors
        )

    # ── 4. Compute confidence ─────────────────────────────────────────────────
    confidence = _compute_confidence(
        home_form=match.home_form,
        away_form=match.away_form,
        h2h=match.h2h,
        data_quality=data_quality,
    )

    reasoning = _build_reasoning(
        match, home_lambda, away_lambda, home_probs, draw_prob, away_probs, factors
    )

    return ModelPrediction(
        match_id=match.match_id,
        probabilities=OutcomeProbabilities(
            home=round(home_probs, 4),
            draw=round(draw_prob, 4),
            away=round(away_probs, 4),
            confidence=round(confidence, 3),
        ),
        model_version="v1-dixon-coles",
        factors_used=factors,
        reasoning=reasoning,
    )


def _estimate_expected_goals(
    home_form: Optional[TeamForm],
    away_form: Optional[TeamForm],
    prior: LeaguePrior,
    factors: list[str],
) -> tuple[float, float, float]:
    """
    Estimate λ_home and λ_away (expected goals) using form vs league average.
    Returns (home_lambda, away_lambda, data_quality 0-1).
    """
    avg_h = prior.avg_home_goals
    avg_a = prior.avg_away_goals

    data_quality = 1.0

    # Attack strength = team's avg goals scored / league avg
    # Defence strength = team's avg goals conceded / league avg (inverted)
    if home_form and home_form.matches_played >= 1:
        home_attack = home_form.avg_goals_scored / avg_h if avg_h > 0 else 1.0
        home_defence = home_form.avg_goals_conceded / avg_a if avg_a > 0 else 1.0
        if home_form.matches_played < 3:
            # Shrink towards prior when few matches available
            w = home_form.matches_played / 3
            home_attack = w * home_attack + (1 - w) * 1.0
            home_defence = w * home_defence + (1 - w) * 1.0
            data_quality *= 0.7
        factors.append(f"home_form({home_form.matches_played}g)")
    else:
        home_attack = 1.0
        home_defence = 1.0
        data_quality *= 0.5
        factors.append("home_form(no_data)")

    if away_form and away_form.matches_played >= 1:
        away_attack = away_form.avg_goals_scored / avg_a if avg_a > 0 else 1.0
        away_defence = away_form.avg_goals_conceded / avg_h if avg_h > 0 else 1.0
        if away_form.matches_played < 3:
            w = away_form.matches_played / 3
            away_attack = w * away_attack + (1 - w) * 1.0
            away_defence = w * away_defence + (1 - w) * 1.0
            data_quality *= 0.7
        factors.append(f"away_form({away_form.matches_played}g)")
    else:
        away_attack = 1.0
        away_defence = 1.0
        data_quality *= 0.5
        factors.append("away_form(no_data)")

    # λ_home = home_attack × away_defence × league_avg_home + home_advantage
    home_lambda = (
        home_attack * away_defence * avg_h + prior.home_advantage
    )
    away_lambda = away_attack * home_defence * avg_a

    # Guard against extreme values
    home_lambda = max(0.2, min(home_lambda, 5.0))
    away_lambda = max(0.2, min(away_lambda, 5.0))

    factors.append(f"λ_home={home_lambda:.2f}")
    factors.append(f"λ_away={away_lambda:.2f}")

    return home_lambda, away_lambda, data_quality


def _poisson_matrix(home_lambda: float, away_lambda: float) -> tuple[float, float, float]:
    """
    Build the joint Poisson score probability matrix and integrate over outcomes.
    Returns (p_home_win, p_draw, p_away_win).
    """
    n = MAX_GOALS_MATRIX + 1
    # P(home goals = i) and P(away goals = j)
    home_pmf = np.array([poisson.pmf(i, home_lambda) for i in range(n)])
    away_pmf = np.array([poisson.pmf(j, away_lambda) for j in range(n)])

    # Joint matrix
    matrix = np.outer(home_pmf, away_pmf)

    # Dixon-Coles low-score correction (makes 0-0, 1-0, 0-1, 1-1 more accurate)
    rho = -0.13   # standard industry estimate
    matrix[0, 0] *= 1 - home_lambda * away_lambda * rho
    matrix[0, 1] *= 1 + home_lambda * rho
    matrix[1, 0] *= 1 + away_lambda * rho
    matrix[1, 1] *= 1 - rho

    # Renormalize
    matrix /= matrix.sum()

    p_home = float(np.tril(matrix, k=-1).sum())   # home_goals > away_goals
    p_draw = float(np.trace(matrix))
    p_away = float(np.triu(matrix, k=1).sum())

    return p_home, p_draw, p_away


def _apply_h2h_adjustment(
    p_home: float,
    p_draw: float,
    p_away: float,
    h2h: HeadToHead,
    factors: list[str],
) -> tuple[float, float, float]:
    """
    Blend model probabilities with H2H historical rates.
    Weight of H2H increases with sample size (up to 30% at 20+ matches).
    """
    n = h2h.total_matches
    if n == 0:
        return p_home, p_draw, p_away

    h2h_home = h2h.home_wins / n
    h2h_draw = h2h.draws / n
    h2h_away = h2h.away_wins / n

    # Blend weight: 10% at 5 matches, 30% at 20+ matches
    weight = min(0.30, (n - H2H_WEIGHT_THRESHOLD) / 15 * 0.20 + 0.10)

    blended_home = (1 - weight) * p_home + weight * h2h_home
    blended_draw = (1 - weight) * p_draw + weight * h2h_draw
    blended_away = (1 - weight) * p_away + weight * h2h_away

    # Renormalise
    total = blended_home + blended_draw + blended_away
    factors.append(f"h2h_adj(n={n},w={weight:.0%})")

    return blended_home / total, blended_draw / total, blended_away / total


def _compute_confidence(
    home_form: Optional[TeamForm],
    away_form: Optional[TeamForm],
    h2h: Optional[HeadToHead],
    data_quality: float,
) -> float:
    """
    Confidence score in [MIN_CONFIDENCE, 1.0].
    Penalised for missing data, rewarded for rich form + H2H.
    """
    score = 0.5   # base

    # Form completeness
    if home_form and home_form.matches_played >= 5:
        score += 0.15
    elif home_form and home_form.matches_played >= 3:
        score += 0.08

    if away_form and away_form.matches_played >= 5:
        score += 0.15
    elif away_form and away_form.matches_played >= 3:
        score += 0.08

    # xG data bonus
    if home_form and home_form.xg_for is not None:
        score += 0.05
    if away_form and away_form.xg_for is not None:
        score += 0.05

    # H2H bonus
    if h2h and h2h.total_matches >= 10:
        score += 0.10
    elif h2h and h2h.total_matches >= 5:
        score += 0.05

    # Apply data quality multiplier
    score *= data_quality

    return max(MIN_CONFIDENCE, min(score, 1.0))


def _build_reasoning(
    match: Match,
    home_lambda: float,
    away_lambda: float,
    p_home: float,
    p_draw: float,
    p_away: float,
    factors: list[str],
) -> str:
    home = match.home_team.name
    away = match.away_team.name
    return (
        f"Dixon-Coles Poisson model: {home} (λ={home_lambda:.2f}) vs {away} (λ={away_lambda:.2f}). "
        f"1X2: {p_home:.1%} / {p_draw:.1%} / {p_away:.1%}. "
        f"Factors: {', '.join(factors)}."
    )


# ── DB persistence ────────────────────────────────────────────────────────────

async def _persist_prediction(db, pred: ModelPrediction) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = pg_insert(PredictionORM).values(
        match_id=pred.match_id,
        model_version=pred.model_version,
        prob_home=pred.probabilities.home,
        prob_draw=pred.probabilities.draw,
        prob_away=pred.probabilities.away,
        confidence=pred.probabilities.confidence,
        factors_used=pred.factors_used,
        reasoning=pred.reasoning,
        predicted_at=pred.predicted_at,
    ).on_conflict_do_nothing()

    await db.execute(stmt)
