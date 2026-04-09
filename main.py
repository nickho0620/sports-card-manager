"""
Card Radar — FastAPI backend
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
import sys
import uuid
from datetime import datetime

# Force stdout/stderr to flush line-by-line so print() statements show up
# immediately in Render's log viewer. Python otherwise block-buffers when
# stdout is not a TTY, which makes server-side debugging impossible.
try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except Exception:
    pass

from datetime import timedelta

import aiofiles
import cloudinary
import cloudinary.uploader
from fastapi import BackgroundTasks, Cookie, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from sqlalchemy import or_

from card_analyzer import analyze_card
from database import Card, PasswordResetRequest, SessionLocal, User, init_db
from ebay_pricing import get_ebay_pricing
from email_service import (
    get_base_url,
    password_reset_email_html,
    send_email,
    smtp_diagnostic,
    verification_email_html,
)
from image_processor import process_card_scan

# ── App Setup ────────────────────────────────────────────────────────────────

app = FastAPI(title="Card Radar", version="1.0.0")

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

# In-memory session store: token -> user_id
_active_sessions: dict[str, str] = {}


def _hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 with random salt. Format: salt$hash (hex)."""
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + "$" + derived.hex()


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        return hmac.compare_digest(derived.hex(), hash_hex)
    except Exception:
        return False


def _create_session(user_id: str) -> str:
    token = secrets.token_hex(32)
    _active_sessions[token] = user_id
    return token


def _current_user_id(request: Request) -> str | None:
    token = request.cookies.get("session")
    return _active_sessions.get(token) if token else None


def _current_user(request: Request) -> User | None:
    uid = _current_user_id(request)
    if not uid:
        return None
    db = SessionLocal()
    try:
        return db.get(User, uid)
    finally:
        db.close()


def require_auth(request: Request) -> User:
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    return user


def require_admin(request: Request) -> User:
    user = require_auth(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _bootstrap_admin():
    """Create the admin user from env vars if it doesn't exist."""
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == ADMIN_USERNAME).first()
        if existing:
            # Ensure flagged as admin
            if not existing.is_admin:
                existing.is_admin = True
                db.commit()
            return
        admin = User(
            id=str(uuid.uuid4()),
            username=ADMIN_USERNAME,
            password_hash=_hash_password(ADMIN_PASSWORD),
            is_admin=True,
            card_limit=999999,
            subscription_tier="unlimited",
        )
        db.add(admin)
        db.commit()
        print(f"[auth] Bootstrapped admin user: {ADMIN_USERNAME}")
    finally:
        db.close()


def _user_card_count(db, user_id: str) -> int:
    """Total cards owned by a user (lifetime)."""
    return db.query(Card).filter(Card.owner_id == user_id).count()


def _user_monthly_card_count(db, user_id: str) -> int:
    """Cards created by a user in the current calendar month."""
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return db.query(Card).filter(
        Card.owner_id == user_id,
        Card.created_at >= month_start,
    ).count()

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
    _bootstrap_admin()


# Serve card images (local mode only — Cloudinary serves its own URLs)
if not USE_CLOUDINARY:
    app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
# Serve PWA static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Page Routes ──────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root(request: Request):
    # Logged-out visitors see the marketing landing page. Signed-in users
    # skip it and land straight in their collection dashboard.
    user = _current_user(request)
    if user:
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))
    return FileResponse(os.path.join(STATIC_DIR, "welcome.html"))


@app.get("/collection", include_in_schema=False)
def collection():
    # Public collection browser — guests can reach this via the welcome
    # page's "Browse Collection" CTA. Write actions are gated by the
    # frontend when no user is signed in.
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/scanner", include_in_schema=False)
def scanner(request: Request):
    # Scanner requires at least a signed-in free account. Guests get
    # bounced to the login page with a note telling them why.
    user = _current_user(request)
    if not user:
        return RedirectResponse(url="/login?scanner=1", status_code=302)
    return FileResponse(os.path.join(STATIC_DIR, "scanner.html"))


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


@app.get("/register", include_in_schema=False)
def register_page():
    return FileResponse(os.path.join(STATIC_DIR, "register.html"))


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(os.path.join(STATIC_DIR, "admin.html"))


@app.get("/profile", include_in_schema=False)
def profile_page(request: Request):
    # Profile is account-only: anonymous visitors get sent to the login page.
    user = _current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.is_admin and not user.email_verified:
        return RedirectResponse(url="/login?unverified=1", status_code=302)
    return FileResponse(os.path.join(STATIC_DIR, "profile.html"))


# ── Auth API ────────────────────────────────────────────────────────────────

