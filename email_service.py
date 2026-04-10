"""
Lightweight email service for verification + password reset emails.

Configuration via environment variables:
    SMTP_HOST        (e.g. smtp.gmail.com)
    SMTP_PORT        (default 587)
    SMTP_USER        (SMTP username)
    SMTP_PASS        (SMTP password or app password)
    SMTP_FROM        (From address, defaults to SMTP_USER)
    SMTP_FROM_NAME   (default "CardMint")
    APP_BASE_URL     (e.g. https://sports-cards.onrender.com)

If SMTP is not configured, the "email" is printed to the console so local
development still works end-to-end (click the logged link to verify/reset).
"""
from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

import requests


def _log(msg: str) -> None:
    """Print + force flush so Render logs pick it up immediately."""
    print(msg, flush=True)
    try:
        sys.stdout.flush()
    except Exception:
        pass


def _smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS"))


def _use_resend_http() -> bool:
    """Return True when we should send via Resend's HTTPS API instead of SMTP.

    Many cloud hosts (Render free tier, Vercel, Fly.io) block outbound SMTP
    on ports 25 / 587. Resend's HTTPS API goes over 443 which is never
    blocked, so we prefer it whenever the API key is a Resend key.
    """
    host = (os.getenv("SMTP_HOST") or "").lower()
    pwd = os.getenv("SMTP_PASS") or ""
    # RESEND_API_KEY overrides everything and always takes the HTTPS path
    if os.getenv("RESEND_API_KEY"):
        return True
    return "resend" in host and pwd.startswith("re_")


def _resend_api_key() -> str:
    return os.getenv("RESEND_API_KEY") or os.getenv("SMTP_PASS") or ""


def _send_via_resend_http(to: str, subject: str, html_body: str, text_body: str | None) -> bool:
    """Send an email through the Resend HTTPS API (port 443, never blocked)."""
    api_key = _resend_api_key()
    from_addr = os.getenv("SMTP_FROM") or "onboarding@resend.dev"
    from_name = os.getenv("SMTP_FROM_NAME") or "CardMint"
    from_header = f"{from_name} <{from_addr}>"

    _log(f"[email] HTTPS → POST https://api.resend.com/emails to={to} from={from_header!r}")
    payload = {
        "from": from_header,
        "to": [to],
        "subject": subject,
        "html": html_body,
    }
    if text_body:
        payload["text"] = text_body

    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
    except requests.RequestException as e:
        _log(f"[email] ❌ HTTPS request failed: {type(e).__name__}: {e}")
        return False

    if 200 <= r.status_code < 300:
        _log(f"[email] ✅ SENT via Resend HTTPS to {to} (status={r.status_code} body={r.text[:200]})")
        return True

    # Resend returns structured JSON errors like
    # {"statusCode":422,"name":"validation_error","message":"..."}
    try:
        err = r.json()
    except Exception:
        err = {"raw": r.text[:500]}
    _log(f"[email] ❌ Resend HTTPS error status={r.status_code} body={err}")
    return False


def get_base_url() -> str:
    return os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")


def smtp_diagnostic() -> dict:
    """Return a dict describing the current email configuration without leaking secrets."""
    pwd = os.getenv("SMTP_PASS") or ""
    return {
        "transport": "resend-https" if _use_resend_http() else ("smtp" if _smtp_configured() else "dev-logger"),
        "configured": _smtp_configured() or bool(os.getenv("RESEND_API_KEY")),
        "SMTP_HOST": os.getenv("SMTP_HOST") or "(missing)",
        "SMTP_PORT": os.getenv("SMTP_PORT") or "(default 587)",
        "SMTP_USER": os.getenv("SMTP_USER") or "(missing)",
        "SMTP_PASS_set": bool(pwd),
        "SMTP_PASS_len": len(pwd),
        "SMTP_PASS_prefix": (pwd[:3] + "…") if pwd else "(missing)",
        "SMTP_FROM": os.getenv("SMTP_FROM") or "(default to SMTP_USER)",
        "SMTP_FROM_NAME": os.getenv("SMTP_FROM_NAME") or "(default CardMint)",
        "APP_BASE_URL": os.getenv("APP_BASE_URL") or "(default http://localhost:8000)",
    }


