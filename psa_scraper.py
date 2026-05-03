"""
PSA Auction Prices Realized (APR) scraper.

Scrapes graded card sale data from psacard.com/auctionprices — covers
eBay, Heritage, Goldin, PWCC/Fanatics, REA, and other auction houses
all in one place. Multi-year history. Graded cards only (PSA grades).

Two-step process:
  1. Search PSA APR for the card → get first matching result URL
  2. Scrape that card's price table → parse grade + price per sale
"""

import re
import statistics
import time
from datetime import datetime
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

PSA_BASE = "https://www.psacard.com"
PSA_SEARCH_URL = "https://www.psacard.com/auctionprices/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.psacard.com/",
    "Connection": "keep-alive",
}


def _build_psa_query(card) -> str:
    """Build a focused search query for PSA APR."""
    parts = []
    if card.year:
        parts.append(str(card.year))
    if card.brand:
        parts.append(card.brand)
    if card.player_name:
        parts.append(card.player_name)
    # Card number helps narrow results but can hurt search recall if wrong
    if card.card_number:
        parts.append(f"#{card.card_number}")
    return " ".join(parts)


def _calc_stats(prices: list[float]) -> dict:
    """Compute avg/low/high/count, dropping extreme outliers."""
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
    """Extract a dollar amount from a string."""
    m = re.search(r"\$([0-9,]+\.?[0-9]*)", text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def _parse_date(text: str) -> datetime | None:
    """Try several date formats PSA uses."""
    text = text.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _get_card_url(session: requests.Session, query: str) -> str | None:
    """
    Search PSA APR and return the URL of the first matching card's price page.
    """
    try:
        time.sleep(2)
        resp = session.get(
            PSA_SEARCH_URL,
            params={"q": query},
            timeout=20,
        )
        resp.raise_for_status()
    except Exception:
        return None

    if len(resp.text) < 2000:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Search result links go to /auctionprices/.../values/... pages
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/auctionprices/" in href and "/values/" in href:
            return href if href.startswith("http") else PSA_BASE + href

    # Fallback: any link with /auctionprices/ that isn't the search page itself
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/auctionprices/" in href and "search" not in href and href != "/auctionprices/":
            return href if href.startswith("http") else PSA_BASE + href

    return None


def _parse_price_table(soup: BeautifulSoup) -> list[dict]:
    """
    Parse the sales table on a PSA APR card page.
    Returns list of {grade, price, date, auction_house} dicts.
    """
    sales = []

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not headers:
            continue

        # Need at least a price column
        has_price = any("price" in h or "sale" in h or "amount" in h for h in headers)
        if not has_price:
            continue

        # Map column index to field name
        col_map = {}
        for i, h in enumerate(headers):
            if "date" in h:
                col_map["date"] = i
            elif "grade" in h:
                col_map["grade"] = i
            elif "price" in h or "sale" in h or "amount" in h:
                col_map["price"] = i
            elif "auction" in h or "house" in h or "seller" in h:
                col_map["auction"] = i

        if "price" not in col_map:
            continue

        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if not cells:
                continue

            sale = {}

            if "price" in col_map and col_map["price"] < len(cells):
                val = _parse_price(cells[col_map["price"]].get_text(strip=True))
                if val:
                    sale["price"] = val

            if "grade" in col_map and col_map["grade"] < len(cells):
                grade_text = cells[col_map["grade"]].get_text(strip=True)
                m = re.search(r"PSA\s*(\d+\.?\d*)", grade_text, re.IGNORECASE)
                if m:
                    sale["grade"] = f"PSA {m.group(1)}"
                elif grade_text:
                    sale["grade"] = grade_text

            if "date" in col_map and col_map["date"] < len(cells):
                d = _parse_date(cells[col_map["date"]].get_text(strip=True))
                if d:
                    sale["date"] = d

            if "auction" in col_map and col_map["auction"] < len(cells):
                sale["auction_house"] = cells[col_map["auction"]].get_text(strip=True)

            if "price" in sale and sale["price"] > 0.50:
                sales.append(sale)

    # Fallback: scan all cells for price + grade if table parsing found nothing
    if not sales:
        all_cells = soup.find_all("td")
        grade_pattern = re.compile(r"PSA\s*(\d+\.?\d*)", re.IGNORECASE)
        price_vals = []
        grade_vals = []
        for cell in all_cells:
            text = cell.get_text(strip=True)
            p = _parse_price(text)
            if p and 0.50 < p < 500_000:
                price_vals.append(p)
            gm = grade_pattern.search(text)
            if gm:
                grade_vals.append(f"PSA {gm.group(1)}")
        # Pair them up positionally (best-effort)
        for i, p in enumerate(price_vals):
            sale = {"price": p}
            if i < len(grade_vals):
                sale["grade"] = grade_vals[i]
            sales.append(sale)

    return sales


def scrape_psa_apr(card) -> dict | None:
    """
    Scrape PSA Auction Prices Realized for a card.

    Returns a dict with:
      graded_avg, graded_low, graded_high, graded_num_sales
      psa10_avg, psa10_low, psa10_high  (if PSA 10 sales found)
      grade_breakdown  {grade: {avg, low, high, count}}
      last_sale_price, last_sale_date, last_sale_source
      source = "psa_apr"
    Returns None if scraping fails or no results found.
    """
    query = _build_psa_query(card)
    if not query.strip():
        return None

    session = requests.Session()
    session.headers.update(HEADERS)

    # Step 1: find the card's price page
    card_url = _get_card_url(session, query)
    if not card_url:
        return None

    # Step 2: scrape the price table
    try:
        time.sleep(2)
        resp = session.get(card_url, timeout=20)
        resp.raise_for_status()
    except Exception:
        return None

    if len(resp.text) < 2000:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    if "captcha" in resp.text.lower() or "verify" in resp.text.lower()[:500]:
        return None

    sales = _parse_price_table(soup)
    if not sales:
        return None

    # Group by grade
    by_grade: dict[str, list[float]] = {}
    for s in sales:
        grade = s.get("grade", "graded")
        by_grade.setdefault(grade, []).append(s["price"])

    all_prices = [s["price"] for s in sales if 0.50 < s["price"] < 500_000]
    graded_stats = _calc_stats(all_prices)
    if not graded_stats:
        return None

    result: dict = {
        "graded_avg": graded_stats["avg"],
        "graded_low": graded_stats["low"],
        "graded_high": graded_stats["high"],
        "graded_num_sales": graded_stats["count"],
        "grade_breakdown": {
            g: _calc_stats(prices)
            for g, prices in by_grade.items()
            if _calc_stats(prices)
        },
        "source": "psa_apr",
        "source_url": card_url,
    }

    # PSA 10 specific
    psa10_prices = by_grade.get("PSA 10") or by_grade.get("PSA10") or []
    if psa10_prices:
        s10 = _calc_stats(psa10_prices)
        result["psa10_avg"] = s10["avg"]
        result["psa10_low"] = s10["low"]
        result["psa10_high"] = s10["high"]
        result["psa10_count"] = s10["count"]

    # Most recent sale
    dated_sales = [s for s in sales if "date" in s]
    if dated_sales:
        latest = max(dated_sales, key=lambda s: s["date"])
        result["last_sale_price"] = latest["price"]
        result["last_sale_date"] = latest["date"]
        result["last_sale_source"] = "psa_apr"

    return result