def user_to_dict(user: User, db=None) -> dict:
    cards_used = 0
    monthly_cards_used = 0
    if db is not None:
        cards_used = _user_card_count(db, user.id)
        monthly_cards_used = _user_monthly_card_count(db, user.id)
    tier = (user.subscription_tier or "free").lower()
    # Effective limit depends on tier:
    #   Free = lifetime cap (card_limit)
    #   Pro = 100/month
    #   Unlimited/admin = unlimited
    if user.is_admin or tier == "unlimited":
        effective_limit = None  # unlimited
        effective_used = cards_used
    elif tier == "pro":
        effective_limit = 100
        effective_used = monthly_cards_used
    else:
        effective_limit = user.card_limit
        effective_used = cards_used
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "phone": user.phone,
        "email_verified": bool(user.email_verified),
        "is_admin": user.is_admin,
        "card_limit": effective_limit,
        "subscription_tier": user.subscription_tier,
        "subscription_expires_at": (user.subscription_expires_at.isoformat() + "Z") if user.subscription_expires_at else None,
        "anonymize_cards": bool(user.anonymize_cards),
        "cards_used": effective_used,
        "monthly_cards_used": monthly_cards_used,
        "total_cards": cards_used,
        "created_at": (user.created_at.isoformat() + "Z") if user.created_at else None,
    }


@app.post("/api/auth/register")
def register(body: dict, background_tasks: BackgroundTasks):
    print(f"[register] endpoint hit (new code path) body_keys={list(body.keys())}", flush=True)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    email = (body.get("email") or "").strip()
    first_name = (body.get("first_name") or "").strip()
    last_name = (body.get("last_name") or "").strip()
    phone = (body.get("phone") or "").strip() or None

    if not first_name:
        raise HTTPException(status_code=400, detail="First name is required")
    if not last_name:
        raise HTTPException(status_code=400, detail="Last name is required")
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Please enter a valid email address")
    if len(username) < 3 or len(username) > 32:
        raise HTTPException(status_code=400, detail="Username must be 3-32 characters")
    if not username.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Username may only contain letters, numbers, _ and -")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == username).first():
            raise HTTPException(status_code=400, detail="Username already taken")
        if email and db.query(User).filter(User.email == email).first():
            raise HTTPException(status_code=400, detail="An account with that email already exists")

        verify_token = secrets.token_urlsafe(32) if email else None
        user = User(
            id=str(uuid.uuid4()),
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            password_hash=_hash_password(password),
            is_admin=False,
            card_limit=5,
            subscription_tier="free",
            email_verified=False,
            email_verify_token=verify_token,
            email_verify_sent_at=datetime.utcnow() if verify_token else None,
        )
        db.add(user)
        db.commit()

        # Queue the verification email to send in the background so the
        # HTTP response returns instantly. The email handler logs success
        # or failure to the server logs.
        verify_url = f"{get_base_url()}/api/auth/verify-email?token={verify_token}"
        html, text = verification_email_html(username, verify_url)
        print(f"[register] queueing verification email to={email}", flush=True)
        background_tasks.add_task(
            send_email, email, "Verify your Card Radar account", html, text
        )
        print(f"[register] verification email queued for {email}", flush=True)

        # NOTE: we intentionally do NOT create a session here. The account
        # exists but is locked until the email is verified via the link.
        return JSONResponse({
            "status": "pending_verification",
            "message": "Account created! Check your email for a verification link to activate your account.",
            "email": email,
            "email_sent": True,
        })
    finally:
        db.close()


@app.get("/api/auth/verify-email", include_in_schema=False)
def verify_email(token: str = Query(...)):
    """User clicks the verification link from their email."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email_verify_token == token).first()
        if not user:
            return HTMLResponse(_verify_page_html(
                title="Verification Failed",
                icon="❌",
                color="#ef4444",
                message="This verification link is invalid or has already been used.",
            ), status_code=400)
        user.email_verified = True
        user.email_verify_token = None
        db.commit()
        return HTMLResponse(_verify_page_html(
            title="Email Verified!",
            icon="✅",
            color="#22c55e",
            message=f"Thanks, <strong>{user.username}</strong>! Your account is now active. Click below to sign in and start scanning cards.",
            button_href="/login?verified=1",
            button_label="Sign In →",
        ))
    finally:
        db.close()


@app.post("/api/auth/resend-verification")
def resend_verification(request: Request, background_tasks: BackgroundTasks):
    user = require_auth(request)
    if not user.email:
        raise HTTPException(status_code=400, detail="No email on file. Add one in your profile first.")
    if user.email_verified:
        return {"status": "already_verified"}
    db = SessionLocal()
    try:
        u = db.get(User, user.id)
        u.email_verify_token = secrets.token_urlsafe(32)
        u.email_verify_sent_at = datetime.utcnow()
        db.commit()
        verify_url = f"{get_base_url()}/api/auth/verify-email?token={u.email_verify_token}"
        html, text = verification_email_html(u.username, verify_url)
        background_tasks.add_task(
            send_email, u.email, "Verify your Card Radar account", html, text
        )
        return {"status": "sent"}
    finally:
        db.close()


def _verify_page_html(
    title: str,
    icon: str,
    color: str,
    message: str,
    button_href: str = "/",
    button_label: str = "← Back to Dashboard",
) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:#0f172a; color:#f1f5f9; margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; padding:24px; }}
  .card {{ background:#1e293b; border:1px solid #334155; border-radius:20px; padding:48px 40px; max-width:440px; text-align:center; }}
  .icon {{ font-size:64px; margin-bottom:16px; }}
  h1 {{ margin:0 0 16px; font-size:26px; color:{color}; }}
  p {{ color:#cbd5e1; line-height:1.6; margin:0 0 28px; }}
  a.btn {{ display:inline-block; background:#3b82f6; color:#fff; text-decoration:none; padding:12px 28px; border-radius:12px; font-weight:700; }}
  a.btn:hover {{ background:#2563eb; }}
</style></head>
<body>
  <div class="card">
    <div class="icon">{icon}</div>
    <h1>{title}</h1>
    <p>{message}</p>
    <a href="{button_href}" class="btn">{button_label}</a>
  </div>
</body></html>"""