def send_email(to: str, subject: str, html_body: str, text_body: str | None = None) -> bool:
    """Send an HTML email. Returns True on success (or dev-log), False on error.

    Prefers Resend's HTTPS API when available (port 443, never blocked by
    cloud hosts). Falls back to raw SMTP for generic providers.
    """
    _log(f"[email] >>> send_email called to={to!r} subject={subject!r}")
    if not to:
        _log("[email] SKIP: no recipient")
        return False

    # Preferred path: Resend HTTPS API (bypasses any outbound-SMTP blocking)
    if _use_resend_http():
        return _send_via_resend_http(to, subject, html_body, text_body)

    if not _smtp_configured():
        _log("=" * 70)
        _log("[email:DEV] SMTP NOT CONFIGURED — printing email instead of sending")
        _log(f"[email:DEV] Diagnostic: {smtp_diagnostic()}")
        _log(f"[email:DEV] To: {to}")
        _log(f"[email:DEV] Subject: {subject}")
        _log("-" * 70)
        _log(text_body or html_body)
        _log("=" * 70)
        return True

    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")
    from_addr = os.getenv("SMTP_FROM", user)
    from_name = os.getenv("SMTP_FROM_NAME", "CardMint")

    _log(f"[email] Attempting to={to} via {host}:{port} user={user!r} from={from_addr!r} pwd_len={len(password or '')}")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_addr}>"
    msg["To"] = to
    msg.set_content(text_body or "This email requires an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")

    try:
        context = ssl.create_default_context()
        if port == 465:
            _log(f"[email] opening SMTP_SSL connection to {host}:{port}")
            with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as s:
                _log("[email] connected, authenticating…")
                s.login(user, password)
                _log("[email] auth ok, sending…")
                s.send_message(msg)
        else:
            _log(f"[email] opening SMTP connection to {host}:{port}")
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                _log("[email] starting TLS…")
                s.starttls(context=context)
                s.ehlo()
                _log("[email] TLS ok, authenticating…")
                s.login(user, password)
                _log("[email] auth ok, sending…")
                s.send_message(msg)
        _log(f"[email] ✅ SENT '{subject}' to {to}")
        return True
    except smtplib.SMTPAuthenticationError as e:
        _log(f"[email] ❌ AUTH FAILED to {to}: {e.smtp_code} {e.smtp_error!r}")
        _log("[email]    SMTP_USER must be exactly 'resend' for Resend; SMTP_PASS must be your re_... API key.")
        return False
    except smtplib.SMTPRecipientsRefused as e:
        _log(f"[email] ❌ RECIPIENT REFUSED to {to}: {e.recipients}")
        _log("[email]    Resend's test domain (onboarding@resend.dev) only allows sending to your own verified address.")
        return False
    except smtplib.SMTPException as e:
        _log(f"[email] ❌ SMTP ERROR to {to}: {type(e).__name__}: {e}")
        return False
    except Exception as e:
        _log(f"[email] ❌ UNEXPECTED ERROR to {to}: {type(e).__name__}: {e}")
        return False


# ── Templates ───────────────────────────────────────────────────────────────

_BASE_STYLE = """
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#f1f5f9; margin:0; padding:32px 16px; color:#0f172a; }
  .wrap { max-width:560px; margin:0 auto; background:#fff; border-radius:16px; overflow:hidden; box-shadow:0 4px 24px rgba(0,0,0,.08); }
  .header { background:linear-gradient(135deg,#1e3a8a,#3b82f6); color:#fff; padding:28px 32px; }
  .header h1 { margin:0; font-size:22px; }
  .content { padding:32px; }
  .content p { line-height:1.6; color:#334155; font-size:15px; }
  .btn { display:inline-block; background:#3b82f6; color:#fff !important; text-decoration:none; padding:14px 28px; border-radius:10px; font-weight:700; margin:20px 0; }
  .muted { color:#64748b; font-size:13px; }
  .footer { background:#f8fafc; padding:20px 32px; text-align:center; color:#94a3b8; font-size:12px; }
  code { background:#f1f5f9; padding:2px 6px; border-radius:4px; font-size:13px; }
"""


def verification_email_html(username: str, verify_url: str) -> tuple[str, str]:
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_BASE_STYLE}</style></head>
<body>
  <div class="wrap">
    <div class="header">
      <h1>💰 Verify your email</h1>
    </div>
    <div class="content">
      <p>Hi <strong>{username}</strong>,</p>
      <p>Thanks for signing up for CardMint! Please confirm your email address by clicking the button below:</p>
      <p style="text-align:center;"><a href="{verify_url}" class="btn">Verify My Email</a></p>
      <p class="muted">Or paste this link into your browser:<br><code>{verify_url}</code></p>
      <p class="muted">If you didn't create this account, you can safely ignore this email.</p>
    </div>
    <div class="footer">CardMint · Your collection, organized.</div>
  </div>
</body></html>"""
    text = (
        f"Hi {username},\n\n"
        f"Please verify your email for CardMint by visiting:\n{verify_url}\n\n"
        f"If you didn't create this account, you can ignore this email."
    )
    return html, text


def password_reset_email_html(username: str, reset_url: str) -> tuple[str, str]:
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_BASE_STYLE}</style></head>
<body>
  <div class="wrap">
    <div class="header">
      <h1>🔐 Reset your password</h1>
    </div>
    <div class="content">
      <p>Hi <strong>{username}</strong>,</p>
      <p>We received a request to reset your CardMint password. Click the button below to choose a new one. This link expires in <strong>1 hour</strong>.</p>
      <p style="text-align:center;"><a href="{reset_url}" class="btn">Reset Password</a></p>
      <p class="muted">Or paste this link into your browser:<br><code>{reset_url}</code></p>
      <p class="muted">If you didn't request a password reset, you can safely ignore this email — your password won't be changed.</p>
    </div>
    <div class="footer">CardMint · Your collection, organized.</div>
  </div>
</body></html>"""
    text = (
        f"Hi {username},\n\n"
        f"Reset your CardMint password here (expires in 1 hour):\n{reset_url}\n\n"
        f"If you didn't request this, you can ignore this email."
    )
    return html, text
