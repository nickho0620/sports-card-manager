"""
Card analyzer — uses Google Gemini Vision to extract all metadata from
front + back card images.
"""
import base64
import json
import os
import time

import google.generativeai as genai
from PIL import Image
import io

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

ANALYSIS_PROMPT = """You are an expert sports card grader and identifier.
Analyze the front and back of this card and return ONLY a valid JSON object — no markdown, no explanation.

Extract every detail you can see:
{
  "player_name": "Full player name, or null",
  "year": <4-digit integer or null>,
  "brand": "Topps / Panini / Upper Deck / Bowman / Donruss / Fleer / Score / Leaf / etc., or null",
  "set_name": "e.g. Prizm, Chrome, Stadium Club, Heritage, Finest, Select, Mosaic, etc., or null",
  "subset": "e.g. All-Star, Draft Picks, or null",
  "card_number": "card number as printed, or null",
  "team": "team name, or null",
  "sport": "Baseball / Basketball / Football / Hockey / Soccer / Other",
  "is_rookie_card": true or false,
  "is_parallel": true or false,
  "parallel_name": "e.g. Gold, Rainbow Foil, Prizm Silver, Refractor, Holo, Scope, Disco, etc., or null",
  "is_foil": true or false,
  "is_autograph": true or false,
  "is_relic": true or false,
  "relic_type": "Jersey / Patch / Bat / Ball / Glove / etc., or null",
  "is_numbered": true or false,
  "print_run": <integer — the total print run e.g. 25 for /25, or null>,
  "serial_number": "the stamped number as shown e.g. '15/25', or null",
  "has_alternate_jersey": true or false,
  "jersey_description": "e.g. City Connect, All-Star, Throwback, Spring Training, or null",
  "is_short_print": true or false,
  "condition": "Mint / Near Mint / Excellent / Very Good / Good / Poor  (visual estimate only)",
  "notable_features": "Any other notable features as a plain string, or null",
  "description": "1-2 sentence plain-English summary of the card"
}"""


def _load_image_part(path: str) -> dict:
    """Load an image from disk, resize if huge, return Gemini inline data."""
    with Image.open(path) as img:
        img = img.convert("RGB")
        # Cap at 2000px on longest side to stay within Gemini limits
        max_dim = 2000
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        data = base64.b64encode(buf.getvalue()).decode()
    return {"mime_type": "image/jpeg", "data": data}


def _clean_json(text: str) -> str:
    """Strip markdown code fences if Gemini wraps the JSON."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Drop first line (```json or ```) and last line (```)
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner).strip()
    return text


def analyze_card(front_path: str, back_path: str, retries: int = 3) -> dict:
    """
    Analyze a card's front and back images with Gemini Vision.
    Returns a dict of card metadata.
    Raises an exception if analysis fails after all retries.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set. "
                         "Get a free key at https://aistudio.google.com")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)

    front_part = _load_image_part(front_path)
    back_part = _load_image_part(back_path)

    for attempt in range(retries):
        try:
            response = model.generate_content([
                ANALYSIS_PROMPT,
                {"inline_data": front_part},
                {"inline_data": back_part},
            ])
            raw = _clean_json(response.text)
            result = json.loads(raw)
            return result
        except json.JSONDecodeError:
            # Gemini returned something that isn't valid JSON — retry
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise ValueError(f"Gemini returned invalid JSON after {retries} attempts: {response.text[:500]}")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
