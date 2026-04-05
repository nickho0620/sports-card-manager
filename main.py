"""
Sports Card Manager — FastAPI backend
Serves the mobile PWA and REST API.
"""
from dotenv import load_dotenv
load_dotenv(override=True)

import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import uuid
from datetime import datetime

import aiofiles
import cloudinary
import cloudinary.uploader
from fastapi import BackgroundTasks, Cookie, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from sqlalchemy import or_

from card_analyzer import analyze_card
from database import Card, SessionLocal, init_db
from ebay_pricing import get_ebay_pricing
from image_processor import process_card_scan

# ── App Setup ────────────────────────────────────────────────────────────────

app = FastAPI(title="Sports Card Manager", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = os.getenv("UPLOAD_DIR", os.path.join(os.path.dirname(__file__), "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# ── Auth ────────────────────────────────────────────────────────────────────
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))

# In-memory session store (survives until server restart)
_active_sessions: set[str] = set()


def _create_session() -> str:
    token = secrets.token_hex(32)
    _active_sessions.add(token)
    return token


def _check_auth(request: Request) -> bool:
    token = request.cookies.get("session")
    return token in _active_sessions if token else False


def require_auth(request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Login required")

# Cloudinary config (optional — falls back to local storage if not set)
USE_CLOUDINARY = bool(os.getenv("CLOUDINARY_CLOUD_NAME"))
if USE_CLOUDINARY:
    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
        api_key=os.getenv("CLOUDINARY_API_KEY"),
        api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    )


@app.on_event("startup")
def startup():
    init_db()


# Serve card images (local mode only — Cloudinary serves its own URLs)
if not USE_CLOUDINARY:
    app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
# Serve PWA static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Page Routes ──────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/scanner", include_in_schema=False)
def scanner():
    return FileResponse(os.path.join(STATIC_DIR, "scanner.html"))


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


# ── Auth API ────────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
def login(body: dict):
    username = body.get("username", "")
    password = body.get("password", "")
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        token = _create_session()
        resp = JSONResponse({"status": "ok"})
        resp.set_cookie(
            "session", token,
            httponly=True, samesite="lax",
            max_age=60 * 60 * 24 * 7,  # 7 days
        )
        return resp
    raise HTTPException(status_code=401, detail="Invalid username or password")


@app.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        _active_sessions.discard(token)
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie("session")
    return resp


@app.get("/api/auth/check")
def auth_check(request: Request):
    return {"authenticated": _check_auth(request)}


# ── Helper ───────────────────────────────────────────────────────────────────

def card_to_dict(card: Card) -> dict:
    # If paths are URLs (Cloudinary), use them directly; otherwise build local URLs
    front_url = card.front_image_path if card.front_image_path and card.front_image_path.startswith("http") else f"/uploads/{card.id}/front.jpg"
    back_url = card.back_image_path if card.back_image_path and card.back_image_path.startswith("http") else f"/uploads/{card.id}/back.jpg"
    return {
        "id": card.id,
        "created_at": (card.created_at.isoformat() + "Z") if card.created_at else None,
        "front_image_url": front_url,
        "back_image_url": back_url,
        "status": card.status,
        # Identity
        "player_name": card.player_name,
        "year": card.year,
        "brand": card.brand,
        "set_name": card.set_name,
        "subset": card.subset,
        "insert_set": card.insert_set,
        "product_code": card.product_code,
        "card_number": card.card_number,
        "team": card.team,
        "sport": card.sport,
        # Attributes
        "is_rookie_card": card.is_rookie_card,
        "is_parallel": card.is_parallel,
        "parallel_name": card.parallel_name,
        "is_foil": card.is_foil,
        "is_autograph": card.is_autograph,
        "is_relic": card.is_relic,
        "relic_type": card.relic_type,
        "is_numbered": card.is_numbered,
        "print_run": card.print_run,
        "serial_number": card.serial_number,
        "has_alternate_jersey": card.has_alternate_jersey,
        "jersey_description": card.jersey_description,
        "is_short_print": card.is_short_print,
        "condition": card.condition,
        "notable_features": card.notable_features,
        "description": card.description,
        # Pricing
        "estimated_price": card.estimated_price,
        "ebay_avg_sale": card.ebay_avg_sale,
        "ebay_low": card.ebay_low,
        "ebay_high": card.ebay_high,
        "ebay_num_sales": card.ebay_num_sales,
        "ebay_last_checked": (card.ebay_last_checked.isoformat() + "Z") if card.ebay_last_checked else None,
        "ebay_search_query": card.ebay_search_query,
        "ebay_search_url": card.ebay_search_url,
        "pricing_source": card.pricing_source,
        # Graded pricing
        "graded_avg": card.graded_avg,
        "graded_low": card.graded_low,
        "graded_high": card.graded_high,
        "graded_num_sales": card.graded_num_sales,
        # Meta
        "notes": card.notes,
    }


# ── Background Processing ────────────────────────────────────────────────────

def process_card(card_id: str):
    """Analyze card with Gemini, then fetch eBay pricing. Runs in thread pool."""
    db = SessionLocal()
    try:
        card = db.get(Card, card_id)
        if not card:
            return

        # Step 1 — AI analysis
        card.status = "analyzing"
        db.commit()

        try:
            analysis = analyze_card(card.front_image_path, card.back_image_path)
            # Map analysis fields onto the card model
            field_map = {
                "player_name", "year", "brand", "set_name", "subset", "insert_set",
                "product_code", "card_number",
                "team", "sport", "is_rookie_card", "is_parallel", "parallel_name",
                "is_foil", "is_autograph", "is_relic", "relic_type", "is_numbered",
                "print_run", "serial_number", "has_alternate_jersey", "jersey_description",
                "is_short_print", "condition", "notable_features", "description",
            }
            for field in field_map:
                if field in analysis and analysis[field] is not None:
                    setattr(card, field, analysis[field])
            card.raw_analysis = json.dumps(analysis)
            card.status = "analyzed"
            db.commit()
        except Exception as e:
            card.status = "error"
            card.notes = f"Analysis error: {e}"
            db.commit()
            return

        # Step 2 — eBay pricing
        card.status = "pricing"
        db.commit()

        try:
            pricing = get_ebay_pricing(card)
            if pricing:
                card.ebay_avg_sale = pricing["avg"]
                card.ebay_low = pricing["low"]
                card.ebay_high = pricing["high"]
                card.ebay_num_sales = pricing["num_sales"]
                card.ebay_last_checked = datetime.utcnow()
                card.ebay_search_query = pricing["search_query"]
                card.ebay_search_url = pricing.get("search_url")
                card.pricing_source = pricing.get("source")
                card.estimated_price = pricing["avg"]
                card.graded_avg = pricing.get("graded_avg")
                card.graded_low = pricing.get("graded_low")
                card.graded_high = pricing.get("graded_high")
                card.graded_num_sales = pricing.get("graded_num_sales")
        except Exception:
            pass  # Pricing failure is non-fatal

        card.status = "complete"
        db.commit()

    finally:
        db.close()


def refresh_pricing(card_id: str):
    """Re-run eBay pricing for an existing card."""
    db = SessionLocal()
    try:
        card = db.get(Card, card_id)
        if not card:
            return
        pricing = get_ebay_pricing(card)
        if pricing:
            card.ebay_avg_sale = pricing["avg"]
            card.ebay_low = pricing["low"]
            card.ebay_high = pricing["high"]
            card.ebay_num_sales = pricing["num_sales"]
            card.ebay_last_checked = datetime.utcnow()
            card.ebay_search_query = pricing["search_query"]
            card.ebay_search_url = pricing.get("search_url")
            card.pricing_source = pricing.get("source")
            card.estimated_price = pricing["avg"]
            card.graded_avg = pricing.get("graded_avg")
            card.graded_low = pricing.get("graded_low")
            card.graded_high = pricing.get("graded_high")
            card.graded_num_sales = pricing.get("graded_num_sales")
            db.commit()
    finally:
        db.close()


# ── API Routes ───────────────────────────────────────────────────────────────

def _resize_image_bytes(raw_bytes: bytes) -> bytes:
    """Resize image to max 2000px and convert to JPEG."""
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    max_dim = 2000
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


@app.get("/api/cards")
def list_cards(
    search: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=60, le=200),
    offset: int = Query(default=0),
):
    """List all cards with optional search + status filter."""
    db = SessionLocal()
    try:
        q = db.query(Card)
        if search:
            like = f"%{search}%"
            q = q.filter(
                or_(
                    Card.player_name.ilike(like),
                    Card.brand.ilike(like),
                    Card.set_name.ilike(like),
                    Card.team.ilike(like),
                    Card.parallel_name.ilike(like),
                )
            )
        if status:
            q = q.filter(Card.status == status)

        total = q.count()
        cards = q.order_by(Card.created_at.desc()).offset(offset).limit(limit).all()

        # Summary stats (unfiltered)
        all_complete = db.query(Card).filter(Card.status == "complete").all()
        total_value = sum(c.estimated_price for c in all_complete if c.estimated_price)
        stats = {
            "total_cards": db.query(Card).count(),
            "complete": len(all_complete),
            "total_value": round(total_value, 2),
        }

        return {
            "cards": [card_to_dict(c) for c in cards],
            "total": total,
            "stats": stats,
        }
    finally:
        db.close()