@app.post("/api/auth/login")
def login(body: dict):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    db = SessionLocal()
    try:
        # Allow login by username OR email
        user = db.query(User).filter(
            or_(User.username == username, User.email == username)
        ).first()
        if not user or not _verify_password(password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        # Block login until email is verified (admins + pre-verified accounts bypass)
        if not user.is_admin and not user.email_verified:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "email_not_verified",
                    "message": "Please verify your email before signing in. Check your inbox for the verification link.",
                    "email": user.email,
                },
            )
        token = _create_session(user.id)
        resp = JSONResponse({"status": "ok", "user": user_to_dict(user, db)})
        resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7)
        return resp
    finally:
        db.close()


@app.post("/api/auth/resend-verification-public")
def resend_verification_public(body: dict, background_tasks: BackgroundTasks):
    """Public endpoint: resend verification email using username or email.

    Used from the login page when an unverified user tries to log in.
    Always returns 200 to avoid leaking which accounts exist.
    """
    identifier = (body.get("identifier") or "").strip()
    if not identifier:
        return {"status": "ok"}
    db = SessionLocal()
    try:
        user = db.query(User).filter(
            or_(User.username == identifier, User.email == identifier)
        ).first()
        if user and user.email and not user.email_verified:
            user.email_verify_token = secrets.token_urlsafe(32)
            user.email_verify_sent_at = datetime.utcnow()
            db.commit()
            verify_url = f"{get_base_url()}/api/auth/verify-email?token={user.email_verify_token}"
            html, text = verification_email_html(user.username, verify_url)
            background_tasks.add_task(
                send_email, user.email, "Verify your Card Radar account", html, text
            )
        return {"status": "ok"}
    finally:
        db.close()


@app.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        _active_sessions.pop(token, None)
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie("session")
    return resp


@app.get("/api/auth/check")
def auth_check(request: Request):
    user = _current_user(request)
    if not user:
        return {"authenticated": False}
    db = SessionLocal()
    try:
        return {"authenticated": True, "user": user_to_dict(user, db)}
    finally:
        db.close()


@app.post("/api/auth/forgot-password")
def forgot_password(body: dict, background_tasks: BackgroundTasks):
    """Email-based password reset.

    Accepts {username?, email?}. If the account has an email on file we send
    a time-limited reset link. We also log a PasswordResetRequest so the
    admin has a record (and a manual fallback if SMTP is not configured or
    the user has no email on file).
    """
    username = (body.get("username") or "").strip() or None
    email = (body.get("email") or "").strip() or None
    if not username and not email:
        raise HTTPException(status_code=400, detail="Please provide a username or email")

    db = SessionLocal()
    try:
        user = None
        if username:
            user = db.query(User).filter(User.username == username).first()
        if not user and email:
            user = db.query(User).filter(User.email == email).first()

        email_sent = False
        if user and user.email:
            user.password_reset_token = secrets.token_urlsafe(32)
            user.password_reset_expires = datetime.utcnow() + timedelta(hours=1)
            db.commit()
            reset_url = f"{get_base_url()}/reset-password?token={user.password_reset_token}"
            html, text = password_reset_email_html(user.username, reset_url)
            background_tasks.add_task(
                send_email, user.email, "Reset your Card Radar password", html, text
            )
            email_sent = True

        # Always also log to the admin queue so a human can handle edge cases
        req = PasswordResetRequest(
            id=str(uuid.uuid4()),
            username=username or (user.username if user else ""),
            email=email or (user.email if user else None),
            status="pending",
        )
        db.add(req)
        db.commit()

        # Don't reveal whether the account exists
        return {"status": "ok", "email_sent": email_sent}
    finally:
        db.close()


