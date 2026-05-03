"""
PWCC / Fanatics Collect sales history scraper.

PWCC (now owned by Fanatics) handles mid-to-high-end card auctions and
publishes a public sales history tool. Their site is a JavaScript SPA,
so this scraper tries two approaches:

  1. Reverse-engineered JSON API  — looks for XHR endpoints that the SPA
     uses internally to load search results.
  2. Embedded JSON in page HTML  — Next.js / React SPAs often embed data
     as __NEXT_DATA__ or similar JSON blocks.

Both approaches fail gracefully — the aggregator treats this source as
optional and continues without it if unavailable.

NOTE: If both approaches stop working (Fanatics update their SPA),
open DevTools → Network → XHR on sales-history.fanaticscollect.com,
search for a card, and find the API endpoint. Update PWCC_API_CANDIDATES
with the new URL pattern.
"""

import json
import re
import statistics
import time
from datetime import datetime
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

# Both old and new domains
PWCC_HISTORY_URLS = [
    "https://sales-history.fanaticscollect.com/",
    "https://sales-history.pwccmarketplace.com/",
]

# Known / likely internal API endpoint patterns (update if SPA changes)
PWCC_API_CANDIDATES = [
    "https://sales-history.fanaticscollect.com/api/search",
    "https://sales-history.pwccmarketplace.com/api/search",
    "https://api.fanaticscollect.com/v1/sales/search",
    "https://api.pwccmarketplace.com/v1/search",
    "https://www.pwccmarketplace.com/api/sales-history/search",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

GRADED_RE = re.compile(
    r"\b(psa|bgs|sgc|cgc|beckett|graded|slab)\b", re.IGNORECASE
)
LOT_RE = re.compile(r"\b(lot|bundle|set\s+of|\d+\s+cards?)\b", re.IGNORECASE)


def _build_query(card) -> str:
    parts = []
    if card.year:
        parts.append(str(card.year))
    if card.brand:
        parts.append(card.brand)
    if card.player_name:
        parts.append(card.player_name)
    if card.parallel_name:
        parts.append(card.parallel_name)
    return " ".join(parts)


def _calc_stats(prices: list[float]) -> dict:
    if not prices:
        return {}
    if len(prices) >= 5:
        med = statistics.median(prices)
        prices = [p for p in prices if p <= med * 4]
    if not prices:
        return {}
    return {
        "avg": round(statistics.mean(prices), 2),
        "low": round(min(prices), 2),
        "high": round(max(prices), 2),
        "count": len(prices),
    }


def _parse_price(text) -> float | None:
    if isinstance(text, (int, float)):
        val = float(text)
        return val if 0.50 < val < 10_000_000 else None
    text = str(text)
    m = re.search(r"\$?([0-9,]+\.?[0-9]*)", text.replace(",", ""))
    if m:
        try:
            val = float(m.group(1))
            return val if 0.50 < val < 10_000_000 else None
        except ValueError:
            pass
    return None


def _parse_date(text) -> datetime | None:
    if isinstance(text, datetime):
        return text
    text = str(text).strip()
    # ISO format: 2024-03-15T...
    m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            pass
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _extract_from_json(data) -> list[dict]:
    """
    Walk a JSON response (dict or list) and extract sale records.
    Looks for common field name patterns used by card marketplaces.
    """
    sales = []

    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # Common wrapper keys
        for key in ("results", "items", "data", "sales", "lots", "hits", "records"):
            if key in data and isinstance(data[key], list):
                items = data[key]
                break
        if not items:
            # Nested: {"data": {"results": [...]}}
            for v in data.values():
                if isinstance(v, dict):
                    for key in ("results", "items", "data", "sales", "lots"):
                        if key in v and isinstance(v[key], list):
                            items = v[key]
                            break
                if items:
                    break

    for item in items:
        if not isinstance(item, dict):
            continue

        sale = {}

        # Price — look for common field names
        for pkey in ("salePrice", "sale_price", "price", "purchasePrice",
                     "purchase_price", "soldPrice", "sold_price", "amount",
                     "realizedPrice", "realized_price"):
            if pkey in item:
                val = _parse_price(item[pkey])
                if val:
                    sale["price"] = val
                    break

        if "price" not in sale:
            continue

        # Title
        for tkey in ("title", "name", "description", "cardDescription",
                     "card_description", "subject", "lot_title"):
            if tkey in item and item[tkey]:
                sale["title"] = str(item[tkey])
                break

        # Date
        for dkey in ("saleDate", "sale_date", "soldDate", "sold_date",
                     "endDate", "end_date", "date", "auctionDate"):
            if dkey in item:
                d = _parse_date(item[dkey])
                if d:
                    sale["date"] = d
                    break

        # Grade
        for gkey in ("grade", "gradeName", "grade_name", "psaGrade"):
            if gkey in item and item[gkey]:
                sale["grade"] = str(item[gkey])
                break

        sales.append(sale)

    return sales


def _try_api_endpoints(session: requests.Session, query: str) -> list[dict]:
    """Try known/likely API endpoint patterns with the search query."""
    params_variants = [
        {"q": query, "page": 1, "per_page": 50},
        {"query": query, "page": 1, "limit": 50},
        {"search": query, "page": 1},
        {"q": query, "sold": "true", "limit": 50},
    ]

    for url in PWCC_API_CANDIDATES:
        for params in params_variants:
            try:
                time.sleep(1)
                resp = session.get(
                    url,
                    params=params,
                    headers={**HEADERS, "Accept": "application/json"},
                    timeout=15,
                )
                if resp.status_code != 200:
                    continue
                ct = resp.headers.get("content-type", "")
                if "json" not in ct:
                    continue
                data = resp.json()
                sales = _extract_from_json(data)
                if sales:
                    return sales
            except Exception:
                continue

    return []


def _try_embedded_json(session: requests.Session, query: str) -> list[dict]:
    """
    Load the PWCC/Fanatics sales history page and look for
    __NEXT_DATA__ or similar embedded JSON.
    """
    for base_url in PWCC_HISTORY_URLS:
        try:
            time.sleep(2)
            resp = session.get(
                base_url,
                params={"q": query},
                headers=HEADERS,
                timeout=20,
            )
            if resp.status_code != 200 or len(resp.text) < 2000:
                continue
        except Exception:
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # Next.js data blob
        next_data = soup.find("script", id="__NEXT_DATA__")
        if next_data and next_data.string:
            try:
                data = json.loads(next_data.string)
                sales = _extract_from_json(data)
                if sales:
                    return sales
                # Deep search in props
                props = data.get("props", {}).get("pageProps", {})
                sales = _extract_from_json(props)
                if sales:
                    return sales
            except (json.JSONDecodeError, Exception):
                pass

        # Generic inline JSON arrays
        for script in soup.find_all("script"):
            text = script.string or ""
            if '"salePrice"' in text or '"sale_price"' in text or '"purchasePrice"' in text:
                m = re.search(r"(\[.*\])", text, re.DOTALL)
                if m:
                    try:
                        data = json.loads(m.group(1))
                        sales = _extract_from_json(data)
                        if sales:
                            return sales
                    except (json.JSONDecodeError, Exception):
                        pass

    return []


def scrape_pwcc(card) -> dict | None:
    """
    Scrape PWCC / Fanatics Collect sales history for a card.

    Returns dict with raw/graded price stats, or None if unavailable.
    Keys: avg, low, high, num_sales, graded_avg, graded_low, graded_high,
          graded_num_sales, last_sale_price, last_sale_date, last_sale_source,
          source = "pwcc"
    """
    query = _build_query(card)
    if not query.strip():
        return None

    session = requests.Session()
    session.headers.update(HEADERS)

    # Try API endpoints first (faster), then embedded JSON
    sales = _try_api_endpoints(session, query)
    if not sales:
        sales = _try_embedded_json(session, query)

    if not sales:
        return None

    raw_prices = []
    graded_prices = []
    last_sale = None

    for s in sales:
        title = s.get("title", "")
        price = s.get("price", 0)

        if LOT_RE.search(title):
            continue

        is_graded = bool(GRADED_RE.search(title)) or bool(s.get("grade"))
        if is_graded:
            graded_prices.append(price)
        else:
            raw_prices.append(price)

        # Track most recent sale
        if "date" in s:
            if last_sale is None or s["date"] > last_sale["date"]:
                last_sale = s

    raw_stats = _calc_stats(raw_prices)
    graded_stats = _calc_stats(graded_prices)

    if not raw_stats and not graded_stats:
        return None

    result: dict = {"source": "pwcc"}

    if raw_stats:
        result.update({
            "avg": raw_stats["avg"],
            "low": raw_stats["low"],
            "high": raw_stats["high"],
            "num_sales": raw_stats["count"],
        })

    if graded_stats:
        result.update({
            "graded_avg": graded_stats["avg"],
            "graded_low": graded_stats["low"],
            "graded_high": graded_stats["high"],
            "graded_num_sales": graded_stats["count"],
        })

    if last_sale:
        result["last_sale_price"] = last_sale["price"]
        result["last_sale_date"] = last_sale.get("date")
        result["last_sale_source"] = "pwcc"

    return result