@app.get("/api/cards/{card_id}")
def get_card(card_id: str):
    db = SessionLocal()
    try:
        card = db.get(Card, card_id)
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")
        return card_to_dict(card)
    finally:
        db.close()


@app.post("/api/cards/{card_id}/image/{side}")
async def update_card_image(
    request: Request,
    card_id: str,
    side: str,
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...),
    reanalyze: bool = Query(default=False),
    scan_mode: bool = Query(default=False),
):
    """Replace front or back image for an existing card."""
    require_auth(request)
    if side not in ("front", "back"):
        raise HTTPException(status_code=400, detail="Side must be 'front' or 'back'")

    db = SessionLocal()
    try:
        card = db.get(Card, card_id)
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")

        raw = await image.read()
        img_bytes = process_card_scan(raw) if scan_mode else _resize_image_bytes(raw)

        if USE_CLOUDINARY:
            result = cloudinary.uploader.upload(
                img_bytes, folder=f"cards/{card_id}", public_id=side,
                resource_type="image", overwrite=True,
            )
            path = result["secure_url"]
        else:
            card_dir = os.path.join(UPLOAD_DIR, card_id)
            os.makedirs(card_dir, exist_ok=True)
            path = os.path.join(card_dir, f"{side}.jpg")
            async with aiofiles.open(path, "wb") as f:
                await f.write(img_bytes)

        if side == "front":
            card.front_image_path = path
        else:
            card.back_image_path = path
        db.commit()

        if reanalyze and card.front_image_path and card.back_image_path:
            background_tasks.add_task(process_card, card_id)

        return card_to_dict(card)
    finally:
        db.close()