@app.post("/api/auth/reset-password")
def reset_password(body: dict):
    """Complete a password reset using a token from the reset email."""
    token = (body.get("token") or "").strip()
    new_password = body.get("password") or ""
    if not token:
        raise HTTPException(status_code=400, detail="Reset token is required")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.password_reset_token == token).first()
        if not user:
            raise HTTPException(status_code=400, detail="This reset link is invalid or has already been used")
        if not user.password_reset_expires or user.password_reset_expires < datetime.utcnow():
            raise HTTPException(status_code=400, detail="This reset link has expired. Request a new one.")

        user.password_hash = _hash_password(new_password)
        user.password_reset_token = None
        user.password_reset_expires = None
        db.commit()

        # Invalidate all active sessions for this user
        for tok in [t for t, uid in _active_sessions.items() if uid == user.id]:
            _active_sessions.pop(tok, None)

        return {"status": "ok"}
    finally:
        db.close()


@app.get("/reset-password", include_in_schema=False)
def reset_password_page():
    return FileResponse(os.path.join(STATIC_DIR, "reset-password.html"))


@app.get("/api/admin/password-resets")
def admin_list_resets(request: Request):
    require_admin(request)
    db = SessionLocal()
    try:
        reqs = db.query(PasswordResetRequest).order_by(PasswordResetRequest.created_at.desc()).all()
        return {
            "requests": [
                {
                    "id": r.id,
                    "username": r.username,
                    "email": r.email,
                    "status": r.status,
                    "created_at": (r.created_at.isoformat() + "Z") if r.created_at else None,
                }
                for r in reqs
            ]
        }
    finally:
        db.close()


@app.post("/api/admin/password-resets/{req_id}/resolve")
def admin_resolve_reset(request: Request, req_id: str):
    require_admin(request)
    db = SessionLocal()
    try:
        r = db.get(PasswordResetRequest, req_id)
        if not r:
            raise HTTPException(status_code=404, detail="Request not found")
        r.status = "resolved"
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()


@app.get("/api/auth/me")
def auth_me(request: Request):
    user = require_auth(request)
    db = SessionLocal()
    try:
        return user_to_dict(user, db)
    finally:
        db.close()


@app.patch("/api/auth/me")
def auth_update_me(request: Request, body: dict, background_tasks: BackgroundTasks):
    """Let an authenticated user update their own profile.

    Accepted fields: first_name, last_name, email, phone, current_password, new_password.
    Changing the email re-locks the account: email_verified is set to False
    and a new verification link is sent.
    Changing the password requires the current password.
    """
    current = require_auth(request)
    db = SessionLocal()
    try:
        user = db.get(User, current.id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Optional simple fields
        if "first_name" in body:
            fn = (body.get("first_name") or "").strip()
            if not fn:
                raise HTTPException(status_code=400, detail="First name cannot be empty")
            user.first_name = fn
        if "last_name" in body:
            ln = (body.get("last_name") or "").strip()
            if not ln:
                raise HTTPException(status_code=400, detail="Last name cannot be empty")
            user.last_name = ln
        if "phone" in body:
            user.phone = (body.get("phone") or "").strip() or None
        if "anonymize_cards" in body:
            user.anonymize_cards = bool(body["anonymize_cards"])

        # Email change → requires re-verification
        if "email" in body:
            new_email = (body.get("email") or "").strip()
            if not new_email:
                raise HTTPException(status_code=400, detail="Email cannot be empty")
            if "@" not in new_email or "." not in new_email.split("@")[-1]:
                raise HTTPException(status_code=400, detail="Please enter a valid email address")
            if new_email != (user.email or ""):
                # Make sure no one else has this address
                clash = db.query(User).filter(User.email == new_email, User.id != user.id).first()
                if clash:
                    raise HTTPException(status_code=400, detail="An account with that email already exists")
                user.email = new_email
                user.email_verified = False
                user.email_verify_token = secrets.token_urlsafe(32)
                user.email_verify_sent_at = datetime.utcnow()
                # Queue re-verification email
                verify_url = f"{get_base_url()}/api/auth/verify-email?token={user.email_verify_token}"
                html, text = verification_email_html(user.username, verify_url)
                background_tasks.add_task(
                    send_email, user.email, "Verify your Card Radar account", html, text
                )

        # Password change → requires current password
        new_password = body.get("new_password") or ""
        if new_password:
            current_password = body.get("current_password") or ""
            if not current_password:
                raise HTTPException(status_code=400, detail="Current password is required to set a new one")
            if not _verify_password(current_password, user.password_hash):
                raise HTTPException(status_code=400, detail="Current password is incorrect")
            if len(new_password) < 6:
                raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
            user.password_hash = _hash_password(new_password)

        db.commit()
        db.refresh(user)
        return {"status": "ok", "user": user_to_dict(user, db)}
    finally:
        db.close()


# ── Admin: User Management ──────────────────────────────────────────────────

@app.get("/api/admin/users")
def admin_list_users(request: Request):
    require_admin(request)
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.created_at.desc()).all()
        return {"users": [user_to_dict(u, db) for u in users]}
    finally:
        db.close()


