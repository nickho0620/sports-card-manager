"""
Multi-source card pricing aggregator.

Combines pricing data from:
  1. eBay — sold listings scrape + Browse API (existing ebay_pricing.py)
  2. PSA Auction Prices Realized — graded sales across all major auction houses
  3. Heritage Auctions — premium auction archive
  4. PWCC / Fanatics Collect — mid-to-high-end auction sales

Strategy:
  - All sources run sequentially (to avoid rate-limit stacking)
  - Raw prices pooled across all sources → one aggregated raw stat
  - Graded prices pooled across all sources → one aggregated graded stat
  - Most recent last_sale_date/price kept across all sources
  - PSA 10 price carried through if PSA APR returns it
  - Falls back to AI estimate (same as before) if every source fails
  - Returns a dict that is a superset of what get_ebay_pricing() returned,
    so existing main.py storage code works without changes
"""

import statistics
from datetime import datetime

from ebay_pricing import (
    _get_ai_pricing,
    _get_ebay_api_pricing,
    _get_ebay_scrape_pricing,
    build_search_query,
    build_search_url,
)
from heritage_scraper import scrape_heritage
from psa_scraper import scrape_psa_apr
from pwcc_scraper import scrape_pwcc


def _pool_stats(price_lists: list[list[float]]) -> dict:
    """Merge multiple price lists and compute aggregate stats."""
    all_prices = [p for lst in price_lists for p in lst]
    if not all_prices:
        return {}
    # Remove extreme outliers (> 4× median)
    if len(all_prices) >= 5:
        med = statistics.median(all_prices)
        all_prices = [p for p in all_prices if p <= med * 4]
    if not all_prices:
        return {}
    return {
        "avg": round(statistics.mean(all_prices), 2),
        "low": round(min(all_prices), 2),
        "high": round(max(all_prices), 2),
        "count": len(all_prices),
    }


def _prices_from_result(result: dict, key: str) -> list[float]:
    """
    Reconstruct an approximate list of prices from stats stored in a result dict.
    We only have avg/low/high/count — generate synthetic spread for pooling.
    Uses count copies of avg, plus one low and one high if count > 2.
    This is imperfect but gives a reasonable weighted pool.
    """
    avg = result.get(f"{key}avg") or result.get("avg") if key == "" else result.get(f"{key}avg")
    low = result.get(f"{key}low") or result.get("low") if key == "" else result.get(f"{key}low")
    high = result.get(f"{key}high") or result.get("high") if key == "" else result.get(f"{key}high")
    count = result.get(f"{key}num_sales") or result.get("num_sales") if key == "" else result.get(f"{key}num_sales")

    if not avg:
        return []
    prices = [avg] * max(1, int(count or 1))
    if low and low != avg:
        prices.append(low)
    if high and high != avg:
        prices.append(high)
    return prices


def _raw_prices_from(result: dict) -> list[float]:
    avg = result.get("avg")
    if not avg:
        return []
    return _prices_from_result(result, "")


def _graded_prices_from(result: dict) -> list[float]:
    avg = result.get("graded_avg")
    if not avg:
        return []
    low = result.get("graded_low")
    high = result.get("graded_high")
    count = result.get("graded_num_sales", 1)
    prices = [avg] * max(1, int(count))
    if low and low != avg:
        prices.append(low)
    if high and high != avg:
        prices.append(high)
    return prices


