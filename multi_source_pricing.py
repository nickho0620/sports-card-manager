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


def _pool_source_avgs(avgs: list[float], counts: list[int]) -> dict:
    """
    Aggregate multiple source averages into one reliable price estimate.

    Strategy:
    - Use each source's median (most robust internal stat) weighted by its
      sample count so a source with 20 sales outweighs one with 1 sale.
    - Apply IQR outlier removal across source medians so a single rogue
      source (e.g. Heritage returning a premium auction result for a common
      card) doesn't drag the final number up.
    - Final price = weighted mean of the surviving source medians.
    - Falls back gracefully to a single source if only one exists.
    """
    if not avgs:
        return {}

    paired = sorted(zip(avgs, counts), key=lambda x: x[0])
    avgs_sorted   = [p[0] for p in paired]
    counts_sorted = [p[1] for p in paired]

    # IQR across source medians — removes outlier sources, not individual prices
    if len(avgs_sorted) >= 3:
        q1 = statistics.median(avgs_sorted[:len(avgs_sorted) // 2])
        q3 = statistics.median(avgs_sorted[(len(avgs_sorted) + 1) // 2:])
        iqr = q3 - q1
        if iqr > 0:
            lo = q1 - 1.5 * iqr
            hi = q3 + 1.5 * iqr
            filtered = [(a, c) for a, c in zip(avgs_sorted, counts_sorted) if lo <= a <= hi]
            if filtered:
                avgs_sorted   = [f[0] for f in filtered]
                counts_sorted = [f[1] for f in filtered]

    if not avgs_sorted:
        return {}

    # Weighted mean across surviving sources
    total_weight = sum(counts_sorted)
    weighted_avg = sum(a * c for a, c in zip(avgs_sorted, counts_sorted)) / total_weight
    total_count  = total_weight

    return {
        "avg":   round(weighted_avg, 2),
        "low":   round(min(avgs_sorted), 2),
        "high":  round(max(avgs_sorted), 2),
        "count": total_count,
    }


def _extract_raw(result: dict) -> tuple[float, int]:
    """Return (median_or_avg, count) for the raw price from a source result."""
    avg   = result.get("median") or result.get("avg")
    count = result.get("num_sales") or result.get("count") or 1
    return (avg, int(count)) if avg else (None, 0)


def _extract_graded(result: dict) -> tuple[float, int]:
    """Return (median_or_avg, count) for the graded price from a source result."""
    avg   = result.get("graded_avg")
    count = result.get("graded_num_sales") or 1
    return (avg, int(count)) if avg else (None, 0)


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

    # Collect raw (ungraded) avgs + counts from each source
    raw_avgs, raw_counts = [], []
    for src_result in [ebay_scrape, ebay_api, heritage_result, pwcc_result]:
        if src_result:
            avg, count = _extract_raw(src_result)
            if avg:
                raw_avgs.append(avg)
                raw_counts.append(count)

    # Collect graded avgs + counts from each source
    graded_avgs, graded_counts = [], []
    for src_result in [ebay_scrape, ebay_api, psa_result, heritage_result, pwcc_result]:
        if src_result:
            avg, count = _extract_graded(src_result)
            if avg:
                graded_avgs.append(avg)
                graded_counts.append(count)

    raw_stats     = _pool_source_avgs(raw_avgs, raw_counts)
    graded_stats  = _pool_source_avgs(graded_avgs, graded_counts)

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