@app.post("/api/admin/users")
def admin_create_user(request: Request, body: dict):
    require_admin(request)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    email = (body.get("email") or "").strip() or None
    first_name = (body.get("first_name") or "").strip() or None
    last_name = (body.get("last_name") or "").strip() or None
    phone = (body.get("phone") or "").strip() or None
    is_admin_flag = bool(body.get("is_admin", False))
    card_limit = body.get("card_limit", 5)
    tier = body.get("subscription_tier", "free")

    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    if tier not in ("free", "pro", "unlimited"):
        raise HTTPException(status_code=400, detail="Invalid tier")

    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == username).first():
            raise HTTPException(status_code=400, detail="Username already taken")
        user = User(
            id=str(uuid.uuid4()),
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            password_hash=_hash_password(password),
            is_admin=is_admin_flag,
            card_limit=int(card_limit) if not is_admin_flag else 999999,
            subscription_tier="unlimited" if is_admin_flag else tier,
            email_verified=True,  # admin-created accounts are trusted
        )
        db.add(user)
        db.commit()
        return user_to_dict(user, db)
    finally:
        db.close()


@app.get("/api/admin/users/export/csv")
def admin_export_users_csv(request: Request):
    """Admin: export all users as CSV.

    Sensitive fields (password hash, reset tokens, session tokens, any stored
    payment/credit-card data) are explicitly excluded. We do NOT store
    credit-card information anywhere in this system; if that ever changes,
    this endpoint must continue to omit it.
    """
    require_admin(request)
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.created_at.desc()).all()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "ID", "Username", "First Name", "Last Name", "Email", "Phone",
            "Email Verified", "Role", "Tier", "Card Limit", "Cards Used",
            "Subscription Expires", "Created At",
        ])
        for u in users:
            writer.writerow([
                u.id,
                u.username,
                u.first_name or "",
                u.last_name or "",
                u.email or "",
                u.phone or "",
                "Yes" if u.email_verified else "No",
                "admin" if u.is_admin else "user",
                u.subscription_tier or "free",
                u.card_limit or 0,
                _user_card_count(db, u.id),
                u.subscription_expires_at.isoformat() if u.subscription_expires_at else "",
                u.created_at.isoformat() if u.created_at else "",
            ])
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=users_export.csv"},
        )
    finally:
        db.close()


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(request: Request, user_id: str, body: dict):
    require_admin(request)
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if "card_limit" in body:
            try:
                user.card_limit = max(0, int(body["card_limit"]))
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="card_limit must be an integer")
        if "subscription_tier" in body:
            tier = body["subscription_tier"]
            if tier not in ("free", "pro", "unlimited"):
                raise HTTPException(status_code=400, detail="Invalid subscription tier")
            user.subscription_tier = tier
            # Auto-bump limits for paid tiers
            if tier == "pro" and user.card_limit < 100:
                user.card_limit = 100
            elif tier == "unlimited":
                user.card_limit = 999999
        if "is_admin" in body:
            user.is_admin = bool(body["is_admin"])
        for field in ("first_name", "last_name", "email", "phone"):
            if field in body:
                val = (body[field] or "").strip() or None
                setattr(user, field, val)
        if "email_verified" in body:
            user.email_verified = bool(body["email_verified"])
        db.commit()
        return user_to_dict(user, db)
    finally:
        db.close()


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(
    request: Request,
    user_id: str,
    delete_cards: bool = Query(default=False),
):
    admin = require_admin(request)
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        # Optionally delete all of the user's cards (and their files)
        if delete_cards:
            user_cards = db.query(Card).filter(Card.owner_id == user_id).all()
            for c in user_cards:
                if USE_CLOUDINARY:
                    try:
                        cloudinary.uploader.destroy(f"cards/{c.id}/front")
                        cloudinary.uploader.destroy(f"cards/{c.id}/back")
                    except Exception:
                        pass
                else:
                    card_dir = os.path.join(UPLOAD_DIR, c.id)
                    if os.path.exists(card_dir):
                        shutil.rmtree(card_dir, ignore_errors=True)
                db.delete(c)
        else:
            # Orphan: just clear ownership
            db.query(Card).filter(Card.owner_id == user_id).update({Card.owner_id: None})
        db.delete(user)
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()


@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(request: Request, user_id: str, body: dict):
    require_admin(request)
    new_password = body.get("password") or ""
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        user.password_hash = _hash_password(new_password)
        db.commit()
        # Invalidate any active sessions for this user
        for tok in [t for t, uid in _active_sessions.items() if uid == user_id]:
            _active_sessions.pop(tok, None)
        return {"status": "ok"}
    finally:
        db.close()


