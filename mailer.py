# mailer.py
# =============================================================================
# One place that sends every PixelPerfect email (password reset, username
# reminder, contact form).
#
# Release H (Nov 2026): email moves from Gmail SMTP to Resend's HTTP API.
#   - With RESEND_API_KEY set, mail goes out through Resend from
#     EMAIL_FROM (default "PixelPerfect <noreply@pixelperfectapi.net>"), with
#     replies going to support@ (or the address the caller passes).
#   - Without it, the old Gmail SMTP path is used, so deploying this release
#     before Resend is configured changes nothing for customers.
#   - With neither configured, nothing is sent and MailNotConfigured is raised.
#
# Errors never include the API key or the SMTP password.
# =============================================================================
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests

log = logging.getLogger("pixelperfect")

RESEND_ENDPOINT = "https://api.resend.com/emails"
RESEND_TIMEOUT_S = 10
DEFAULT_FROM = "PixelPerfect <noreply@pixelperfectapi.net>"
DEFAULT_REPLY_TO = "support@pixelperfectapi.net"


class MailError(RuntimeError):
    """Sending failed. The message is safe to log (no credentials)."""


class MailNotConfigured(MailError):
    """Neither Resend nor SMTP is configured on this server."""


def provider() -> str:
    """Which path send_email() will use right now: resend | smtp | none."""
    if os.getenv("RESEND_API_KEY", "").strip():
        return "resend"
    if os.getenv("SMTP_USERNAME", "").strip() and os.getenv("SMTP_PASSWORD", "").strip():
        return "smtp"
    return "none"


def send_email(to: str, subject: str, text: str, html: str,
               reply_to: Optional[str] = None) -> str:
    """Send one email. Returns the provider used. Raises MailError on failure."""
    reply_to = reply_to or os.getenv("EMAIL_REPLY_TO", DEFAULT_REPLY_TO)
    which = provider()
    if which == "resend":
        _send_via_resend(to, subject, text, html, reply_to)
    elif which == "smtp":
        _send_via_smtp(to, subject, text, html, reply_to)
    else:
        raise MailNotConfigured("No email provider configured (set RESEND_API_KEY).")
    return which


def _send_via_resend(to, subject, text, html, reply_to) -> None:
    payload = {
        "from": os.getenv("EMAIL_FROM", DEFAULT_FROM),
        "to": [to],
        "subject": subject,
        "text": text,
        "html": html,
        "reply_to": reply_to,
    }
    try:
        r = requests.post(
            RESEND_ENDPOINT,
            json=payload,
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY'].strip()}"},
            timeout=RESEND_TIMEOUT_S,
        )
    except requests.RequestException as e:
        raise MailError(f"Resend unreachable: {type(e).__name__}") from None
    if r.status_code >= 300:
        # Resend's error body names the problem (e.g. unverified domain) and
        # never echoes the key, but keep it short.
        detail = (r.text or "")[:200]
        raise MailError(f"Resend rejected the email (HTTP {r.status_code}): {detail}")


def _send_via_smtp(to, subject, text, html, reply_to) -> None:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    from_name = os.getenv("CONTACT_FROM_NAME", "PixelPerfect")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{user}>"
    msg["To"] = to
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.login(user, password)
            server.sendmail(user, to, msg.as_string())
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"SMTP send failed: {type(e).__name__}") from None

# ===== END OF mailer.py ======================================================