def get_multi_source_pricing(card) -> dict | None:
    """
    Gather pricing from all sources and return an aggregated result.

    Return dict keys (superset of get_ebay_pricing output):
      avg, low, high, num_sales          — aggregated raw/ungraded
      graded_avg, graded_low,
        graded_high, graded_num_sales    — aggregated graded
      psa10_avg, psa10_low, psa10_high   — PSA 10 specific (if available)
      last_sale_price, last_sale_date,
        last_sale_source                 — most recent confirmed sale
      search_query, search_url           — for eBay manual verification link
      source                             — "multi_source" or individual source
      sources_used                       — comma-separated list
      per_source                         — {source_name: result_dict} breakdown
    """
    query = build_search_query(card)
    sources_used = []
    per_source: dict[str, dict] = {}

    # ── 1. eBay sold scrape ──────────────────────────────────────────────────
    ebay_scrape = _get_ebay_scrape_pricing(card)
    if ebay_scrape:
        per_source["ebay_sold"] = ebay_scrape
        sources_used.append("ebay_sold")

    # ── 2. eBay Browse API (active listings fallback) ────────────────────────
    ebay_api = None
    if not ebay_scrape:
        ebay_api = _get_ebay_api_pricing(card)
        if ebay_api:
            per_source["ebay_api"] = ebay_api
            sources_used.append("ebay_api")

    # ── 3. PSA Auction Prices Realized ───────────────────────────────────────
    psa_result = None
    try:
        psa_result = scrape_psa_apr(card)
    except Exception:
        pass
    if psa_result:
        per_source["psa_apr"] = psa_result
        sources_used.append("psa_apr")

    # ── 4. Heritage Auctions ─────────────────────────────────────────────────
    heritage_result = None
    try:
        heritage_result = scrape_heritage(card)
    except Exception:
        pass
    if heritage_result:
        per_source["heritage"] = heritage_result
        sources_used.append("heritage")

    # ── 5. PWCC / Fanatics Collect ───────────────────────────────────────────
    pwcc_result = None
    try:
        pwcc_result = scrape_pwcc(card)
    except Exception:
        pass
    if pwcc_result:
        per_source["pwcc"] = pwcc_result
        sources_used.append("pwcc")

    # ── Aggregate ────────────────────────────────────────────────────────────

    # Pool all raw prices
    raw_price_lists = []
    for src_result in [ebay_scrape, ebay_api, heritage_result, pwcc_result]:
        if src_result:
            prices = _raw_prices_from(src_result)
            if prices:
                raw_price_lists.append(prices)

    # Pool all graded prices
    graded_price_lists = []
    for src_result in [ebay_scrape, ebay_api, psa_result, heritage_result, pwcc_result]:
        if src_result:
            prices = _graded_prices_from(src_result)
            if prices:
                graded_price_lists.append(prices)

    raw_stats = _pool_stats(raw_price_lists)
    graded_stats = _pool_stats(graded_price_lists)

    # ── No data at all — fall back to AI estimate ────────────────────────────
    if not raw_stats and not graded_stats:
        ai = _get_ai_pricing(card)
        if ai:
            ai["sources_used"] = "ai_estimate"
            ai["per_source"] = {"ai_estimate": ai.copy()}
            return ai
        return None

    # ── Build final result ───────────────────────────────────────────────────
    result: dict = {
        "search_query": query,
        "search_url": build_search_url(query),
        "source": "multi_source" if len(sources_used) > 1 else (sources_used[0] if sources_used else "unknown"),
        "sources_used": ",".join(sources_used),
        "per_source": per_source,
    }

    if raw_stats:
        result.update({
            "avg": raw_stats["avg"],
            "low": raw_stats["low"],
            "high": raw_stats["high"],
            "num_sales": raw_stats["count"],
        })
    elif graded_stats:
        # No raw data — use graded avg as the estimated price
        result.update({
            "avg": graded_stats["avg"],
            "low": graded_stats["low"],
            "high": graded_stats["high"],
            "num_sales": graded_stats["count"],
        })

    if graded_stats:
        result.update({
            "graded_avg": graded_stats["avg"],
            "graded_low": graded_stats["low"],
            "graded_high": graded_stats["high"],
            "graded_num_sales": graded_stats["count"],
        })

    # PSA 10 — carry through from PSA APR if available
    if psa_result and psa_result.get("psa10_avg"):
        result["psa10_avg"] = psa_result["psa10_avg"]
        result["psa10_low"] = psa_result.get("psa10_low")
        result["psa10_high"] = psa_result.get("psa10_high")

    # Most recent last sale across all sources
    last_sale_candidates = []
    for src_result in per_source.values():
        if src_result.get("last_sale_price") and src_result.get("last_sale_date"):
            last_sale_candidates.append({
                "price": src_result["last_sale_price"],
                "date": src_result["last_sale_date"],
                "source": src_result.get("last_sale_source", src_result.get("source", "")),
            })

    if last_sale_candidates:
        # Pick most recent
        latest = max(
            last_sale_candidates,
            key=lambda x: x["date"] if isinstance(x["date"], datetime) else datetime.min,
        )
        result["last_sale_price"] = latest["price"]
        result["last_sale_date"] = latest["date"]
        result["last_sale_source"] = latest["source"]

    return result