@app.post("/api/admin/cards/{card_id}/reassign")
def admin_reassign_card(request: Request, card_id: str, body: dict):
    """Transfer card ownership to another user."""
    require_admin(request)
    new_owner_id = body.get("owner_id")
    db = SessionLocal()
    try:
        card = db.get(Card, card_id)
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")
        if new_owner_id:
            new_owner = db.get(User, new_owner_id)
            if not new_owner:
                raise HTTPException(status_code=404, detail="New owner not found")
        card.owner_id = new_owner_id or None
        db.commit()
        return card_to_dict(card, db)
    finally:
        db.close()


@app.get("/api/admin/stats")
def admin_stats(request: Request):
    require_admin(request)
    db = SessionLocal()
    try:
        total_users = db.query(User).count()
        admin_users = db.query(User).filter(User.is_admin == True).count()
        paying_users = db.query(User).filter(User.subscription_tier.in_(("pro", "unlimited"))).count()
        total_cards = db.query(Card).count()
        complete_cards = db.query(Card).filter(Card.status == "complete").count()
        orphaned_cards = db.query(Card).filter(Card.owner_id.is_(None)).count()
        total_value = sum(
            (c.estimated_price or 0)
            for c in db.query(Card).filter(Card.status == "complete").all()
        )
        return {
            "total_users": total_users,
            "admin_users": admin_users,
            "paying_users": paying_users,
            "total_cards": total_cards,
            "complete_cards": complete_cards,
            "orphaned_cards": orphaned_cards,
            "total_value": round(total_value, 2),
        }
    finally:
        db.close()


@app.get("/api/admin/email-diagnostic")
def admin_email_diagnostic(request: Request):
    """Admin-only: returns the current SMTP env-var configuration so you can
    confirm Render actually loaded them. Never returns the password itself."""
    require_admin(request)
    return smtp_diagnostic()


@app.post("/api/admin/email-test")
def admin_email_test(request: Request, body: dict):
    """Admin-only: synchronously try to send a test email and return the result.
    Body: {"to": "you@example.com"}"""
    require_admin(request)
    to = (body.get("to") or "").strip()
    if not to:
        raise HTTPException(status_code=400, detail="Provide a 'to' address")
    html = "<p>This is a Card Radar SMTP test email. If you see this, sending works! 🎉</p>"
    text = "This is a Card Radar SMTP test email. If you see this, sending works!"
    ok = send_email(to, "Card Radar SMTP test", html, text)
    return {"ok": ok, "to": to, "diagnostic": smtp_diagnostic()}


@app.get("/api/admin/analytics")
def admin_analytics(request: Request, time_range: str = Query(default="30", alias="range")):
    """Return daily counts of new users and new cards for the given range.
    range values: '7', '30', '90', '365', 'all'."""
    require_admin(request)
    db = SessionLocal()
    try:
        from sqlalchemy import func, cast, Date
        today = datetime.utcnow().date()

        if time_range == "all":
            start_date = None
        else:
            days = int(time_range)
            start_date = today - timedelta(days=days - 1)

        def daily_series(model, date_col):
            q = db.query(
                cast(date_col, Date).label("day"),
                func.count().label("cnt"),
            )
            if start_date:
                q = q.filter(date_col >= datetime.combine(start_date, datetime.min.time()))
            q = q.group_by("day").order_by("day")
            rows = q.all()
            return {str(r.day): r.cnt for r in rows}

        user_data = daily_series(User, User.created_at)
        card_data = daily_series(Card, Card.created_at)

        # Fill in missing days
        if start_date:
            all_days = [(start_date + timedelta(days=i)) for i in range((today - start_date).days + 1)]
        else:
            # Use union of all dates present
            all_day_strs = sorted(set(list(user_data.keys()) + list(card_data.keys())))
            all_days = [datetime.strptime(d, "%Y-%m-%d").date() for d in all_day_strs] if all_day_strs else []

        users_series = [{"label": d.strftime("%m/%d"), "count": user_data.get(str(d), 0)} for d in all_days]
        cards_series = [{"label": d.strftime("%m/%d"), "count": card_data.get(str(d), 0)} for d in all_days]

        return {
            "users_series": users_series,
            "cards_series": cards_series,
        }
    finally:
        db.close()


# ── Subscription (stub) ─────────────────────────────────────────────────────

@app.post("/api/subscription/upgrade")
def subscription_upgrade(request: Request, body: dict):
    """Stub upgrade endpoint. Real implementation would integrate Stripe.
    For now this just sets the tier so admin can manually approve later."""
    user = require_auth(request)
    tier = body.get("tier", "pro")
    if tier not in ("pro", "unlimited"):
        raise HTTPException(status_code=400, detail="Invalid tier")
    # In a real flow this would create a Stripe checkout session and return the URL.
    return {
        "status": "pending",
        "message": "Subscription checkout is not yet enabled. Contact the admin to upgrade your account.",
        "requested_tier": tier,
    }


# ── Helper ───────────────────────────────────────────────────────────────────

_username_cache: dict[str, str] = {}


