"""
OutrightAnalyzer
================
Evaluates outright WC markets and generates entry signals.

Model: FIFA ranking-based Dirichlet prior with power transform
  p_model[team] = points[team]^α / sum(points^α)

The power exponent (α=4) amplifies the spread between strong and weak
teams: a 1.6x FIFA-points advantage becomes a 6.5x probability advantage.
Without it, all 48 WC teams get ~2% — indistinguishable noise.

Signal condition (proactive):
  EV = (p_model / p_market) - 1 >= OUTRIGHT_EV_THRESHOLD

Guards:
  - p_market < MIN_MARKET_PRICE → skip (market is almost certainly right)
  - EV > MAX_CREDIBLE_EV → skip (model error, not edge)

The analyzer is also called by ShockDetector (reactive path) with
trigger="shock" — same EV logic, different entry reason.
"""
from __future__ import annotations

from sporting_edge.agents.risk_manager import kelly_fraction
from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.models.schemas import OutrightMarket, OutrightSignal

log = get_logger(__name__)

# ── Model constants ────────────────────────────────────────────────────────────

# Power exponent for FIFA points transform: p ∝ points^ALPHA
# Higher alpha → stronger teams dominate more. α=4 makes Brazil ~6x more
# likely than Iraq (vs 1.2x with raw points) which is closer to market reality.
_ALPHA = 4.0

# Teams priced below this are almost certainly correct — model can't beat
# the wisdom of $336M in liquidity on extreme tail events.
_MIN_MARKET_PRICE = 0.005  # 0.5%

# EV above this is a model error, not edge (e.g. Iraq at 3977%).
_MAX_CREDIBLE_EV = 5.0  # 500%

# ── FIFA Rankings prior (WC 2026 participants, approximate points) ─────────────
# Source: FIFA ranking as of WC 2026 qualification.
# Used to compute p_model via Dirichlet prior with power transform.
# Update this table after each ranking update for better calibration.

FIFA_POINTS: dict[str, float] = {
    # CONMEBOL
    "Argentina": 1897, "Brazil": 1852, "Uruguay": 1719, "Colombia": 1710,
    "Ecuador": 1571, "Chile": 1502, "Paraguay": 1480, "Bolivia": 1451,
    "Peru": 1495, "Venezuela": 1462,
    # UEFA
    "France": 1868, "Spain": 1847, "England": 1806, "Portugal": 1764,
    "Germany": 1747, "Netherlands": 1693, "Belgium": 1642, "Croatia": 1636,
    "Italy": 1625, "Switzerland": 1623, "Denmark": 1621, "Austria": 1608,
    "Serbia": 1575, "Poland": 1546, "Ukraine": 1530, "Turkey": 1519,
    "Romania": 1501, "Slovakia": 1498, "Hungary": 1490, "Albania": 1475,
    "Slovenia": 1468, "Czech Republic": 1462,
    # CONCACAF
    "United States": 1671, "Mexico": 1641, "Canada": 1583,
    "Costa Rica": 1498, "Panama": 1465, "Jamaica": 1445,
    "Honduras": 1432, "Guatemala": 1415,
    # CAF
    "Morocco": 1717, "Senegal": 1644, "Egypt": 1608, "Nigeria": 1576,
    "Cameroon": 1573, "Ivory Coast": 1561, "Ghana": 1544, "Algeria": 1535,
    "Tunisia": 1530, "South Africa": 1523, "Mali": 1511, "Burkina Faso": 1498,
    # AFC
    "Japan": 1712, "South Korea": 1651, "Saudi Arabia": 1603, "Iran": 1591,
    "Australia": 1572, "Qatar": 1536, "Uzbekistan": 1520, "Iraq": 1503,
    # OFC
    "New Zealand": 1098,
}

