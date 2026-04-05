"""
eBay pricing — uses the eBay Browse API for active listing prices,
scrapes sold listings as a secondary source, and falls back to Claude AI estimate.
"""
import base64
import json
import os
import re
import statistics
import time
from urllib.parse import quote_plus

import anthropic
import requests
from bs4 import BeautifulSoup

# ── eBay API config ─────────────────────────────────────────────────────────

EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")
EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

_ebay_token_cache = {"token": None, "expires_at": 0}

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.ebay.com/",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

PRICING_PROMPT = """You are an expert sports card pricing analyst. Based on the following card details,
estimate the current market value for this card sold on eBay as a single ungraded card.

IMPORTANT: Be conservative. Most modern base cards (2020+) sell for $0.50-$3.00.
Common inserts sell for $1-5. Only star players, rookies, numbered parallels, and autos command higher prices.
Do NOT overestimate — a wrong high estimate is worse than a wrong low estimate for a seller.

Card details:
- Player: {player_name}
- Year: {year}
- Brand: {brand}
- Set: {set_name}
- Card Number: {card_number}
- Team: {team}
- Sport: {sport}
- Rookie Card: {is_rookie_card}
- Parallel: {parallel_name}
- Autograph: {is_autograph}
- Relic/Patch: {is_relic} ({relic_type})
- Numbered: {serial_number}
- Condition: {condition}

Return ONLY a valid JSON object with your price estimates (no markdown, no explanation):
{{
  "avg": <estimated average sold price as a number>,
  "low": <estimated low end of recent sales>,
  "high": <estimated high end of recent sales>,
  "num_sales": <estimated number of this card sold per month on eBay>,
  "confidence": "high" or "medium" or "low",
  "reasoning": "1-2 sentence explanation of pricing"
}}"""


def build_search_query(card) -> str:
    """Build a targeted eBay search string from card metadata."""
    parts = []
    if card.year:
        parts.append(str(card.year))
    # Skip brand if set_name already includes it (e.g. "Topps Chrome" already has "Topps")
    if card.brand and card.set_name and card.set_name.lower().startswith(card.brand.lower()):
        parts.append(card.set_name)
    else:
        if card.brand:
            parts.append(card.brand)
        if card.set_name:
            parts.append(card.set_name)
    if card.player_name:
        parts.append(card.player_name)
    if getattr(card, 'insert_set', None):
        parts.append(card.insert_set)
    if card.parallel_name:
        parts.append(card.parallel_name)
    if card.is_autograph:
        parts.append("auto")
    if card.is_rookie_card:
        parts.append("RC")
    # Only add print run if it's not already in the parallel name
    if card.is_numbered and card.print_run:
        parallel = card.parallel_name or ""
        if f"/{card.print_run}" not in parallel:
            parts.append(f"/{card.print_run}")
    if card.card_number:
        parts.append(f"#{card.card_number}")
    return " ".join(parts)


def build_search_url(query: str) -> str:
    """Build the eBay sold listings search URL for manual verification."""
    return (
        f"https://www.ebay.com/sch/i.html"
        f"?_nkw={quote_plus(query)}"
        f"&LH_Sold=1&LH_Complete=1&_sop=13"
    )


# ── eBay Browse API ─────────────────────────────────────────────────────────

