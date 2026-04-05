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

    # ── Raw Data ─────────────────────────────────────────────────────────────
    raw_analysis = Column(Text)   # full JSON from Gemini
    notes = Column(Text)          # user-editable notes


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate(engine)


def _migrate(eng):
    """Add any columns that are missing from the existing table."""
    from sqlalchemy import inspect, text
    inspector = inspect(eng)
    if "cards" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("cards")}
    # Map of column_name -> SQL type for columns that may need adding
    new_columns = {
        "insert_set": "VARCHAR",
        "product_code": "VARCHAR",
        "ebay_search_url": "VARCHAR",
        "pricing_source": "VARCHAR",
    }
    with eng.begin() as conn:
        for col_name, col_type in new_columns.items():
            if col_name not in existing:
                conn.execute(text(f"ALTER TABLE cards ADD COLUMN {col_name} {col_type}"))
                print(f"[migrate] Added column: {col_name}")
