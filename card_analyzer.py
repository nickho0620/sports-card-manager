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


def analyze_card(front_path: str, back_path: str, retries: int = 3) -> dict:
    """
    Analyze a card's front and back images with Claude Vision.
    Returns a dict of card metadata.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set. "
                         "Get a key at https://console.anthropic.com")

    client = anthropic.Anthropic(api_key=api_key)

    front_b64, front_type = _load_image_b64(front_path)
    back_b64, back_type = _load_image_b64(back_path)

    for attempt in range(retries):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": ANALYSIS_PROMPT},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": front_type,
                                "data": front_b64,
                            },
                        },
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": back_type,
                                "data": back_b64,
                            },
                        },
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
