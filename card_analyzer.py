"""
Card analyzer — uses Claude Vision (Anthropic) to extract all metadata from
front + back card images. Includes a web verification step that searches eBay
for the card number to find known parallels, then re-examines the images.
"""
import base64
import io
import json
import os
import re
import time
from urllib.parse import quote_plus

import anthropic
import requests
from bs4 import BeautifulSoup
from PIL import Image

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

ANALYSIS_PROMPT = """You are an expert sports card grader, identifier, and cataloger with encyclopedic knowledge of all card sets, inserts, parallels, and product codes.

CRITICAL STEP — READ ALL CODES AND FINE PRINT ON THE BACK FIRST:
Look at the BACK of the card carefully. Read ALL text, especially the smallest print at the bottom. You are looking for:
1. PRODUCT CODE — a code like "CMP097855", "CODE#CMP097855", or similar alphanumeric string. This is a manufacturer catalog code that uniquely identifies the product/set/release. Transcribe it EXACTLY.
2. SET/INSERT CODE — a short code before the card number (e.g. "T88-10" means the "T88" 1988 Topps insert, card #10; "BCP-42" = Bowman Chrome Prospects #42; "PSC" = Prizm Silver Chrome). This identifies the specific insert or subset.
3. COPYRIGHT YEAR — text like "© 2024 Topps" or "© 2025 Panini" — this is the ACTUAL release year. Do NOT guess the year from player stats.
4. SET NAME — often printed in a header, footer, or alongside the card number (e.g. "TOPPS CHROME", "DONRUSS RATED ROOKIE", "PRIZM", "1990 TOPPS CHROME SILVER PACK").
5. CARD NUMBER — e.g. "#123", "BCP-42", "T88-10"
Read EVERY piece of fine print on the back before making your identification.

Also examine:
- The FRONT for: card design style, border color/pattern, foil stamps, holographic effects, refractor rainbow patterns, numbered stamps (like /25, /50, /99), autograph stickers or on-card autos, jersey/patch swatches, RC logo, the uniform the player is wearing.
- The BACK for: the card number (e.g. "#123" or "BCP-42"), the set name in the header or footer, copyright year, product code, any "PARALLEL" or "INSERT" text, print run stamps.

Use ALL of this information to identify the EXACT card. Do not guess the year from the player's stats — use the copyright year on the back.

Return ONLY a valid JSON object — no markdown, no explanation:
{
  "player_name": "Full player name, or null",
  "year": <4-digit integer from the copyright year on the back, or null>,
  "brand": "Topps / Panini / Upper Deck / Bowman / Donruss / Fleer / Score / Leaf / etc., or null",
  "set_name": "The specific set name (e.g. 'Topps Chrome', 'Prizm', 'Stadium Club', 'Bowman 1st', 'Donruss Rated Rookie', 'Select', 'Mosaic') — NOT just the brand, or null",
  "subset": "e.g. All-Star, Draft Picks, Rookie Debut, or null",
  "insert_set": "If this is an insert card, the insert set name (e.g. '1989 Topps', 'Silver Pack Mojo', 'Finest Flashbacks', '1990 Topps Chrome', 'Wander Franco Generation Now') — use the product code to identify this, or null",
  "card_number": "Card number/ID exactly as printed (e.g. '123', 'BCP-42', 'T88-10', 'MLMAR-CB'). Include the full alphanumeric code — the prefix often identifies the insert set (e.g. MLMAR = Major League Marquee, T88 = 1988 Topps insert), or null",
  "team": "team name, or null",
  "sport": "Baseball / Basketball / Football / Hockey / Soccer / Other",
  "product_code": "The manufacturer/catalog code from the back (e.g. 'CMP097855', 'CODE#CMP097855') — transcribe EXACTLY as printed, or null",
  "is_rookie_card": true or false,
  "is_parallel": true or false,
  "parallel_name": "Be specific — e.g. 'Gold /2024', 'Rainbow Foil', 'Prizm Silver', 'Refractor', 'Holo', 'Scope', 'Speckle', 'Disco', 'Mojo', 'Green Shimmer', 'Red /199', 'Blue /150', 'Purple /75', or null. LOOK for visual cues: rainbow sheen = refractor, colored border = color parallel, sparkle = foil/shimmer",
  "is_foil": true or false,
  "is_autograph": true or false,
  "is_relic": true or false,
  "relic_type": "Jersey / Patch / Bat / Ball / Glove / etc., or null",
  "is_numbered": true or false,
  "print_run": <integer — the total from the stamp e.g. 25 for /25, or null>,
  "serial_number": "the stamped number exactly as shown e.g. '15/25', or null",
  "has_alternate_jersey": true or false,
  "jersey_description": "e.g. City Connect, All-Star, Throwback, Spring Training, Players Weekend, or null",
  "is_short_print": true or false,
  "condition": "Mint / Near Mint / Excellent / Very Good / Good / Poor  (visual estimate — look at centering, corners, edges, surface)",
  "notable_features": "Any other notable features as a plain string, or null",
  "description": "1-2 sentence summary including the exact set identification (e.g. '2024 Topps Series 1 1989 Topps Silver Pack Chrome insert of Juan Soto')"
}

IMPORTANT: If you are uncertain about ANY field, set it to null rather than guessing.

CRITICAL: If the image is NOT a sports card (e.g. it's a random photo, a non-sports item, a meme, a document, etc.), return ONLY this JSON:
{"error": "not_a_sports_card", "description": "This does not appear to be a sports card."}
Only analyze images that are clearly sports trading cards (baseball, basketball, football, hockey, soccer, etc.)."""

