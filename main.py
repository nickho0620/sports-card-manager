"""
Sports Card Manager — FastAPI backend
Serves the mobile PWA and REST API.
"""
from dotenv import load_dotenv
load_dotenv()

import json
import os
import shutil
import uuid
from datetime import datetime

import aiofiles
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import or_

from card_analyzer import analyze_card
from database import Card, SessionLocal, init_db
from ebay_pricing import get_ebay_pricing

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


@app.on_event("startup")
def startup():
    init_db()


# Serve card images
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


# ── Helper ───────────────────────────────────────────────────────────────────

def card_to_dict(card: Card) -> dict:
    return {
        "id": card.id,
        "created_at": card.created_at.isoformat() if card.created_at else None,
        "front_image_url": f"/uploads/{card.id}/front.jpg",
        "back_image_url": f"/uploads/{card.id}/back.jpg",
        "status": card.status,
        # Identity
        "player_name": card.player_name,
        "year": card.year,
        "brand": card.brand,
        "set_name": card.set_name,
        "subset": card.subset,
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
        "ebay_last_checked": card.ebay_last_checked.isoformat() if card.ebay_last_checked else None,
        "ebay_search_query": card.ebay_search_query,
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
                "player_name", "year", "brand", "set_name", "subset", "card_number",
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
                card.estimated_price = pricing["avg"]
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
            card.estimated_price = pricing["avg"]
            db.commit()
    finally:
        db.close()


# ── API Routes ───────────────────────────────────────────────────────────────

@app.post("/api/cards/upload")
async def upload_card(
    background_tasks: BackgroundTasks,
    front_image: UploadFile = File(...),
    back_image: UploadFile = File(...),
):
    """Receive front + back images from the mobile scanner."""
    card_id = str(uuid.uuid4())
    card_dir = os.path.join(UPLOAD_DIR, card_id)
    os.makedirs(card_dir, exist_ok=True)

    front_path = os.path.join(card_dir, "front.jpg")
    back_path = os.path.join(card_dir, "back.jpg")

    async with aiofiles.open(front_path, "wb") as f:
        await f.write(await front_image.read())
    async with aiofiles.open(back_path, "wb") as f:
        await f.write(await back_image.read())

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

    background_tasks.add_task(process_card, card_id)

    return {"card_id": card_id, "status": "pending"}


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


@app.patch("/api/cards/{card_id}")
def update_card(card_id: str, body: dict):
    """Update user-editable fields: notes, condition, estimated_price, player_name, etc."""
    allowed = {
        "notes", "condition", "estimated_price", "player_name", "year",
        "brand", "set_name", "team", "description",
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
def reprice_card(card_id: str, background_tasks: BackgroundTasks):
    """Trigger a fresh eBay pricing lookup."""
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
def delete_card(card_id: str):
    """Delete a card and its images."""
    db = SessionLocal()
    try:
        card = db.get(Card, card_id)
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")
        card_dir = os.path.join(UPLOAD_DIR, card_id)
        if os.path.exists(card_dir):
            shutil.rmtree(card_dir)
        db.delete(card)
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok"}
