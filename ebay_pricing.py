"""
eBay pricing — scrapes completed/sold listings to estimate card value.
No API key required.
"""
import re
import statistics
import time
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def build_search_query(card) -> str:
    """Build a targeted eBay search string from card metadata."""
    parts = []
    if card.year:
        parts.append(str(card.year))
    if card.brand:
        parts.append(card.brand)
    if card.set_name:
        parts.append(card.set_name)
    if card.player_name:
        parts.append(card.player_name)
    if card.parallel_name:
        parts.append(card.parallel_name)
    if card.is_autograph:
        parts.append("auto")
    if card.is_rookie_card:
        parts.append("RC")
    if card.is_numbered and card.print_run:
        parts.append(f"/{card.print_run}")
    if card.card_number:
        parts.append(f"#{card.card_number}")
    return " ".join(parts)


def _extract_prices(soup: BeautifulSoup) -> list[float]:
    """Parse sold prices out of an eBay search results page."""
    prices = []
    # eBay uses several price element patterns — try all
    price_selectors = [
        ".s-item__price",
        ".srp-item__price",
        "[data-testid='item-price']",
    ]
    items = soup.select(".s-item, .srp-results .srp-item")
    for item in items:
        price_el = None
        for sel in price_selectors:
            price_el = item.select_one(sel)
            if price_el:
                break
        if not price_el:
            continue
        text = price_el.get_text(strip=True)
        # Handle ranges like "$5.00 to $10.00" — take the lower
        match = re.search(r"\$([0-9,]+\.?[0-9]*)", text)
        if match:
            try:
                price = float(match.group(1).replace(",", ""))
                # Filter out obvious junk (shipping-only listings, $0, absurdly high)
                if 0.25 < price < 50_000:
                    prices.append(price)
            except ValueError:
                pass
    return prices


def get_ebay_pricing(card, max_results: int = 25) -> dict | None:
    """
    Fetch completed/sold eBay listings for the card.
    Returns a dict with avg, low, high, num_sales, search_query.
    Returns None if no results found or on network error.
    """
    query = build_search_query(card)
    if not query.strip():
        return None

    url = (
        f"https://www.ebay.com/sch/i.html"
        f"?_nkw={quote_plus(query)}"
        f"&LH_Sold=1&LH_Complete=1&_sop=13"   # sort by most recent
    )

    try:
        time.sleep(1)  # be polite
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    prices = _extract_prices(soup)[:max_results]

    if not prices:
        return None

    return {
        "avg": round(statistics.mean(prices), 2),
        "median": round(statistics.median(prices), 2),
        "low": round(min(prices), 2),
        "high": round(max(prices), 2),
        "num_sales": len(prices),
        "search_query": query,
    }