@app.post("/api/cards/upload")
async def upload_card(
    request: Request,
    background_tasks: BackgroundTasks,
    front_image: UploadFile = File(None),
    back_image: UploadFile = File(None),
    scan_mode: bool = Query(default=False),
):
    """Receive front and/or back images. At least one required. scan_mode applies edge detection + perspective correction."""
    require_auth(request)
    if not front_image and not back_image:
        raise HTTPException(status_code=400, detail="At least one image is required")

    card_id = str(uuid.uuid4())
    front_path = None
    back_path = None

    if front_image:
        raw = await front_image.read()
        front_bytes = process_card_scan(raw) if scan_mode else _resize_image_bytes(raw)
        if USE_CLOUDINARY:
            result = cloudinary.uploader.upload(
                front_bytes, folder=f"cards/{card_id}", public_id="front",
                resource_type="image",
            )
            front_path = result["secure_url"]
        else:
            card_dir = os.path.join(UPLOAD_DIR, card_id)
            os.makedirs(card_dir, exist_ok=True)
            fp = os.path.join(card_dir, "front.jpg")
            async with aiofiles.open(fp, "wb") as f:
                await f.write(front_bytes)
            front_path = fp

    if back_image:
        raw = await back_image.read()
        back_bytes = process_card_scan(raw) if scan_mode else _resize_image_bytes(raw)
        if USE_CLOUDINARY:
            result = cloudinary.uploader.upload(
                back_bytes, folder=f"cards/{card_id}", public_id="back",
                resource_type="image",
            )
            back_path = result["secure_url"]
        else:
            card_dir = os.path.join(UPLOAD_DIR, card_id)
            os.makedirs(card_dir, exist_ok=True)
            bp = os.path.join(card_dir, "back.jpg")
            async with aiofiles.open(bp, "wb") as f:
                await f.write(back_bytes)
            back_path = bp

    db = SessionLocal()
    card = Card(
        id=card_id,
        front_image_path=front_path,
        back_image_path=back_path,
        status="pending",
    )
    db.add(card)
    db.commit()
    db.close()

    # Only auto-analyze if both images are present
    if front_path and back_path:
        background_tasks.add_task(process_card, card_id)

    return {"card_id": card_id, "status": "pending"}


@app.patch("/api/cards/{card_id}")
def update_card(request: Request, card_id: str, body: dict):
    """Update any card field. Supports all detail fields + notes."""
    require_auth(request)
    allowed = {
        "notes", "condition", "estimated_price", "player_name", "year",
        "brand", "set_name", "team", "description", "subset", "insert_set", "product_code", "card_number",
        "sport", "is_rookie_card", "is_parallel", "parallel_name", "is_foil",
        "is_autograph", "is_relic", "relic_type", "is_numbered", "print_run",
        "serial_number", "has_alternate_jersey", "jersey_description",
        "is_short_print", "notable_features",
    }
    db = SessionLocal()
    try:
        card = db.get(Card, card_id)
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")
        for key, value in body.items():
            if key in allowed:
                setattr(card, key, value)
        db.commit()
        return card_to_dict(card)
    finally:
        db.close()


