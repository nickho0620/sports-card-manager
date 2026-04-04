"""
eBay pricing — uses Claude to estimate card value based on market knowledge,
with eBay scraping as a secondary data source when available.
"""
import json
import os
import re
import statistics
import time
from urllib.parse import quote_plus

import anthropic
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

PRICING_PROMPT = """You are an expert sports card pricing analyst. Based on the following card details,
estimate the current market value for this card sold on eBay as a single ungraded card.

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
    if card.is_numbered and card.print_run:
        parts.append(f"/{card.print_run}")
    if card.card_number:
        parts.append(f"#{card.card_number}")
    return " ".join(parts)


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
        result["search_query"] = build_search_query(card)
        result["source"] = "ai_estimate"
        return result
    except Exception:
        return None


def _extract_prices(soup: BeautifulSoup) -> list[float]:
    """Parse sold prices out of an eBay search results page."""
    prices = []
    price_selectors = [".s-item__price", ".srp-item__price", "[data-testid='item-price']"]
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
        match = re.search(r"\$([0-9,]+\.?[0-9]*)", text)
        if match:
            try:
                price = float(match.group(1).replace(",", ""))
                if 0.25 < price < 50_000:
                    prices.append(price)
            except ValueError:
                pass
    return prices


def _get_ebay_pricing(card, max_results: int = 25) -> dict | None:
    """Try to scrape eBay sold listings. Returns None on failure."""
    query = build_search_query(card)
    if not query.strip():
        return None

    url = (
        f"https://www.ebay.com/sch/i.html"
        f"?_nkw={quote_plus(query)}"
        f"&LH_Sold=1&LH_Complete=1&_sop=13"
    )

    try:
        time.sleep(1)
        resp = requests.get(url, headers=HEADERS, timeout=20)
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
        "source": "ebay_sold",
    }


def get_ebay_pricing(card) -> dict | None:
    """
    Get pricing for a card. Tries eBay scraping first, falls back to Claude AI estimate.
    """
    # Try eBay scraping first
    result = _get_ebay_pricing(card)
    if result:
        return result

    # Fall back to AI-based pricing
    return _get_ai_pricing(card)