VERIFY_PROMPT = """You previously analyzed this card and produced the initial identification below.
I then searched eBay for this card and found the following listing titles from sold/active listings.
These titles show the REAL variants, parallels, and details that collectors and sellers use.

YOUR INITIAL ANALYSIS:
{initial_analysis}

EBAY LISTING TITLES FOR THIS CARD:
{listing_titles}

Now re-examine the card images carefully. Compare the visual characteristics of THIS card
(border color, refractor/rainbow pattern, foil type, numbering stamp, any color tint)
against the variants mentioned in the eBay listings above.

Key things to verify or correct:
1. PARALLEL — Is this a specific color parallel (Orange, Blue, Green, Red, Gold, etc.)? Is it a Refractor, Mojo, Prizm, Holo, Scope, Speckle, Shimmer? Match the visual cues to the parallels seen in listings.
2. NUMBERED — Do you see a stamp like /25, /50, /75, /99, /150, /199, /250, /299, /2024 anywhere on the card? Listings mentioning "/#" confirm numbered variants exist.
3. INSERT SET — Is this from a specific insert set? The listings may clarify the exact insert name.
4. SET NAME — Confirm the exact set name based on what sellers call it in listings.
5. YEAR — Confirm from copyright text on back, not from player stats.

Return ONLY an updated valid JSON object with ALL the same fields as before — include every field,
not just the ones you changed. No markdown, no explanation."""


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _search_ebay_listings(query: str, max_titles: int = 30) -> list[str]:
    """Search eBay for a card and return listing titles (sold + active)."""
    titles = []

    for search_type in ["sold", "active"]:
        url = (
            f"https://www.ebay.com/sch/i.html"
            f"?_nkw={quote_plus(query)}"
            f"&_sop=13"
        )
        if search_type == "sold":
            url += "&LH_Sold=1&LH_Complete=1"

        try:
            time.sleep(0.5)
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            for item in soup.select(".s-item__title, .srp-item__title"):
                text = item.get_text(strip=True)
                if text and text != "Shop on eBay":
                    titles.append(text)
        except Exception:
            continue

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for t in titles:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique[:max_titles]


def _build_verification_query(analysis: dict) -> str:
    """Build a search query from the initial analysis to find this card on eBay."""
    parts = []
    if analysis.get("year"):
        parts.append(str(analysis["year"]))
    brand = analysis.get("brand", "")
    set_name = analysis.get("set_name", "")
    if brand and set_name and set_name.lower().startswith(brand.lower()):
        parts.append(set_name)
    else:
        if brand:
            parts.append(brand)
        if set_name:
            parts.append(set_name)
    if analysis.get("player_name"):
        parts.append(analysis["player_name"])
    if analysis.get("card_number"):
        parts.append(f"#{analysis['card_number']}")
    return " ".join(parts)


def _load_image_b64(path: str) -> tuple[str, str]:
    """Load an image, resize if huge, return (base64_data, media_type)."""
    with Image.open(path) as img:
        img = img.convert("RGB")
        max_dim = 2000
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        data = base64.standard_b64encode(buf.getvalue()).decode()
    return data, "image/jpeg"