def _get_username(db, user_id: str | None) -> str | None:
    if not user_id:
        return None
    if user_id in _username_cache:
        return _username_cache[user_id]
    u = db.get(User, user_id)
    name = u.username if u else None
    if name:
        _username_cache[user_id] = name
    return name


def card_to_dict(card: Card, db=None) -> dict:
    # If paths are URLs (Cloudinary), use them directly; otherwise build local URLs
    front_url = card.front_image_path if card.front_image_path and card.front_image_path.startswith("http") else f"/uploads/{card.id}/front.jpg"
    back_url = card.back_image_path if card.back_image_path and card.back_image_path.startswith("http") else f"/uploads/{card.id}/back.jpg"
    owner_username = None
    if db is not None and card.owner_id:
        owner_username = _get_username(db, card.owner_id)
    return {
        "id": card.id,
        "owner_id": card.owner_id,
        "owner_username": owner_username,
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
        # Privacy & hints
        "is_public": bool(card.is_public),
        "set_hint": card.set_hint,
        # Meta
        "notes": card.notes,
    }


# ── Background Processing ────────────────────────────────────────────────────

def process_card(card_id: str):
    """Analyze card with Claude, then fetch eBay pricing. Runs in thread pool."""
    db = SessionLocal()
    try:
        card = db.get(Card, card_id)
        if not card:
            return

        # Step 1 — AI analysis
        card.status = "analyzing"
        db.commit()

        try:
            analysis = analyze_card(
                card.front_image_path, card.back_image_path,
                set_hint=card.set_hint,
            )
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
    request: Request,
    search: str = Query(default=""),
    status: str = Query(default=""),
    owner: str = Query(default=""),
    limit: int = Query(default=60, le=200),
    offset: int = Query(default=0),
):
    """List cards. The 'Card Collection' (all) view only returns public cards
    unless the viewer is an admin. The 'My Collection' (mine) view returns all
    of the user's own cards regardless of is_public."""
    db = SessionLocal()
    try:
        user = _current_user(request)
        q = db.query(Card)
        is_mine = False
        if owner == "mine":
            if not user:
                raise HTTPException(status_code=401, detail="Login required")
            q = q.filter(Card.owner_id == user.id)
            is_mine = True
        elif owner:
            q = q.filter(Card.owner_id == owner)
        else:
            # "Card Collection" — public cards only (admins see everything)
            if not (user and user.is_admin):
                q = q.filter(Card.is_public == True)  # noqa: E712
        if search:
            like = f"%{search}%"
            # Also search card_number and owner username
            from sqlalchemy import exists, select
            owner_sub = db.query(User.id).filter(User.username.ilike(like)).subquery()
            q = q.filter(
                or_(
                    Card.player_name.ilike(like),
                    Card.brand.ilike(like),
                    Card.set_name.ilike(like),
                    Card.team.ilike(like),
                    Card.parallel_name.ilike(like),
                    Card.card_number.ilike(like),
                    Card.owner_id.in_(select(owner_sub)),
                )
            )
        if status:
            q = q.filter(Card.status == status)

        total = q.count()
        cards = q.order_by(Card.created_at.desc()).offset(offset).limit(limit).all()

        # Build card dicts, anonymizing owner when the user has that preference
        card_dicts = []
        # Cache owner anonymize preference to avoid repeated lookups
        _anon_cache: dict[str, bool] = {}
        for c in cards:
            d = card_to_dict(c, db)
            # Anonymize: if viewing public collection (not "mine"), check
            # per-card override first, then the owner's profile setting.
            if not is_mine and c.owner_id and c.owner_id != (user.id if user else None):
                if c.owner_id not in _anon_cache:
                    owner_obj = db.get(User, c.owner_id)
                    _anon_cache[c.owner_id] = bool(owner_obj and owner_obj.anonymize_cards)
                if _anon_cache[c.owner_id]:
                    d["owner_username"] = "Anonymous"
            card_dicts.append(d)

        # Summary stats (scoped to query, not global)
        all_complete = db.query(Card).filter(Card.status == "complete")
        if is_mine and user:
            all_complete = all_complete.filter(Card.owner_id == user.id)
        elif not (user and user.is_admin) and not is_mine:
            all_complete = all_complete.filter(Card.is_public == True)  # noqa: E712
        complete_cards = all_complete.all()
        total_value = sum(c.estimated_price for c in complete_cards if c.estimated_price)
        stats = {
            "total_cards": total,
            "complete": len(complete_cards),
            "total_value": round(total_value, 2),
        }

        return {
            "cards": card_dicts,
            "total": total,
            "stats": stats,
        }
    finally:
        db.close()


