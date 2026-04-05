"""
Card analyzer — uses Claude Vision (Anthropic) to extract all metadata from
front + back card images. Falls back to Google Gemini if configured.
"""
import base64
import io
import json
import os
import time

import anthropic
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
}"""


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


def analyze_card(front_path: str, back_path: str, retries: int = 3) -> dict:
    """
    Analyze a card's front and back images with Claude Vision.
    Accepts local file paths or URLs (e.g. Cloudinary).
    Returns a dict of card metadata.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set. "
                         "Get a key at https://console.anthropic.com")

    client = anthropic.Anthropic(api_key=api_key)

    front_content = _make_image_content(front_path)
    back_content = _make_image_content(back_path)

    for attempt in range(retries):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": ANALYSIS_PROMPT},
                        front_content,
                        back_content,
                    ],
                }],
            )
            raw = _clean_json(response.content[0].text)
            result = json.loads(raw)
            return result
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
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