def _clean_json(text: str) -> str:
    """Strip markdown code fences if the model wraps the JSON."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner).strip()
    return text


def _make_image_content(path_or_url: str) -> dict:
    """Build a Claude image content block from a local path or URL."""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return {
            "type": "image",
            "source": {"type": "url", "url": path_or_url},
        }
    else:
        b64, media_type = _load_image_b64(path_or_url)
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        }


def _call_claude(client, model: str, max_tokens: int, content: list, retries: int = 3) -> dict:
    """Make a Claude API call with retries, return parsed JSON."""
    for attempt in range(retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": content}],
            )
            raw = _clean_json(response.content[0].text)
            return json.loads(raw)
        except json.JSONDecodeError:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise ValueError(f"Claude returned invalid JSON after {retries} attempts: {response.content[0].text[:500]}")
        except anthropic.RateLimitError:
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def _verify_with_ebay(client, front_content: dict, back_content: dict,
                      initial: dict) -> dict | None:
    """Search eBay for the card, then ask Claude to re-examine with context."""
    query = _build_verification_query(initial)
    if not query.strip():
        return None

    titles = _search_ebay_listings(query)
    if not titles:
        # Try a simpler query with just player + card number
        parts = []
        if initial.get("player_name"):
            parts.append(initial["player_name"])
        if initial.get("card_number"):
            parts.append(f"#{initial['card_number']}")
        if parts:
            titles = _search_ebay_listings(" ".join(parts))
    if not titles:
        return None

    titles_text = "\n".join(f"- {t}" for t in titles)
    initial_text = json.dumps(initial, indent=2)

    prompt = VERIFY_PROMPT.format(
        initial_analysis=initial_text,
        listing_titles=titles_text,
    )

    try:
        verified = _call_claude(
            client, CLAUDE_MODEL, 1024,
            [
                {"type": "text", "text": prompt},
                front_content,
                back_content,
            ],
        )
        return verified
    except Exception:
        return None


COMBINED_ANALYSIS_PROMPT = """You are an expert sports card grader, identifier, and cataloger with encyclopedic knowledge of all card sets, inserts, parallels, and product codes.

This single image contains BOTH the front and back of a sports card that was scanned on a flatbed scanner. The two sides are laid out together — typically side by side or top and bottom. First, identify which portion is the FRONT of the card and which is the BACK.

Then follow these instructions:

