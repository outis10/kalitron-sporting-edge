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
    """
    Query Gamma API for active WC outright markets.

    Strategy:
      1. Try GET /events?slug=world-cup-winner to find the WC event ID,
         then fetch all markets for that event (fast, ~48 results).
      2. Fall back to paginated /markets search with 422-safe pagination.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        # Strategy 1: event-based lookup (fast path)
        results = await _fetch_by_event(client)
        if results:
            log.debug("outright_gamma_via_event", count=len(results))
            return results

        # Strategy 2: paginated search with error-safe pagination
        log.debug("outright_gamma_falling_back_to_pagination")
        results = await _fetch_by_pagination(client)
        log.debug("outright_gamma_raw_found", count=len(results))
        return results


async def _fetch_by_event(client: httpx.AsyncClient) -> list[dict]:
    """
    Look up the WC event on Gamma and return all its markets.
    Tries several likely event slugs.
    """
    slugs = [
        "world-cup-winner",
        "2026-fifa-world-cup-winner",
        "fifa-world-cup-2026-winner",
        "will-win-the-2026-fifa-world-cup",
    ]
    for slug in slugs:
        try:
            resp = await client.get(
                f"{GAMMA_URL}/events",
                params={"slug": slug, "active": "true"},
            )
            if resp.status_code != 200:
                continue
            events = resp.json()
            if not events:
                continue

            event = events[0] if isinstance(events, list) else events
            event_id = event.get("id") or event.get("event_id")
            if not event_id:
                continue

            # Fetch all markets for this event
            mresp = await client.get(
                f"{GAMMA_URL}/markets",
                params={"event_id": event_id, "limit": 100},
            )
            if mresp.status_code == 200:
                markets = mresp.json()
                wc_markets = [
                    m for m in markets
                    if _is_wc_outright(m.get("question", "") or m.get("title", ""))
                ]
                if wc_markets:
                    return wc_markets
        except Exception:
            continue
    return []


async def _fetch_by_pagination(client: httpx.AsyncClient) -> list[dict]:
    """
    Paginate /markets filtered by WC question text.
    Stops on 422 (max offset exceeded) or when batch is empty.
    """
    results: list[dict] = []
    offset = 0
    limit = 100
    max_offset = 5_000  # safety cap — WC markets appear in first pages

    while offset <= max_offset:
        try:
            resp = await client.get(
                f"{GAMMA_URL}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                },
            )
            if resp.status_code == 422:
                break  # API offset limit reached
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            break

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
