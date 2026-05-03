"""
Heritage Auctions archive scraper.

Scrapes realized prices from Heritage's sports cards auction archive at
sports.ha.com. Heritage handles mid-to-high-end cards and provides years
of historical depth. Server-rendered HTML (no JS needed).

Covers both raw and graded sales — classifies by listing title.
"""

import re
import statistics
import time
from datetime import datetime
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

HERITAGE_ARCHIVE_URL = "https://sports.ha.com/c/search/results.zx"
HERITAGE_BASE = "https://sports.ha.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://sports.ha.com/",
    "Connection": "keep-alive",
}

# Grading company keywords for classification
GRADED_RE = re.compile(
    r"\b(psa|bgs|sgc|cgc|beckett|graded|slab|slabbed|gem\s*mint)\b",
    re.IGNORECASE,
)

# Lot / bundle patterns to skip
LOT_RE = re.compile(
    r"\b(lot|bundle|collection|set\s+of|\d+\s*cards?)\b",
    re.IGNORECASE,
)


def _build_heritage_query(card) -> str:
    parts = []
    if card.year:
        parts.append(str(card.year))
    if card.brand:
        parts.append(card.brand)
    if card.player_name:
        parts.append(card.player_name)
    if card.parallel_name:
        parts.append(card.parallel_name)
    if card.is_autograph:
        parts.append("autograph")
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


def _parse_price(text: str) -> float | None:
    m = re.search(r"\$([0-9,]+\.?[0-9]*)", text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def _parse_date(text: str) -> datetime | None:
    text = text.strip()
    # Heritage uses formats like "March 12, 2024" or "3/12/2024"
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # Try partial: "Mar 2024" or "2024"
    m = re.search(r"(\d{4})", text)
    if m:
        try:
            return datetime(int(m.group(1)), 1, 1)
        except ValueError:
            pass
    return None


def _classify(title: str) -> str:
    """Return 'raw', 'graded', or 'skip'."""
    if LOT_RE.search(title):
        return "skip"
    if GRADED_RE.search(title):
        return "graded"
    return "raw"


def _parse_results_page(soup: BeautifulSoup) -> list[dict]:
    """
    Parse Heritage archive results page.
    Returns list of {title, price, classification, date} dicts.
    """
    lots = []

    # Heritage uses several possible container patterns
    # Try: .lot-result, .lot-item, article.lot, li.result-item, .srp-lot
    containers = (
        soup.select(".lot-result")
        or soup.select(".lot-item")
        or soup.select("article.lot")
        or soup.select("li.result-item")
        or soup.select(".srp-lot")
        or soup.select(".search-result")
    )

    if not containers:
        # Fallback: look for any element with a realized price
        containers = soup.find_all(
            lambda tag: tag.name in ("li", "div", "article")
            and re.search(r"Realized|Sold|Price", tag.get_text())
            and _parse_price(tag.get_text()) is not None
        )

    for item in containers:
        text = item.get_text(" ", strip=True)

        # Get title
        title_el = (
            item.select_one(".lot-title")
            or item.select_one("h3")
            or item.select_one("h4")
            or item.select_one(".title")
            or item.select_one("a")
        )
        title = title_el.get_text(strip=True) if title_el else text[:120]

        classification = _classify(title)
        if classification == "skip":
            continue

        # Get realized price — Heritage shows "Realized: $XXX" or similar
        price_text = text
        price_el = (
            item.select_one(".realized-price")
            or item.select_one(".lot-price")
            or item.select_one(".price")
        )
        if price_el:
            price_text = price_el.get_text(strip=True)

        price = _parse_price(price_text)
        if not price or price < 1.00 or price > 10_000_000:
            continue

        lot = {"title": title, "price": price, "classification": classification}

        # Try to extract sale date
        date_el = item.select_one(".lot-date") or item.select_one(".date")
        if date_el:
            d = _parse_date(date_el.get_text(strip=True))
            if d:
                lot["date"] = d
        else:
            # Try to find year in text
            d = _parse_date(text)
            if d:
                lot["date"] = d

        lots.append(lot)

    return lots


def scrape_heritage(card) -> dict | None:
    """
    Scrape Heritage Auctions archive for a card.

    Returns dict with raw and/or graded price stats, or None on failure.
    Keys: avg, low, high, num_sales, graded_avg, graded_low, graded_high,
          graded_num_sales, last_sale_price, last_sale_date, last_sale_source,
          source = "heritage"
    """
    query = _build_heritage_query(card)
    if not query.strip():
        return None

    session = requests.Session()
    session.headers.update(HEADERS)

    params = {
        "term": query,
        "archive_state": "5327",   # archived/completed
        "dept": "3923",            # sports cards
        "sold_status": "1526",     # sold items
        "mode": "archive",
        "sb": "14",                # sort by date
        "page": "1~25",            # page 1, 25 items
    }

    try:
        time.sleep(2)
        resp = session.get(HERITAGE_ARCHIVE_URL, params=params, timeout=25)
        resp.raise_for_status()
    except Exception:
        return None

    if len(resp.text) < 3000:
        return None

    if "captcha" in resp.text.lower() or "cf-browser-verification" in resp.text.lower():
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    lots = _parse_results_page(soup)

    if not lots:
        return None

    raw_prices = [l["price"] for l in lots if l["classification"] == "raw"]
    graded_prices = [l["price"] for l in lots if l["classification"] == "graded"]

    raw_stats = _calc_stats(raw_prices)
    graded_stats = _calc_stats(graded_prices)

    # Need at least one set of stats
    if not raw_stats and not graded_stats:
        return None

    result: dict = {"source": "heritage", "source_url": resp.url}

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

    # Most recent sale across all lots
    dated = [l for l in lots if "date" in l]
    if dated:
        latest = max(dated, key=lambda l: l["date"])
        result["last_sale_price"] = latest["price"]
        result["last_sale_date"] = latest["date"]
        result["last_sale_source"] = "heritage"

    return result