@app.post("/api/cards/{card_id}/reprice")
def reprice_card(request: Request, card_id: str, background_tasks: BackgroundTasks):
    """Trigger a fresh eBay pricing lookup."""
    require_auth(request)
    db = SessionLocal()
    try:
        card = db.get(Card, card_id)
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")
    finally:
        db.close()
    background_tasks.add_task(refresh_pricing, card_id)
    return {"status": "repricing"}


@app.delete("/api/cards/{card_id}")
def delete_card(request: Request, card_id: str):
    """Delete a card and its images."""
    require_auth(request)
    db = SessionLocal()
    try:
        card = db.get(Card, card_id)
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")
        if USE_CLOUDINARY:
            try:
                cloudinary.uploader.destroy(f"cards/{card_id}/front")
                cloudinary.uploader.destroy(f"cards/{card_id}/back")
            except Exception:
                pass  # Non-fatal if Cloudinary cleanup fails
        else:
            card_dir = os.path.join(UPLOAD_DIR, card_id)
            if os.path.exists(card_dir):
                shutil.rmtree(card_dir)
        db.delete(card)
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()


@app.get("/api/export/csv")
def export_csv():
    """Download all cards as a CSV spreadsheet."""
    db = SessionLocal()
    try:
        cards = db.query(Card).order_by(Card.created_at.desc()).all()
        output = io.StringIO()
        writer = csv.writer(output)

        # Header row
        writer.writerow([
            "ID", "Date Added", "Status", "Player", "Year", "Brand", "Set",
            "Subset", "Insert Set", "Product Code", "Card #", "Team", "Sport", "Rookie", "Parallel",
            "Parallel Name", "Foil", "Autograph", "Relic", "Relic Type",
            "Numbered", "Print Run", "Serial #", "Alt Jersey",
            "Jersey Desc", "Short Print", "Condition", "Notable Features",
            "Description", "Est. Price", "eBay Avg", "eBay Low", "eBay High",
            "eBay # Sales", "eBay Last Checked", "Pricing Source", "Notes",
        ])

        for c in cards:
            writer.writerow([
                c.id, c.created_at, c.status, c.player_name, c.year,
                c.brand, c.set_name, c.subset, c.insert_set, c.product_code, c.card_number, c.team,
                c.sport, c.is_rookie_card, c.is_parallel, c.parallel_name,
                c.is_foil, c.is_autograph, c.is_relic, c.relic_type,
                c.is_numbered, c.print_run, c.serial_number,
                c.has_alternate_jersey, c.jersey_description,
                c.is_short_print, c.condition, c.notable_features,
                c.description, c.estimated_price, c.ebay_avg_sale,
                c.ebay_low, c.ebay_high, c.ebay_num_sales,
                c.ebay_last_checked, c.pricing_source, c.notes,
            ])

        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=sports_cards_inventory.csv"},
        )
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok"}


EBAY_VERIFICATION_TOKEN = os.getenv("EBAY_VERIFICATION_TOKEN", "sportscardmanager2026verificationtoken")


@app.get("/api/ebay/account-deletion")
def ebay_account_deletion_challenge(challenge_code: str = Query(default="")):
    """eBay verification challenge — echoes back the challenge code."""
    import hashlib
    endpoint = "https://sports-card-manager.onrender.com/api/ebay/account-deletion"
    hash_input = challenge_code + EBAY_VERIFICATION_TOKEN + endpoint
    response_hash = hashlib.sha256(hash_input.encode()).hexdigest()
    return {"challengeResponse": response_hash}


@app.post("/api/ebay/account-deletion")
def ebay_account_deletion(body: dict = {}):
    """eBay Marketplace Account Deletion webhook — required for eBay API compliance.
    We don't store any eBay user data, so this just acknowledges the request."""
    return {"status": "ok"}


@app.get("/api/debug")
def debug():
    """Debug endpoint — shows DB state and any errors."""
    from sqlalchemy import inspect, text
    db = SessionLocal()
    try:
        inspector = inspect(db.bind)
        tables = inspector.get_table_names()
        cols = []
        if "cards" in tables:
            cols = [c["name"] for c in inspector.get_columns("cards")]
        count = db.execute(text("SELECT COUNT(*) FROM cards")).scalar() if "cards" in tables else 0
        # Try the actual query that's failing
        error = None
        try:
            cards = db.query(Card).limit(1).all()
            if cards:
                card_to_dict(cards[0])
        except Exception as e:
            error = str(e)
        return {"tables": tables, "columns": cols, "card_count": count, "error": error}
    except Exception as e:
        return {"error": str(e)}
    finally:
        db.close()
