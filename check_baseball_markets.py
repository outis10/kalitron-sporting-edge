"""
Script to explore baseball markets on Polymarket via Gamma API.
No authentication required — Gamma API is public.

Usage:
    python check_baseball_markets.py
"""
import asyncio
from typing import Any

import httpx

GAMMA_URL = "https://gamma-api.polymarket.com"
MIN_LIQUIDITY = 1_000  # lower threshold to see what exists
KEYWORDS = ["baseball", "mlb", "world series", "nlcs", "alcs", "pitcher", "beisbol"]


async def fetch_markets(client: httpx.AsyncClient, keyword: str, limit: int = 100) -> list[dict]:
    resp = await client.get(
        f"{GAMMA_URL}/markets",
        params={"active": "true", "closed": "false", "limit": limit},
    )
    resp.raise_for_status()
    markets = resp.json()
    kw = keyword.lower()
    return [
        m for m in markets
        if kw in m.get("question", "").lower()
        or kw in (m.get("description") or "").lower()
        or kw in (m.get("groupItemTitle") or "").lower()
    ]


def summarize(market: dict[str, Any]) -> dict:
    tokens = market.get("tokens", [])
    yes_price = float(tokens[0].get("price", 0)) if tokens else 0.0
    no_price = float(tokens[1].get("price", 0)) if len(tokens) > 1 else 0.0
    liquidity = float(market.get("liquidity") or 0)
    volume_24h = float(market.get("volume24hr") or 0)
    volume_total = float(market.get("volume") or 0)
    end_date = market.get("endDate") or market.get("endDateIso") or "?"
    return {
        "question": market.get("question", "")[:90],
        "condition_id": market.get("condition_id", "")[:16] + "...",
        "yes_price": round(yes_price, 3),
        "no_price": round(no_price, 3),
        "spread": round(yes_price + no_price, 3),
        "liquidity_usd": round(liquidity),
        "volume_24h_usd": round(volume_24h),
        "volume_total_usd": round(volume_total),
        "end_date": end_date[:10],
    }


async def main():
    print(f"\n{'='*80}")
    print("  POLYMARKET — Baseball Market Scanner")
    print(f"  Min liquidity filter: ${MIN_LIQUIDITY:,}")
    print(f"{'='*80}\n")

    seen: set[str] = set()
    all_markets: list[dict] = []

    async with httpx.AsyncClient(timeout=20.0) as client:
        for kw in KEYWORDS:
            try:
                results = await fetch_markets(client, kw)
                for m in results:
                    cid = m.get("condition_id", "")
                    if cid and cid not in seen:
                        seen.add(cid)
                        all_markets.append(m)
                print(f"  [{kw:>12}]  {len(results)} markets found")
            except Exception as e:
                print(f"  [{kw:>12}]  ERROR: {e}")

    print(f"\n  Total unique markets found: {len(all_markets)}")

    # Filter by liquidity
    liquid = [m for m in all_markets if float(m.get("liquidity") or 0) >= MIN_LIQUIDITY]
    liquid.sort(key=lambda m: float(m.get("liquidity") or 0), reverse=True)

    print(f"  With liquidity >= ${MIN_LIQUIDITY:,}: {len(liquid)}\n")

    if not liquid:
        print("  No markets meet the liquidity threshold.\n")
        # Show top 10 by liquidity regardless
        dry = sorted(all_markets, key=lambda m: float(m.get("liquidity") or 0), reverse=True)[:10]
        if dry:
            print("  Top 10 by liquidity (any amount):\n")
            _print_table(dry)
        return

    # Bucket by liquidity tiers
    tiers = [
        ("$50k+",  50_000),
        ("$10k+",  10_000),
        ("$5k+",    5_000),
        ("$1k+",    1_000),
    ]

    for label, floor in tiers:
        bucket = [m for m in liquid if float(m.get("liquidity") or 0) >= floor]
        print(f"\n  ── {label} liquidity  ({len(bucket)} markets) ──────────────────────")
        if bucket:
            _print_table(bucket[:20])


def _print_table(markets: list[dict]):
    rows = [summarize(m) for m in markets]
    headers = ["Liquidity", "Vol 24h", "YES", "Spread", "Ends", "Question"]
    print(f"  {'Liquidity':>10}  {'Vol 24h':>9}  {'YES':>5}  {'Spread':>6}  {'Ends':<10}  Question")
    print(f"  {'-'*10}  {'-'*9}  {'-'*5}  {'-'*6}  {'-'*10}  {'-'*50}")
    for r in rows:
        liq = f"${r['liquidity_usd']:,}"
        vol = f"${r['volume_24h_usd']:,}"
        print(
            f"  {liq:>10}  {vol:>9}  {r['yes_price']:>5.3f}  {r['spread']:>6.3f}"
            f"  {r['end_date']:<10}  {r['question']}"
        )


if __name__ == "__main__":
    asyncio.run(main())
