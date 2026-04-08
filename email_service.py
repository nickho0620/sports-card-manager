"""
Lightweight email service for verification + password reset emails.

Configuration via environment variables:
    SMTP_HOST        (e.g. smtp.gmail.com)
    SMTP_PORT        (default 587)
    SMTP_USER        (SMTP username)
    SMTP_PASS        (SMTP password or app password)
    SMTP_FROM        (From address, defaults to SMTP_USER)
    SMTP_FROM_NAME   (default "Sports Card Manager")
    APP_BASE_URL     (e.g. https://sports-cards.onrender.com)

If SMTP is not configured, the "email" is printed to the console so local
development still works end-to-end (click the logged link to verify/reset).
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


def _smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS"))


def get_base_url() -> str:
    return os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")


def send_email(to: str, subject: str, html_body: str, text_body: str | None = None) -> bool:
    """Send an HTML email. Returns True on success (or dev-log), False on SMTP error."""
    if not to:
        print("[email] skip: no recipient")
        return False

    if not _smtp_configured():
        # Dev fallback — just log it.
        print("\n" + "=" * 70)
        print(f"[email:DEV] To: {to}")
        print(f"[email:DEV] Subject: {subject}")
        print("-" * 70)
        print(text_body or html_body)
        print("=" * 70 + "\n")
        return True

    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")
    from_addr = os.getenv("SMTP_FROM", user)
    from_name = os.getenv("SMTP_FROM_NAME", "Sports Card Manager")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_addr}>"
    msg["To"] = to
    msg.set_content(text_body or "This email requires an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")

    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.ehlo()
                s.starttls(context=context)
                s.ehlo()
                s.login(user, password)
                s.send_message(msg)
        print(f"[email] sent '{subject}' to {to}")
        return True
    except Exception as e:
        print(f"[email] ERROR sending to {to}: {e}")
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
      <h1>⚾ Verify your email</h1>
    </div>
    <div class="content">
      <p>Hi <strong>{username}</strong>,</p>
      <p>Thanks for signing up for Sports Card Manager! Please confirm your email address by clicking the button below:</p>
      <p style="text-align:center;"><a href="{verify_url}" class="btn">Verify My Email</a></p>
      <p class="muted">Or paste this link into your browser:<br><code>{verify_url}</code></p>
      <p class="muted">If you didn't create this account, you can safely ignore this email.</p>
    </div>
    <div class="footer">Sports Card Manager · Your collection, organized.</div>
  </div>
</body></html>"""
    text = (
        f"Hi {username},\n\n"
        f"Please verify your email for Sports Card Manager by visiting:\n{verify_url}\n\n"
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
      <p>We received a request to reset your Sports Card Manager password. Click the button below to choose a new one. This link expires in <strong>1 hour</strong>.</p>
      <p style="text-align:center;"><a href="{reset_url}" class="btn">Reset Password</a></p>
      <p class="muted">Or paste this link into your browser:<br><code>{reset_url}</code></p>
      <p class="muted">If you didn't request a password reset, you can safely ignore this email — your password won't be changed.</p>
    </div>
    <div class="footer">Sports Card Manager · Your collection, organized.</div>
  </div>
</body></html>"""
    text = (
        f"Hi {username},\n\n"
        f"Reset your Sports Card Manager password here (expires in 1 hour):\n{reset_url}\n\n"
        f"If you didn't request this, you can ignore this email."
    )
    return html, text