@app.get("/api/cards/stats")
def cards_stats(request: Request):
    """Return stats for the logged-in user (their cards only), or global if not logged in."""
    user = _current_user(request)
    db = SessionLocal()
    try:
        q = db.query(Card)
        scope = "global"
        if user:
            q = q.filter(Card.owner_id == user.id)
            scope = "user"
        all_cards = q.all()
        complete = [c for c in all_cards if c.status == "complete"]
        total_value = sum((c.estimated_price or 0) for c in complete)
        return {
            "scope": scope,
            "total_cards": len(all_cards),
            "complete": len(complete),
            "total_value": round(total_value, 2),
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
        return card_to_dict(card, db)
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
    user = require_auth(request)
    if side not in ("front", "back"):
        raise HTTPException(status_code=400, detail="Side must be 'front' or 'back'")

    db = SessionLocal()
    try:
        card = db.get(Card, card_id)
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")
        if not user.is_admin and card.owner_id != user.id:
            raise HTTPException(status_code=403, detail="You can only edit your own cards")

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

        return card_to_dict(card, db)
    finally:
        db.close()


@app.post("/api/cards/upload")
async def upload_card(
    request: Request,
    background_tasks: BackgroundTasks,
    front_image: UploadFile = File(None),
    back_image: UploadFile = File(None),
    scan_mode: bool = Query(default=False),
    set_hint: str = Form(default=""),
    is_public: str = Form(default="false"),
):
    """Receive front and/or back images. At least one required. scan_mode applies edge detection + perspective correction."""
    user = require_auth(request)
    if not front_image and not back_image:
        raise HTTPException(status_code=400, detail="At least one image is required")

    # Enforce per-user card limit:
    #   Free: lifetime cap (card_limit, default 5)
    #   Pro:  100 per calendar month (resets on the 1st)
    #   Unlimited/Admin: no cap
    db_check = SessionLocal()
    try:
        tier = (user.subscription_tier or "free").lower()
        if not user.is_admin and tier != "unlimited":
            if tier == "pro":
                used = _user_monthly_card_count(db_check, user.id)
                limit = 100
                if used >= limit:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Monthly card limit reached ({used}/{limit} this month). Your limit resets on the 1st of next month.",
                    )
            else:
                # Free tier — lifetime cap
                used = _user_card_count(db_check, user.id)
                if used >= user.card_limit:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Card limit reached ({used}/{user.card_limit}). Upgrade to Pro for 100 cards per month.",
                    )
    finally:
        db_check.close()

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
        owner_id=user.id,
        set_hint=set_hint.strip() or None,
        is_public=(is_public.lower() in ("true", "1", "yes")),
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
    user = require_auth(request)
    allowed = {
        "notes", "condition", "estimated_price", "player_name", "year",
        "brand", "set_name", "team", "description", "subset", "insert_set", "product_code", "card_number",
        "sport", "is_rookie_card", "is_parallel", "parallel_name", "is_foil",
        "is_autograph", "is_relic", "relic_type", "is_numbered", "print_run",
        "serial_number", "has_alternate_jersey", "jersey_description",
        "is_short_print", "notable_features", "is_public",
    }
    db = SessionLocal()
    try:
        card = db.get(Card, card_id)
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")
        if not user.is_admin and card.owner_id != user.id:
            raise HTTPException(status_code=403, detail="You can only edit your own cards")
        for key, value in body.items():
            if key in allowed:
                setattr(card, key, value)
        db.commit()
        return card_to_dict(card, db)
    finally:
        db.close()


@app.post("/api/cards/{card_id}/reprice")
def reprice_card(request: Request, card_id: str, background_tasks: BackgroundTasks):
    """Trigger a fresh eBay pricing lookup."""
    user = require_auth(request)
    db = SessionLocal()
    try:
        card = db.get(Card, card_id)
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")
        if not user.is_admin and card.owner_id != user.id:
            raise HTTPException(status_code=403, detail="You can only reprice your own cards")
    finally:
        db.close()
    background_tasks.add_task(refresh_pricing, card_id)
    return {"status": "repricing"}


@app.delete("/api/cards/{card_id}")
def delete_card(request: Request, card_id: str):
    """Delete a card and its images."""
    user = require_auth(request)
    db = SessionLocal()
    try:
        card = db.get(Card, card_id)
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")
        if not user.is_admin and card.owner_id != user.id:
            raise HTTPException(status_code=403, detail="You can only delete your own cards")
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
def export_csv(request: Request):
    """Download the logged-in user's cards as a CSV spreadsheet.

    Access rules:
      • Must be logged in.
      • Free tier is blocked (upgrade required).
      • Pro / Unlimited / Admin can export — scoped to their own cards
        (admins still get only their own; to export everything they can
        use the admin console).
    """
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required to export CSV")
    tier = (user.subscription_tier or "free").lower()
    if not user.is_admin and tier not in ("pro", "unlimited"):
        raise HTTPException(
            status_code=403,
            detail="CSV export is a Pro feature. Please upgrade your plan.",
        )
    db = SessionLocal()
    try:
        cards = (
            db.query(Card)
            .filter(Card.owner_id == user.id)
            .order_by(Card.created_at.desc())
            .all()
        )
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
