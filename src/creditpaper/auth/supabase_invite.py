"""Send invitation email — Supabase Auth if configured, else SMTP fallback."""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def _smtp_configured() -> bool:
    return bool(os.getenv("SMTP_USERNAME") and os.getenv("SMTP_PASSWORD"))


def _supabase_configured() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))


def send_invite_email(
    email: str,
    role: str,
    token: str,
    tenant_name: str | None = None,
    inviter_email: str | None = None,
) -> bool:
    """Send invitation email. Tries Supabase Auth first, falls back to SMTP.

    Returns True if sent, False if skipped/failed (caller should not fail the request).
    """
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
    invite_url = f"{frontend_url}/set-password?token={token}"
    firm = tenant_name or "your firm"

    # Try Supabase Auth Admin invite (preferred — creates auth.users entry)
    if _supabase_configured():
        try:
            return _send_via_supabase(email, role, invite_url)
        except Exception:
            logger.exception("Supabase invite failed for %s, trying SMTP", email)

    # Fallback: SMTP
    if _smtp_configured():
        try:
            return _send_via_smtp(email, role, invite_url, firm, inviter_email)
        except Exception:
            logger.exception("SMTP invite failed for %s", email)
            return False

    logger.warning("No email service configured — invite for %s not sent (token=%s)", email, token)
    return False


def _send_via_supabase(email: str, role: str, invite_url: str) -> bool:
    import httpx

    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    # Supabase Auth Admin invite endpoint
    resp = httpx.post(
        f"{url}/auth/v1/admin/invite",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={
            "email": email,
            "data": {"role": role, "invite_url": invite_url},
        },
        timeout=15,
    )
    if resp.status_code in (200, 201):
        logger.info("Supabase invite sent to %s", email)
        return True
    logger.warning("Supabase invite failed for %s: %s %s", email, resp.status_code, resp.text)
    return False


def _send_via_smtp(
    email: str, role: str, invite_url: str, firm: str, inviter_email: str | None = None
) -> bool:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    from_addr = os.getenv("SMTP_FROM", username)

    role_label = {"tenant_admin": "Admin", "maker": "Maker", "checker": "Checker", "viewer": "Viewer"}.get(
        role, role
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"You have been invited to join {firm} on Creditpaper"
    msg["From"] = from_addr
    msg["To"] = email

    inviter = inviter_email or "A colleague"
    # Plain-text fallback
    text = f"""You're Invited!

{inviter} has invited you to join {firm} on Creditpaper.

Your assigned role: {role_label}

Accept your invitation:
{invite_url}

Or copy and paste this link:
{invite_url}

This invitation expires in 7 days.
If you weren't expecting this, you can safely ignore this email.
"""

    # HTML — matches Verify Your Email template: navy header, plum button, same spacing
    html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#F0F2F5;">
<div style="max-width:480px;margin:0 auto;background:#F0F2F5;padding:24px 16px;">
<div style="background:#853E69;border-radius:8px 8px 0 0;padding:18px 24px;text-align:center;">
<span style="color:#FFFFFF;font-family:Arial,Helvetica,sans-serif;font-size:16px;
font-weight:700;letter-spacing:1.2px;">CREDITPAPER</span>
</div>
<div style="background:#FFFFFF;border-radius:0 0 8px 8px;padding:32px 24px;text-align:center;">
<p style="margin:0 0 8px;color:#0F1926;font-family:Arial,Helvetica,sans-serif;
font-size:20px;font-weight:700;">You're Invited!</p>
<p style="margin:0 0 20px;color:#475569;font-family:Arial,Helvetica,sans-serif;
font-size:14px;line-height:20px;">
<a href="mailto:{inviter}" style="color:#853E69;text-decoration:none;font-weight:600;">{inviter}</a>
has invited you to join
<a href="#" style="color:#853E69;text-decoration:none;font-weight:600;">{firm}</a><br>on CreditPaper.
</p>
<div style="background:#F8F4F8;border:1px solid #E8D5E8;border-radius:8px;
padding:14px 16px;margin:0 0 20px;">
<p style="margin:0 0 4px;color:#64748B;font-family:Arial,Helvetica,sans-serif;
font-size:12px;">Your assigned role</p>
<p style="margin:0;color:#853E69;font-family:Arial,Helvetica,sans-serif;
font-size:15px;font-weight:700;">{role_label}</p>
</div>
<a href="{invite_url}" style="display:inline-block;background:#853E69;color:#FFFFFF;
font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:600;text-decoration:none;
padding:12px 28px;border-radius:6px;">Accept Invitation</a>
<p style="margin:20px 0 6px;color:#94A3B8;font-family:Arial,Helvetica,sans-serif;
font-size:12px;">Or copy and paste this link:</p>
<p style="margin:0;word-break:break-all;"><a href="{invite_url}"
style="color:#853E69;font-family:Arial,Helvetica,sans-serif;
font-size:12px;word-break:break-all;">{invite_url}</a></p>
</div>
<div style="background:#F8F9FA;border-radius:0 0 8px 8px;padding:16px 24px;
text-align:center;margin-top:1px;">
<p style="margin:0 0 4px;color:#94A3B8;font-family:Arial,Helvetica,sans-serif;
font-size:11px;">This invitation expires in 7 days.</p>
<p style="margin:0;color:#94A3B8;font-family:Arial,Helvetica,sans-serif;
font-size:11px;">If you weren't expecting this, you can safely ignore this email.</p>
</div>
</div>
</body></html>
"""

    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(username, password)
        server.sendmail(from_addr, [email], msg.as_string())

    logger.info("SMTP invite sent to %s", email)
    return True
