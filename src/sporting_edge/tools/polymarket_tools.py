"""
Polymarket API tools — Gamma (public) + CLOB (authenticated).

Uses py-clob-client-v2 (signal-to-order latency: ~17ms vs 257ms in V1).

Key features:
  - L2 API credentials derived automatically if not provided
  - Prices fetched from CLOB orderbook bid/ask, not Gamma last-trade
  - FAK (Fill-and-Kill) order type with hint_price to cut race window
  - estimate_fill() to simulate book depth before committing
  - USDC.e collateral (not native USDC) — Polygon only
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.models.schemas import MatchOdds, Outcome

log = get_logger(__name__)

GAMMA_URL = settings.gamma_api_url
CLOB_URL = settings.clob_api_url

# Maximum book consumption allowed before rejecting a live order
MAX_BOOK_CONSUMPTION_PCT = 20.0


# ── CLOB client singleton ────────────────────────────────────────────────────

_clob_client = None


def get_clob_client():
    """
    Returns an authenticated ClobClient singleton (V2).

    Authentication flow:
      1. Init ClobClient with L1 private key
      2. If explicit L2 creds in settings → pass as ApiCreds to constructor
         Else → create_or_derive_api_key() (derives from private key, ~2s)
    """
    global _clob_client
    if _clob_client is not None:
        return _clob_client

    if not settings.polymarket_private_key or not settings.polymarket_funder_address:
        return None

    try:
        from py_clob_client_v2 import ApiCreds, ClobClient

        client = ClobClient(
            host=CLOB_URL,
            chain_id=137,  # Polygon mainnet
            key=settings.polymarket_private_key,
            funder=settings.polymarket_funder_address,
            signature_type=settings.polymarket_signature_type,
        )

        if settings.clob_has_l2_creds:
            creds = ApiCreds(
                api_key=settings.polymarket_api_key,
                api_secret=settings.polymarket_api_secret,
                api_passphrase=settings.polymarket_api_passphrase,
            )
            log.info("clob_v2_l2_creds_loaded_from_env")
        else:
            # Derive creds from private key (one-time ~2s cost)
            log.info("clob_v2_deriving_l2_creds")
            creds = client.create_or_derive_api_key()
            log.info(
                "clob_v2_l2_creds_derived",
                hint="Set POLYMARKET_API_KEY/SECRET/PASSPHRASE in .env to skip derivation",
            )

        client.set_api_creds(creds)
        _clob_client = client
        return _clob_client

    except Exception as exc:
        log.warning("clob_client_init_failed", error=str(exc))
        return None


def reset_clob_client() -> None:
    """Force re-initialisation (e.g. after credential rotation)."""
    global _clob_client
    _clob_client = None


# ── Fill simulation ──────────────────────────────────────────────────────────

@dataclass
class FillEstimate:
    """Result of simulating a fill by walking the order book ask side."""
    avg_fill_price: float | None = None
    worst_fill_price: float | None = None
    best_ask: float | None = None
    fillable_notional_usd: float = 0.0
    fillable_shares: float = 0.0
    requested_notional_usd: float = 0.0
    levels_consumed: int = 0
    slippage_vs_best_ask_bps: float | None = None
    total_ask_notional_usd: float = 0.0
    book_consumption_pct: float | None = None
    fully_fillable: bool = False
    insufficient_liquidity: bool = False


def estimate_fill(asks: list[dict], notional_usd: float) -> FillEstimate:
    """
    Walk the ask side of the order book to estimate fill quality.
    asks: [{"price": float, "shares": float}, ...] sorted ascending.
    """
    est = FillEstimate(requested_notional_usd=notional_usd)

    if not asks or notional_usd <= 0:
        est.insufficient_liquidity = not asks
        return est

    est.best_ask = float(asks[0]["price"])
    est.total_ask_notional_usd = sum(
        float(lv["price"]) * float(lv["shares"]) for lv in asks
    )

    remaining = notional_usd
    total_shares = 0.0
    total_spent = 0.0

    for lv in asks:
        price = float(lv["price"])
        available = float(lv["shares"])
        if price <= 0 or available <= 0:
            continue
        level_notional = price * available
        if remaining <= level_notional:
            shares_bought = remaining / price
            total_shares += shares_bought
            total_spent += remaining
            est.worst_fill_price = price
            est.levels_consumed += 1
            remaining = 0.0
            break
        else:
            total_shares += available
            total_spent += level_notional
            est.worst_fill_price = price
            est.levels_consumed += 1
            remaining -= level_notional

    est.fillable_notional_usd = round(total_spent, 6)
    est.fillable_shares = round(total_shares, 6)
    est.fully_fillable = remaining <= 1e-9

    if total_shares > 0:
        est.avg_fill_price = round(total_spent / total_shares, 6)
        if est.best_ask and est.best_ask > 0:
            est.slippage_vs_best_ask_bps = round(
                (est.avg_fill_price - est.best_ask) / est.best_ask * 10_000, 2
            )

    if est.total_ask_notional_usd > 0:
        est.book_consumption_pct = round(
            est.fillable_notional_usd / est.total_ask_notional_usd * 100, 2
        )

    return est


def _parse_orderbook_levels(orderbook) -> tuple[list[dict], list[dict]]:
    """Convert py_clob_client OrderBookSummary to plain dicts."""
    def _convert(levels, sort_asc: bool) -> list[dict]:
        parsed = []
        for lv in (levels or []):
            try:
                parsed.append({"price": float(lv.price), "shares": float(lv.size)})
            except (AttributeError, TypeError, ValueError):
                pass
        return sorted(parsed, key=lambda x: x["price"], reverse=not sort_asc)

    bids = _convert(getattr(orderbook, "bids", []), sort_asc=False)
    asks = _convert(getattr(orderbook, "asks", []), sort_asc=True)
    return bids, asks


# ── Gamma (public) ───────────────────────────────────────────────────────────

class GammaClient:
    """Async client for Polymarket Gamma API (no auth required)."""

    async def search_football_markets(
        self, query: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{GAMMA_URL}/markets",
                params={"active": "true", "closed": "false", "limit": limit},
            )
            resp.raise_for_status()
            markets = resp.json()

        query_lower = query.lower()
        return [
            m for m in markets
            if query_lower in m.get("question", "").lower()
            or query_lower in m.get("description", "").lower()
        ]

    async def get_market(self, condition_id: str) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{GAMMA_URL}/markets",
                params={"condition_id": condition_id},
            )
            resp.raise_for_status()
            markets = resp.json()
        return markets[0] if markets else None

    async def find_match_markets(
        self, home_team: str, away_team: str
    ) -> list[dict[str, Any]]:
        queries = [
            f"{home_team} vs {away_team}",
            f"{home_team} {away_team}",
            home_team,
        ]
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        for q in queries:
            markets = await self.search_football_markets(q, limit=5)
            for m in markets:
                cid = m.get("condition_id", "")
                if cid not in seen:
                    seen.add(cid)
                    results.append(m)
        return results


def parse_market_to_odds(
    market: dict[str, Any],
    match_id: str,
    outcome: Outcome,
) -> MatchOdds | None:
    """
    Convert a Gamma market dict to MatchOdds.
    Uses Gamma yes_price as a discovery price only — execution will
    re-fetch from the CLOB orderbook before placing any order.
    """
    tokens = market.get("tokens", [])
    if len(tokens) < 2:
        return None

    yes_price = float(tokens[0].get("price", 0) or 0)
    no_price = float(tokens[1].get("price", 0) or 0)

    if yes_price <= 0 or yes_price >= 1:
        return None

    liquidity = float(market.get("liquidity", 0) or 0)
    if liquidity < settings.min_market_liquidity:
        log.debug(
            "market_skipped_low_liquidity",
            condition_id=market.get("condition_id"),
            liquidity=liquidity,
        )
        return None

    return MatchOdds(
        condition_id=market.get("condition_id", ""),
        market_question=market.get("question", ""),
        match_id=match_id,
        outcome=outcome,
        yes_price=yes_price,
        no_price=no_price,
        volume_24h=float(market.get("volume24hr", 0) or 0),
        liquidity=liquidity,
        yes_token_id=tokens[0].get("token_id"),
        no_token_id=tokens[1].get("token_id"),
        fetched_at=datetime.now(tz=timezone.utc),
    )


# ── CLOB — accurate pre-execution prices ─────────────────────────────────────

@dataclass
class ClobPrices:
    """Prices fetched directly from CLOB orderbook — used for EV and execution."""
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    yes_mid: float
    no_mid: float
    yes_asks: list[dict] = field(default_factory=list)   # for fill simulation
    no_asks: list[dict] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


def fetch_real_odds_from_clob(
    yes_token_id: str,
    no_token_id: str,
) -> ClobPrices | None:
    """
    Fetch live bid/ask from the CLOB orderbook for both YES and NO tokens.

    This replaces Gamma's `yes_price` (last-trade) with the actual
    best_ask that you'd pay to enter — critical for accurate EV calculation.

    Mirrors polymarket-trading-system/backend/services/polymarket.py:fetch_real_prices()
    """
    client = get_clob_client()
    if not client:
        log.debug("clob_client_unavailable_skipping_real_prices")
        return None

    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_yes = pool.submit(client.get_order_book, yes_token_id)
            f_no = pool.submit(client.get_order_book, no_token_id)
            yes_ob = f_yes.result(timeout=10)
            no_ob = f_no.result(timeout=10)

        if not yes_ob or not no_ob:
            return None

        yes_bids, yes_asks = _parse_orderbook_levels(yes_ob)
        no_bids, no_asks = _parse_orderbook_levels(no_ob)

        yes_bid = yes_bids[0]["price"] if yes_bids else 0.50
        yes_ask = yes_asks[0]["price"] if yes_asks else 0.50
        no_bid = no_bids[0]["price"] if no_bids else 0.50
        no_ask = no_asks[0]["price"] if no_asks else 0.50

        return ClobPrices(
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            yes_mid=(yes_bid + yes_ask) / 2,
            no_mid=(no_bid + no_ask) / 2,
            yes_asks=yes_asks,
            no_asks=no_asks,
        )

    except Exception as exc:
        log.warning("clob_real_prices_failed", error=str(exc))
        return None


# ── Single-token bid fetch (for position manager REST fallback) ───────────────

def fetch_token_best_bid(token_id: str) -> float | None:
    """
    Fetch the best bid for a single token via CLOB REST.
    Used by position_manager when the WS streamer cache is stale.
    Returns the highest bid price, or None on error.
    """
    client = get_clob_client()
    if not client:
        return None
    try:
        orderbook = client.get_order_book(token_id)
        bids, _ = _parse_orderbook_levels(orderbook)
        return bids[0]["price"] if bids else None
    except Exception as exc:
        log.warning("fetch_token_best_bid_failed", token_id=token_id[:8], error=str(exc))
        return None


# ── Order placement ───────────────────────────────────────────────────────────

def _get_tick_size(client, token_id: str) -> str:
    """Fetch tick size for a token; default '0.01' on error."""
    try:
        return str(client.get_tick_size(token_id))
    except Exception:
        return "0.01"


def _round_to_tick(price: float, tick_size: float) -> float:
    """Round price to nearest valid tick increment."""
    rounded = round(price / tick_size) * tick_size
    return round(rounded, 6)


def place_fak_order(
    token_id: str,
    side: str,          # "BUY" | "SELL"
    size_usd: float,
    hint_price: float = 0.0,
) -> dict[str, Any]:
    """
    Place a Fill-and-Kill (FAK) order — fills what it can, cancels the rest.

    FAK is preferred over GTC for football markets because:
    - GTC can fill after kickoff when the edge is gone
    - FAK executes immediately at best available prices or not at all
    - hint_price (pre-fetched best_ask) reduces the CLOB's internal book lookup,
      narrowing the race-condition window with market makers

    Paper-trading mode returns a simulated response without touching the CLOB.
    """
    if settings.paper_trading or not settings.execute_trades:
        log.info(
            "paper_fak_simulated",
            token_id=token_id,
            side=side,
            size_usd=size_usd,
            hint_price=hint_price,
        )
        return {
            "paper": True,
            "token_id": token_id,
            "side": side,
            "size_usd": size_usd,
            "hint_price": hint_price,
            "order_id": f"PAPER-{token_id[:8]}",
            "order_type": "FAK",
        }

    client = get_clob_client()
    if not client:
        raise RuntimeError("CLOB client not initialised — check POLYMARKET credentials")

    from py_clob_client_v2 import MarketOrderArgsV2, OrderType, PartialCreateOrderOptions
    from py_clob_client_v2.order_utils.model.side import Side

    tick = _get_tick_size(client, token_id)
    order_args = MarketOrderArgsV2(
        token_id=token_id,
        amount=size_usd,
        side=Side.BUY if side.upper() == "BUY" else Side.SELL,
    )
    resp = client.create_and_post_market_order(
        order_args=order_args,
        options=PartialCreateOrderOptions(tick_size=tick),
        order_type=OrderType.FAK,
    )
    log.info("fak_order_placed", token_id=token_id[:8], size_usd=size_usd, response=resp)
    return resp


def place_gtc_limit_order(
    token_id: str,
    side: str,
    price: float,
    size_usd: float,
) -> dict[str, Any]:
    """
    Place a GTC (Good-Till-Cancelled) limit order with tick-size rounding.

    Use this only for markets where you want resting liquidity (rare for
    sports). Most executions should use place_fak_order() instead.
    """
    if settings.paper_trading or not settings.execute_trades:
        log.info(
            "paper_gtc_simulated",
            token_id=token_id,
            side=side,
            price=price,
            size_usd=size_usd,
        )
        return {
            "paper": True,
            "token_id": token_id,
            "side": side,
            "price": price,
            "size_usd": size_usd,
            "order_id": f"PAPER-{token_id[:8]}",
            "order_type": "GTC",
        }

    client = get_clob_client()
    if not client:
        raise RuntimeError("CLOB client not initialised — check POLYMARKET credentials")

    from py_clob_client_v2 import OrderArgsV2, OrderType, PartialCreateOrderOptions
    from py_clob_client_v2.order_utils.model.side import Side

    tick = _get_tick_size(client, token_id)
    rounded_price = _round_to_tick(price, float(tick))
    shares = round(size_usd / rounded_price, 6)

    log.info(
        "gtc_order_preparing",
        token_id=token_id[:8],
        price_raw=price,
        price_rounded=rounded_price,
        tick=tick,
        shares=shares,
    )

    order_args = OrderArgsV2(
        token_id=token_id,
        price=rounded_price,
        size=shares,
        side=Side.BUY if side.upper() == "BUY" else Side.SELL,
    )
    result = client.create_and_post_order(
        order_args=order_args,
        options=PartialCreateOrderOptions(tick_size=tick),
        order_type=OrderType.GTC,
    )
    log.info("gtc_order_placed", token_id=token_id[:8], response=result)
    return result


# ── Account helpers ───────────────────────────────────────────────────────────

def get_usdc_balance() -> float | None:
    """
    Fetch USDC balance allowance from CLOB.
    Handles on-chain base-unit encoding (1 USDC = 1_000_000 units).

    IMPORTANT: Polymarket CLOB only accepts USDC.e (bridged USDC on Polygon),
    NOT native USDC. AssetType.COLLATERAL maps to USDC.e on Polygon.
    Ensure your wallet holds USDC.e, not native USDC — they are different tokens.
    Bridge at: https://wallet.polygon.technology/ (USDC → USDC.e)
    """
    client = get_clob_client()
    if not client:
        return None

    try:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        # AssetType.COLLATERAL = USDC.e on Polygon (not native USDC)
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=settings.polymarket_signature_type,
        )
        raw = client.get_balance_allowance(params)
        value = _extract_numeric(raw)
        if value is None:
            return None
        # USDC uses 6 decimals on-chain
        return value / 1_000_000 if abs(value) >= 1_000_000 else value

    except Exception as exc:
        log.warning("get_balance_failed", error=str(exc))
        return None


def _extract_numeric(payload: Any) -> float | None:
    """Recursively extract the first numeric value from a nested payload."""
    if payload is None:
        return None
    if isinstance(payload, (int, float)):
        return float(payload)
    if isinstance(payload, str):
        try:
            return float(payload)
        except ValueError:
            return None
    priority = ("balance", "available_balance", "available", "allowance", "cash", "total")
    if isinstance(payload, dict):
        for key in priority:
            if key in payload:
                result = _extract_numeric(payload[key])
                if result is not None:
                    return result
        for v in payload.values():
            result = _extract_numeric(v)
            if result is not None:
                return result
    for attr in ("balance", "available", "allowance"):
        if hasattr(payload, attr):
            result = _extract_numeric(getattr(payload, attr))
            if result is not None:
                return result
    return None


def cancel_order(order_id: str) -> bool:
    """Cancel an open order by ID."""
    client = get_clob_client()
    if not client:
        return False
    try:
        client.cancel_order(order_id)
        log.info("order_cancelled", order_id=order_id)
        return True
    except Exception as exc:
        log.warning("cancel_order_failed", order_id=order_id, error=str(exc))
        return False