CRITICAL STEP — READ ALL CODES AND FINE PRINT ON THE BACK FIRST:
Look at the BACK portion carefully. Read ALL text, especially the smallest print at the bottom. You are looking for:
1. PRODUCT CODE — a code like "CMP097855", "CODE#CMP097855", or similar alphanumeric string. This is a manufacturer catalog code that uniquely identifies the product/set/release. Transcribe it EXACTLY.
2. SET/INSERT CODE — a short code before the card number (e.g. "T88-10" means the "T88" 1988 Topps insert, card #10; "BCP-42" = Bowman Chrome Prospects #42; "PSC" = Prizm Silver Chrome). This identifies the specific insert or subset.
3. COPYRIGHT YEAR — text like "© 2024 Topps" or "© 2025 Panini" — this is the ACTUAL release year. Do NOT guess the year from player stats.
4. SET NAME — often printed in a header, footer, or alongside the card number (e.g. "TOPPS CHROME", "DONRUSS RATED ROOKIE", "PRIZM", "1990 TOPPS CHROME SILVER PACK").
5. CARD NUMBER — e.g. "#123", "BCP-42", "T88-10"
Read EVERY piece of fine print on the back before making your identification.

Also examine:
- The FRONT portion for: card design style, border color/pattern, foil stamps, holographic effects, refractor rainbow patterns, numbered stamps (like /25, /50, /99), autograph stickers or on-card autos, jersey/patch swatches, RC logo, the uniform the player is wearing.
- The BACK portion for: the card number (e.g. "#123" or "BCP-42"), the set name in the header or footer, copyright year, product code, any "PARALLEL" or "INSERT" text, print run stamps.

Use ALL of this information to identify the EXACT card. Do not guess the year from the player's stats — use the copyright year on the back.

Return ONLY a valid JSON object — no markdown, no explanation:
{
  "player_name": "Full player name, or null",
  "year": <4-digit integer from the copyright year on the back, or null>,
  "brand": "Topps / Panini / Upper Deck / Bowman / Donruss / Fleer / Score / Leaf / etc., or null",
  "set_name": "The specific set name (e.g. 'Topps Chrome', 'Prizm', 'Stadium Club', 'Bowman 1st', 'Donruss Rated Rookie', 'Select', 'Mosaic') — NOT just the brand, or null",
  "subset": "e.g. All-Star, Draft Picks, Rookie Debut, or null",
  "insert_set": "If this is an insert card, the insert set name (e.g. '1989 Topps', 'Silver Pack Mojo', 'Finest Flashbacks', '1990 Topps Chrome', 'Wander Franco Generation Now') — use the product code to identify this, or null",
  "card_number": "Card number/ID exactly as printed (e.g. '123', 'BCP-42', 'T88-10', 'MLMAR-CB'). Include the full alphanumeric code — the prefix often identifies the insert set (e.g. MLMAR = Major League Marquee, T88 = 1988 Topps insert), or null",
  "team": "team name, or null",
  "sport": "Baseball / Basketball / Football / Hockey / Soccer / Other",
  "product_code": "The manufacturer/catalog code from the back (e.g. 'CMP097855', 'CODE#CMP097855') — transcribe EXACTLY as printed, or null",
  "is_rookie_card": true or false,
  "is_parallel": true or false,
  "parallel_name": "Be specific — e.g. 'Gold /2024', 'Rainbow Foil', 'Prizm Silver', 'Refractor', 'Holo', 'Scope', 'Speckle', 'Disco', 'Mojo', 'Green Shimmer', 'Red /199', 'Blue /150', 'Purple /75', or null. LOOK for visual cues: rainbow sheen = refractor, colored border = color parallel, sparkle = foil/shimmer",
  "is_foil": true or false,
  "is_autograph": true or false,
  "is_relic": true or false,
  "relic_type": "Jersey / Patch / Bat / Ball / Glove / etc., or null",
  "is_numbered": true or false,
  "print_run": <integer — the total from the stamp e.g. 25 for /25, or null>,
  "serial_number": "the stamped number exactly as shown e.g. '15/25', or null",
  "has_alternate_jersey": true or false,
  "jersey_description": "e.g. City Connect, All-Star, Throwback, Spring Training, Players Weekend, or null",
  "is_short_print": true or false,
  "condition": "Mint / Near Mint / Excellent / Very Good / Good / Poor  (visual estimate — look at centering, corners, edges, surface)",
  "notable_features": "Any other notable features as a plain string, or null",
  "description": "1-2 sentence summary including the exact set identification (e.g. '2024 Topps Series 1 1989 Topps Silver Pack Chrome insert of Juan Soto')"
}

IMPORTANT: If you are uncertain about ANY field, set it to null rather than guessing.

CRITICAL: If the image is NOT a sports card (e.g. it's a random photo, a non-sports item, a meme, a document, etc.), return ONLY this JSON:
{"error": "not_a_sports_card", "description": "This does not appear to be a sports card."}
Only analyze images that are clearly sports trading cards (baseball, basketball, football, hockey, soccer, etc.)."""


def analyze_card_combined(image_path: str, retries: int = 3,
                          set_hint: str | None = None) -> dict:
    """
    Analyze a single image containing both front and back of a card.
    Used for flatbed scanner bulk uploads where both sides are in one file.
    Same two-pass process as analyze_card() but with a combined prompt.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set.")

    client = anthropic.Anthropic(api_key=api_key)
    image_content = _make_image_content(image_path)

    prompt_text = COMBINED_ANALYSIS_PROMPT
    if set_hint and set_hint.strip().lower() not in ("", "unknown"):
        prompt_text += (
            f"\n\nIMPORTANT HINT FROM THE USER: The user has indicated this card "
            f"is from the **{set_hint.strip()}** set. Use this as a strong prior "
            f"when identifying the set_name, brand, and year. Still verify against "
            f"the copyright text and visual cues — if the card clearly contradicts "
            f"the hint, trust the physical evidence."
        )

    # Pass 1 — Initial analysis
    result = _call_claude(
        client, CLAUDE_MODEL, 1024,
        [
            {"type": "text", "text": prompt_text},
            image_content,
        ],
        retries=retries,
    )

    # Pass 2 — Web verification (pass the same image as both front and back)
    verified = _verify_with_ebay(client, image_content, image_content, result)
    if verified:
        result = verified

    return result


def analyze_card(front_path: str, back_path: str, retries: int = 3,
                  set_hint: str | None = None) -> dict:
    """
    Analyze a card's front and back images with Claude Vision.
    Two-pass process:
      1. Initial analysis — extract all metadata from images
      2. Web verification — search eBay for the card, then re-examine
         images against known parallels/variants from real listings
    Accepts local file paths or URLs (e.g. Cloudinary).
    ``set_hint`` is an optional user-provided set name (e.g. "Topps Chrome")
    that primes the prompt so the AI can narrow down the year/parallel.
    Returns a dict of card metadata.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set. "
                         "Get a key at https://console.anthropic.com")

    client = anthropic.Anthropic(api_key=api_key)

    front_content = _make_image_content(front_path)
    back_content = _make_image_content(back_path)

    # Build the prompt — append a set hint if the user provided one
    prompt_text = ANALYSIS_PROMPT
    if set_hint and set_hint.strip().lower() not in ("", "unknown"):
        prompt_text += (
            f"\n\nIMPORTANT HINT FROM THE USER: The user has indicated this card "
            f"is from the **{set_hint.strip()}** set. Use this as a strong prior "
            f"when identifying the set_name, brand, and year. Still verify against "
            f"the copyright text and visual cues — if the card clearly contradicts "
            f"the hint, trust the physical evidence."
        )

    # Pass 1 — Initial analysis
    result = _call_claude(
        client, CLAUDE_MODEL, 1024,
        [
            {"type": "text", "text": prompt_text},
            front_content,
            back_content,
        ],
        retries=retries,
    )

    # Pass 2 — Web verification (non-fatal if it fails)
    verified = _verify_with_ebay(client, front_content, back_content, result)
    if verified:
        result = verified

    return result
