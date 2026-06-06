"""
OutrightCollector
=================
Discovers and parses Polymarket outright tournament markets:
  "Will X win the 2026 FIFA World Cup?"

Calls Gamma API (public, no auth) and returns a list of OutrightMarket
objects ready for OutrightAnalyzer.

Key fields parsed from Gamma:
  - clobTokenIds[0/1] → yes_token_id / no_token_id
  - outcomePrices[0]  → yes_price
  - tokens[*].price   → best_bid / best_ask fallback
  - negRiskMarketId   → shared NegRisk pool ID
"""
from __future__ import annotations

import json
import re

import httpx

from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.models.schemas import OutrightMarket

log = get_logger(__name__)

GAMMA_URL = settings.gamma_api_url

# Regex to extract team name from "Will TEAM win the 2026 FIFA World Cup?"
_WC_QUESTION_RE = re.compile(
    r"will\s+(.+?)\s+win\s+the\s+\d{4}\s+fifa\s+world\s+cup",
    re.IGNORECASE,
)


async def collect_outright_markets(
    min_liquidity: float = 1_000.0,
) -> list[OutrightMarket]:
    """
    Fetch all active WC 2026 outright markets from Gamma API.
    Returns markets sorted by liquidity descending.
    """
    raw = await _fetch_gamma_markets()
    markets: list[OutrightMarket] = []

    for m in raw:
        parsed = _parse_market(m)
        if parsed is None:
            continue
        if parsed.liquidity < min_liquidity:
            log.debug(
                "outright_market_skipped_low_liquidity",
                team=parsed.team_name,
                liquidity=parsed.liquidity,
            )
            continue
        markets.append(parsed)

    markets.sort(key=lambda x: x.liquidity, reverse=True)
    log.info(
        "outright_collector_done",
        total=len(markets),
        teams=[m.team_name for m in markets[:8]],
    )
    return markets


async def _fetch_gamma_markets() -> list[dict]:
    """Query Gamma API for active WC outright markets."""
    results: list[dict] = []
    offset = 0
    limit = 100

    async with httpx.AsyncClient(timeout=20.0) as client:
        while True:
            resp = await client.get(
                f"{GAMMA_URL}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                },
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break

            for m in batch:
                q = m.get("question", "") or m.get("title", "")
                if _is_wc_outright(q):
                    results.append(m)

            if len(batch) < limit:
                break
            offset += limit

    log.debug("outright_gamma_raw_found", count=len(results))
    return results


def _is_wc_outright(question: str) -> bool:
    """True if the market question matches the WC winner pattern."""
    return bool(_WC_QUESTION_RE.search(question))


def _parse_market(m: dict) -> OutrightMarket | None:
    """Convert a raw Gamma market dict to OutrightMarket."""
    question = m.get("question", "") or m.get("title", "")
    match = _WC_QUESTION_RE.search(question)
    if not match:
        return None

    team_name = match.group(1).strip().title()
    condition_id = m.get("conditionId") or m.get("condition_id", "")
    if not condition_id:
        return None

    # Parse token IDs from clobTokenIds JSON string
    clob_raw = m.get("clobTokenIds", "[]")
    try:
        clob_ids: list[str] = json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw
    except (json.JSONDecodeError, TypeError):
        clob_ids = []

    if len(clob_ids) < 2:
        return None

    yes_token_id = clob_ids[0]
    no_token_id = clob_ids[1]

    # Parse outcome prices from outcomePrices JSON string
    op_raw = m.get("outcomePrices", "[]")
    try:
        outcome_prices: list[str] = json.loads(op_raw) if isinstance(op_raw, str) else op_raw
    except (json.JSONDecodeError, TypeError):
        outcome_prices = []

    yes_price = 0.0
    if outcome_prices:
        try:
            yes_price = float(outcome_prices[0])
        except (ValueError, TypeError):
            pass

    # Fallback: use tokens[0].price
    tokens = m.get("tokens", [])
    best_bid = 0.0
    best_ask = yes_price
    if tokens and yes_price == 0.0:
        try:
            yes_price = float(tokens[0].get("price", 0) or 0)
            best_ask = yes_price
        except (ValueError, TypeError):
            pass

    if yes_price <= 0 or yes_price >= 1:
        return None

    return OutrightMarket(
        condition_id=condition_id,
        question=question,
        team_name=team_name,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        yes_price=yes_price,
        best_bid=best_bid,
        best_ask=best_ask,
        liquidity=float(m.get("liquidity", 0) or 0),
        volume_24h=float(m.get("volume24hr", 0) or 0),
        neg_risk_market_id=m.get("negRiskMarketId", "") or "",
    )