def _get_ebay_token() -> str | None:
    """Get an OAuth2 token using client credentials. Caches until expiry."""
    if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        return None

    now = time.time()
    if _ebay_token_cache["token"] and now < _ebay_token_cache["expires_at"] - 60:
        return _ebay_token_cache["token"]

    credentials = base64.b64encode(
        f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()
    ).decode()

    try:
        resp = requests.post(
            EBAY_TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {credentials}",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        _ebay_token_cache["token"] = data["access_token"]
        _ebay_token_cache["expires_at"] = now + data.get("expires_in", 7200)
        return data["access_token"]
    except Exception:
        return None


GRADED_KEYWORDS = {"psa", "bgs", "sgc", "cgc", "graded", "slab", "slabbed", "gem mint", "beckett"}
PARALLEL_KEYWORDS = {
    "refractor", "prizm", "holo", "gold", "silver", "blue", "red", "green",
    "orange", "purple", "pink", "black", "white", "mojo", "shimmer", "speckle",
    "scope", "disco", "wave", "camo", "tiger", "snakeskin", "lava", "ice",
    "chrome", "foil", "rainbow", "optic", "hyper", "neon",
}


def _classify_listing(title: str, card) -> str:
    """Classify a listing as 'raw', 'graded', or 'skip'."""
    title_lower = title.lower()

    # Skip lot listings
    if re.search(r'\b(lot|bundle|set of|x\d+|\d+x)\b', title_lower):
        return "skip"

    # If our card is NOT a parallel, skip listings that mention parallel types
    if not card.is_parallel:
        for kw in PARALLEL_KEYWORDS:
            set_name = (card.set_name or "").lower()
            if kw in title_lower and kw not in set_name:
                return "skip"

    # Check if graded
    for kw in GRADED_KEYWORDS:
        if kw in title_lower:
            return "graded"

    return "raw"


def _calc_stats(prices: list[float]) -> dict:
    """Calculate price statistics from a list of prices."""
    if not prices:
        return {}
    # Remove outliers: drop prices more than 3x the median
    if len(prices) >= 5:
        med = statistics.median(prices)
        prices = [p for p in prices if p <= med * 3]
    if not prices:
        return {}
    return {
        "avg": round(statistics.mean(prices), 2),
        "median": round(statistics.median(prices), 2),
        "low": round(min(prices), 2),
        "high": round(max(prices), 2),
        "count": len(prices),
    }


def _get_ebay_api_pricing(card) -> dict | None:
    """Use the eBay Browse API to get active listing prices."""
    token = _get_ebay_token()
    if not token:
        return None

    query = build_search_query(card)
    if not query.strip():
        return None

    try:
        resp = requests.get(
            EBAY_BROWSE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            },
            params={
                "q": query,
                "limit": 50,
                "sort": "newlyListed",
                "filter": "buyingOptions:{FIXED_PRICE}",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    items = data.get("itemSummaries", [])
    if not items:
        return None

    raw_prices = []
    graded_prices = []
    for item in items:
        title = item.get("title", "")
        classification = _classify_listing(title, card)
        if classification == "skip":
            continue

        price_info = item.get("price", {})
        try:
            val = float(price_info.get("value", 0))
            currency = price_info.get("currency", "USD")
            if currency == "USD" and 0.25 < val < 50_000:
                if classification == "graded":
                    graded_prices.append(val)
                else:
                    raw_prices.append(val)
        except (ValueError, TypeError):
            continue

    raw_stats = _calc_stats(raw_prices)
    if not raw_stats:
        return None

    result = {
        "avg": raw_stats["avg"],
        "median": raw_stats["median"],
        "low": raw_stats["low"],
        "high": raw_stats["high"],
        "num_sales": raw_stats["count"],
        "search_query": query,
        "search_url": build_search_url(query),
        "source": "ebay_api",
    }

    graded_stats = _calc_stats(graded_prices)
    if graded_stats:
        result["graded_avg"] = graded_stats["avg"]
        result["graded_low"] = graded_stats["low"]
        result["graded_high"] = graded_stats["high"]
        result["graded_num_sales"] = graded_stats["count"]

    return result


# ── eBay Scraping (sold listings) ──────────────────────────────────────────

def _extract_prices(soup: BeautifulSoup, card=None) -> dict:
    """Parse sold prices out of an eBay search results page. Returns {'raw': [], 'graded': []}."""
    raw_prices = []
    graded_prices = []
    items = soup.select(".s-item")

    for item in items:
        title_el = item.select_one(".s-item__title")
        if not title_el:
            continue
        title_text = title_el.get_text(strip=True)
        if title_text == "Shop on eBay" or title_text == "":
            continue

        # Classify listing
        if card:
            classification = _classify_listing(title_text, card)
            if classification == "skip":
                continue
        else:
            classification = "raw"

        # Skip items with "to" price ranges
        price_el = item.select_one(".s-item__price")
        if not price_el:
            continue
        text = price_el.get_text(strip=True)
        if " to " in text.lower():
            continue

        match = re.search(r"\$([0-9,]+\.?[0-9]*)", text)
        if match:
            try:
                price = float(match.group(1).replace(",", ""))
                if 0.25 < price < 50_000:
                    if classification == "graded":
                        graded_prices.append(price)
                    else:
                        raw_prices.append(price)
            except ValueError:
                pass
    return {"raw": raw_prices, "graded": graded_prices}


def _get_ebay_scrape_pricing(card, max_results: int = 25) -> dict | None:
    """Try to scrape eBay sold listings. Returns None on failure."""
    query = build_search_query(card)
    if not query.strip():
        return None

    url = build_search_url(query)

    session = requests.Session()
    session.headers.update(SCRAPE_HEADERS)

    try:
        time.sleep(1)
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    if len(resp.text) < 5000:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    if soup.select_one("#captcha") or "please verify" in resp.text.lower():
        return None

    price_data = _extract_prices(soup, card)
    raw_prices = price_data["raw"][:max_results]
    graded_prices = price_data["graded"][:max_results]

    raw_stats = _calc_stats(raw_prices)
    if not raw_stats:
        return None

    result = {
        "avg": raw_stats["avg"],
        "median": raw_stats["median"],
        "low": raw_stats["low"],
        "high": raw_stats["high"],
        "num_sales": raw_stats["count"],
        "search_query": query,
        "search_url": url,
        "source": "ebay_sold",
    }

    graded_stats = _calc_stats(graded_prices)
    if graded_stats:
        result["graded_avg"] = graded_stats["avg"]
        result["graded_low"] = graded_stats["low"]
        result["graded_high"] = graded_stats["high"]
        result["graded_num_sales"] = graded_stats["count"]

    return result


# ── AI Estimate (fallback) ─────────────────────────────────────────────────

def _get_ai_pricing(card) -> dict | None:
    """Use Claude to estimate card value based on market knowledge."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    client = anthropic.Anthropic(api_key=api_key)

    prompt = PRICING_PROMPT.format(
        player_name=card.player_name or "Unknown",
        year=card.year or "Unknown",
        brand=card.brand or "Unknown",
        set_name=card.set_name or "Unknown",
        card_number=card.card_number or "Unknown",
        team=card.team or "Unknown",
        sport=card.sport or "Unknown",
        is_rookie_card=card.is_rookie_card,
        parallel_name=card.parallel_name or "Base",
        is_autograph=card.is_autograph,
        is_relic=card.is_relic,
        relic_type=card.relic_type or "N/A",
        serial_number=card.serial_number or ("/" + str(card.print_run) if card.print_run else "Not numbered"),
        condition=card.condition or "Unknown",
    )

    query = build_search_query(card)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
            text = "\n".join(inner).strip()
        result = json.loads(text)
        result["search_query"] = query
        result["search_url"] = build_search_url(query)
        result["source"] = "ai_estimate"
        return result
    except Exception:
        return None


# ── Main entry point ────────────────────────────────────────────────────────

def get_ebay_pricing(card) -> dict | None:
    """
    Get pricing for a card. Tries in order:
    1. eBay scraping (sold listings — real sale prices)
    2. eBay Browse API (active listings — what's currently listed)
    3. Claude AI estimate (fallback)
    """
    # Try scraping sold listings first (real sale prices)
    result = _get_ebay_scrape_pricing(card)
    if result:
        return result

    # Fall back to eBay API (active listings, clearly labeled)
    result = _get_ebay_api_pricing(card)
    if result:
        return result

    # Last resort: AI estimate
    return _get_ai_pricing(card)
