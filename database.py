import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, String, Integer, Boolean, Float, DateTime, Text
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./cards.db")

connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class Card(Base):
    __tablename__ = "cards"

    id = Column(String, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    front_image_path = Column(String)
    back_image_path = Column(String)

    # Workflow status: pending → analyzing → analyzed → pricing → complete | error
    status = Column(String, default="pending")

    # ── Card Identity ────────────────────────────────────────────────────────
    player_name = Column(String)
    year = Column(Integer)
    brand = Column(String)       # Topps, Panini, Upper Deck, Bowman, Donruss …
    set_name = Column(String)    # Prizm, Chrome, Stadium Club, Heritage …
    subset = Column(String)      # All-Star, Draft Picks …
    insert_set = Column(String)  # Rated Rookie, Silver Pack, Mojo …
    product_code = Column(String)  # Code from back of card (T88, PSC, BCP…)
    card_number = Column(String)
    team = Column(String)
    sport = Column(String)       # Baseball, Basketball, Football …

    # ── Special Attributes ──────────────────────────────────────────────────
    is_rookie_card = Column(Boolean, default=False)
    is_parallel = Column(Boolean, default=False)
    parallel_name = Column(String)      # Gold, Rainbow Foil, Refractor …
    is_foil = Column(Boolean, default=False)
    is_autograph = Column(Boolean, default=False)
    is_relic = Column(Boolean, default=False)
    relic_type = Column(String)         # Jersey, Patch, Bat …
    is_numbered = Column(Boolean, default=False)
    print_run = Column(Integer)         # e.g. 25 for /25
    serial_number = Column(String)      # e.g. "15/25"
    has_alternate_jersey = Column(Boolean, default=False)
    jersey_description = Column(String) # City Connect, All-Star, Throwback …
    is_short_print = Column(Boolean, default=False)
    condition = Column(String)          # Mint, Near Mint, Excellent …
    notable_features = Column(Text)
    description = Column(Text)

    # ── Pricing ─────────────────────────────────────────────────────────────
    estimated_price = Column(Float)
    ebay_avg_sale = Column(Float)
    ebay_low = Column(Float)
    ebay_high = Column(Float)
    ebay_num_sales = Column(Integer)
    ebay_last_checked = Column(DateTime)
    ebay_search_query = Column(String)
    ebay_search_url = Column(String)   # Direct eBay link for manual verification
    pricing_source = Column(String)    # "ebay_sold" or "ai_estimate"
    # Graded pricing (PSA/BGS/SGC)
    graded_avg = Column(Float)
    graded_low = Column(Float)
    graded_high = Column(Float)
    graded_num_sales = Column(Integer)

    # ── Ownership ────────────────────────────────────────────────────────────
    owner_id = Column(String)     # FK -> users.id (no constraint for migration ease)

    # ── Privacy ─────────────────────────────────────────────────────────────
    # Public cards show up on the shared "Card Collection" tab for everyone.
    # Private cards (default) are only visible to the owner and admins.
    is_public = Column(Boolean, default=False)

    # ── Scanner input ──────────────────────────────────────────────────────
    # Optional hint the user provided at scan time ("set dropdown") to help
    # the AI narrow down the year/parallel. Free-form text.
    set_hint = Column(String)

    # ── Raw Data ─────────────────────────────────────────────────────────────
    raw_analysis = Column(Text)   # full JSON from Gemini
    notes = Column(Text)          # user-editable notes


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True)
    username = Column(String, unique=True, nullable=False, index=True)
    email = Column(String)
    password_hash = Column(String, nullable=False)
    is_admin = Column(Boolean, default=False)
    card_limit = Column(Integer, default=5)             # max cards user can upload
    subscription_tier = Column(String, default="free")  # "free" | "pro" | "unlimited"
    subscription_expires_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    # ── Profile ─────────────────────────────────────────────────────────────
    first_name = Column(String)
    last_name = Column(String)
    phone = Column(String)
    profile_picture = Column(String)  # relative URL path to uploaded image

    # ── Privacy ─────────────────────────────────────────────────────────────
    # When True, the user's name is hidden on public cards (shows "Anonymous")
    anonymize_cards = Column(Boolean, default=False)

    # ── Email verification ──────────────────────────────────────────────────
    email_verified = Column(Boolean, default=False)
    email_verify_token = Column(String)
    email_verify_sent_at = Column(DateTime)

    # ── Usage tracking ─────────────────────────────────────────────────────
    # Lifetime counters that survive card deletion so "deleted cards still
    # count towards the user's monthly scan limit."
    scans_this_month = Column(Integer, default=0)   # scans in current month
    scans_month_key = Column(String)                 # "2026-04" — reset trigger
    reprices_this_month = Column(Integer, default=0)
    reprices_month_key = Column(String)

    # ── Password reset (email-based) ────────────────────────────────────────
    password_reset_token = Column(String)
    password_reset_expires = Column(DateTime)


class PasswordResetRequest(Base):
    __tablename__ = "password_reset_requests"

    id = Column(String, primary_key=True)
    username = Column(String, nullable=False)
    email = Column(String)
    status = Column(String, default="pending")   # "pending" | "resolved"
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate(engine)


def _migrate(eng):
    """Add any columns that are missing from the existing tables."""
    from sqlalchemy import inspect, text
    inspector = inspect(eng)
    tables = inspector.get_table_names()

    with eng.begin() as conn:
        # ── cards table ─────────────────────────────────────────────────────
        if "cards" in tables:
            existing = {col["name"] for col in inspector.get_columns("cards")}
            card_cols = {
                "insert_set": "VARCHAR",
                "product_code": "VARCHAR",
                "ebay_search_url": "VARCHAR",
                "pricing_source": "VARCHAR",
                "graded_avg": "FLOAT",
                "graded_low": "FLOAT",
                "graded_high": "FLOAT",
                "graded_num_sales": "INTEGER",
                "owner_id": "VARCHAR",
                "is_public": "BOOLEAN",
                "set_hint": "VARCHAR",
            }
            for col_name, col_type in card_cols.items():
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE cards ADD COLUMN {col_name} {col_type}"))
                    print(f"[migrate] cards: added column {col_name}")

        # ── users table ─────────────────────────────────────────────────────
        if "users" in tables:
            existing = {col["name"] for col in inspector.get_columns("users")}
            user_cols = {
                "first_name": "VARCHAR",
                "last_name": "VARCHAR",
                "phone": "VARCHAR",
                "anonymize_cards": "BOOLEAN",
                "email_verified": "BOOLEAN",
                "email_verify_token": "VARCHAR",
                "email_verify_sent_at": "TIMESTAMP",
                "password_reset_token": "VARCHAR",
                "password_reset_expires": "TIMESTAMP",
                "scans_this_month": "INTEGER",
                "scans_month_key": "VARCHAR",
                "reprices_this_month": "INTEGER",
                "reprices_month_key": "VARCHAR",
                "profile_picture": "VARCHAR",
            }
            for col_name, col_type in user_cols.items():
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}"))
                    print(f"[migrate] users: added column {col_name}")