# Aliases to normalize team names from Gamma API question text
_ALIASES: dict[str, str] = {
    "Usa": "United States",
    "Us": "United States",
    "Usmnt": "United States",
    "Côte D'Ivoire": "Ivory Coast",
    "Cote D'Ivoire": "Ivory Coast",
    "Korea Republic": "South Korea",
    "Dpr Korea": "South Korea",
    "Republic Of Ireland": "Ireland",
    "Czechia": "Czech Republic",
}


def _normalize_team(name: str) -> str:
    """Normalize team name to match FIFA_POINTS keys."""
    titled = name.strip().title()
    return _ALIASES.get(titled, titled)


def compute_model_probability(
    team_name: str,
    all_markets: list[OutrightMarket],
) -> float:
    """
    Dirichlet prior: p_model[team] = points[team] / sum(points[active teams]).

    Uses only teams present in all_markets to normalize correctly —
    teams eliminated from the tournament won't appear in active markets.
    """
    active_teams = {_normalize_team(m.team_name) for m in all_markets}
    active_teams.add(_normalize_team(team_name))

    # Power transform: p ∝ points^α amplifies gap between strong/weak teams
    total_weight = sum(
        FIFA_POINTS.get(t, 1200.0) ** _ALPHA
        for t in active_teams
    )
    if total_weight == 0:
        return 1.0 / max(len(active_teams), 1)

    team_key = _normalize_team(team_name)
    team_weight = FIFA_POINTS.get(team_key, 1200.0) ** _ALPHA
    return team_weight / total_weight


def analyze_outright_markets(
    markets: list[OutrightMarket],
    trigger: str = "proactive",
) -> list[OutrightSignal]:
    """
    Evaluate each market and return OutrightSignals where EV >= threshold.

    Args:
        markets: Active outright markets from OutrightCollector (or a single
                 shocked market from ShockDetector).
        trigger: "proactive" (scheduled scan) | "shock" (reactive entry).
    """
    signals: list[OutrightSignal] = []
    threshold = settings.outright_ev_threshold

    for market in markets:
        p_model = compute_model_probability(market.team_name, markets)
        p_market = market.yes_price  # market implied probability = YES ask price

        if p_market <= 0:
            continue

        # Skip extreme tail markets — $336M in liquidity prices these correctly
        if p_market < _MIN_MARKET_PRICE:
            log.debug(
                "outright_skip_tail_market",
                team=market.team_name,
                p_market=round(p_market, 4),
                min_threshold=_MIN_MARKET_PRICE,
            )
            continue

        ev = (p_model / p_market) - 1.0

        # EV above this threshold is model error, not edge
        if ev > _MAX_CREDIBLE_EV:
            log.debug(
                "outright_skip_extreme_ev",
                team=market.team_name,
                ev=round(ev, 2),
                p_model=round(p_model, 4),
                p_market=round(p_market, 4),
            )
            continue

        log.debug(
            "outright_ev_check",
            team=market.team_name,
            p_model=round(p_model, 4),
            p_market=round(p_market, 4),
            ev=round(ev, 4),
            threshold=threshold,
        )

        if ev < threshold:
            continue

        # Kelly sizing using outright bankroll limit
        k = kelly_fraction(p_model, p_market)
        bankroll = settings.bankroll_usd
        size_usd = min(
            k * settings.max_kelly_fraction * bankroll,
            bankroll * settings.outright_max_bet_pct,
        )

        if size_usd < 1.0:
            continue

        signal = OutrightSignal(
            market=market,
            model_probability=round(p_model, 4),
            market_probability=round(p_market, 4),
            expected_value=round(ev, 4),
            kelly_fraction=round(k * settings.max_kelly_fraction, 4),
            size_usd=round(size_usd, 2),
            trigger=trigger,
        )
        signals.append(signal)
        log.info(
            "outright_signal_generated",
            team=market.team_name,
            p_model=signal.model_probability,
            p_market=signal.market_probability,
            ev=f"{ev:.1%}",
            size_usd=f"${size_usd:.2f}",
            trigger=trigger,
        )

    return signals
