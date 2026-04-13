# backend/email_utils.py
# ============================================================================
# PixelPerfect Email Utilities
# Author: OneTechly
# Updated: April 2026
#
# ✅ FIX (Apr 2026): Replaced SendGrid with Gmail SMTP.
#
#   Root cause of forgot-password emails never arriving:
#     The original implementation used SendGrid (SENDGRID_API_KEY).
#     That env var was never set in Render's environment. So on every call:
#       1. SENDGRID_API_KEY check → falsy
#       2. log.info("[PWD-RESET] (no SENDGRID_API_KEY) ...") → silent log
#       3. return None  ← no email sent, no exception raised
#       4. Caller catches no error → returns {"ok": True}
#       5. Frontend shows "Check your email" → user gets nothing
#
#   Fix:
#     Use the same Gmail SMTP stack already configured and proven by the
#     /contact endpoint (SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD).
#     No new service, no new credentials, no new Render env vars required.
#     The function now raises on failure so main.py's except block can log it.
# ============================================================================

import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

log = logging.getLogger("pixelperfect")


def send_password_reset_email(to_email: str, reset_link: str) -> None:
    """
    Send a password reset email via Gmail SMTP.

    Uses the same env vars as the /contact endpoint:
      SMTP_HOST       (default: smtp.gmail.com)
      SMTP_PORT       (default: 587)
      SMTP_USERNAME   onetechly@gmail.com
      SMTP_PASSWORD   16-character Gmail app password

    Raises smtplib.SMTPException (or similar) on delivery failure so that
    main.py's /auth/forgot-password handler can log the error. The endpoint
    deliberately swallows the exception and returns {"ok": True} to prevent
    user enumeration — but the error IS logged for your visibility.
    """
    smtp_host     = os.getenv("SMTP_HOST",     "smtp.gmail.com")
    smtp_port     = int(os.getenv("SMTP_PORT", "587"))
    smtp_user     = os.getenv("SMTP_USERNAME", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    from_name     = os.getenv("CONTACT_FROM_NAME", "PixelPerfect")

    # ── Guard: SMTP not configured ────────────────────────────────────────
    # In local dev with no SMTP vars set, log the link so you can test the
    # reset flow without sending real email.
    if not smtp_user or not smtp_password:
        log.warning(
            "[PWD-RESET] SMTP not configured — reset email NOT sent to %s. "
            "Set SMTP_USERNAME + SMTP_PASSWORD in Render environment.",
            to_email,
        )
        log.info("[PWD-RESET] Dev reset link: %s", reset_link)
        return

    # ── Email content ─────────────────────────────────────────────────────
    subject = "Reset your PixelPerfect password"

    text_body = f"""Hello,

We received a request to reset your PixelPerfect account password.

Click the link below to set a new password (valid for 1 hour):
{reset_link}

If you didn't request this, you can safely ignore this email —
your password will not change.

— The PixelPerfect Team
pixelperfectapi.net
"""

    html_body = f"""
<html>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">

  <!-- Header -->
  <div style="background:#1e40af;padding:24px 20px;border-radius:8px 8px 0 0;">
    <h2 style="color:white;margin:0;font-size:20px;">&#128274; Reset Your Password</h2>
    <p style="color:#bfdbfe;margin:6px 0 0;font-size:14px;">PixelPerfect Screenshot API</p>
  </div>

  <!-- Body -->
  <div style="background:#f8fafc;border:1px solid #e2e8f0;border-top:none;
              padding:28px 24px;border-radius:0 0 8px 8px;">

    <p style="color:#374151;margin:0 0 16px;">Hello,</p>

    <p style="color:#374151;margin:0 0 24px;">
      We received a request to reset your <strong>PixelPerfect</strong>
      account password. Click the button below to set a new one:
    </p>

    <!-- CTA button -->
    <p style="text-align:center;margin:0 0 28px;">
      <a href="{reset_link}"
         style="display:inline-block;padding:14px 32px;
                background:#1e40af;color:#ffffff;
                border-radius:8px;text-decoration:none;
                font-weight:bold;font-size:16px;letter-spacing:0.3px;">
        Reset Password
      </a>
    </p>

    <p style="color:#6b7280;font-size:14px;margin:0 0 16px;">
      This link expires in <strong>1 hour</strong>.
      If you didn't request a password reset, you can safely ignore this email —
      your password will not change.
    </p>

    <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0;">

    <!-- Fallback link -->
    <p style="color:#94a3b8;font-size:12px;margin:0;">
      Button not working? Copy and paste this link into your browser:<br>
      <a href="{reset_link}" style="color:#3b82f6;word-break:break-all;">
        {reset_link}
      </a>
    </p>
  </div>

</body>
</html>
"""

    # ── Build MIME message ────────────────────────────────────────────────
    msg = MIMEMultipart("alternative")
    msg["Subject"]  = subject
    msg["From"]     = f"{from_name} <{smtp_user}>"
    msg["To"]       = to_email

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body,  "html"))

    # ── Send ─────────────────────────────────────────────────────────────
    # Any exception here propagates to the caller (main.py).
    # The /auth/forgot-password endpoint catches it, logs it, and still
    # returns {"ok": True} to prevent user enumeration.
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, to_email, msg.as_string())

    log.info("✅ Password reset email sent to %s", to_email)

# ===== END OF email_utils.py =================================================

# # =================================================================================
# # backend/email_utils.py
# import os
# import logging
# from typing import Optional

# log = logging.getLogger(__name__)

# SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")  # set in Render → Environment
# SENDGRID_FROM    = os.getenv("CONTACT_FROM", "no-reply@onetechly.com")  # verified sender
# SENDGRID_NAME    = os.getenv("CONTACT_FROM_NAME", "OneTechly")          # optional display name

# def send_password_reset_email(to_email: str, reset_link: str) -> Optional[str]:
#     """
#     Sends a password reset email via SendGrid if SENDGRID_API_KEY is set.
#     Returns the SendGrid message ID on success, or None if we fell back to logging.
#     Raises on hard SendGrid errors.
#     """
#     if not SENDGRID_API_KEY:
#         log.info("[PWD-RESET] (no SENDGRID_API_KEY) Send this link to the user: %s", reset_link)
#         return None

#     # Lazy import so the app still runs without sendgrid installed locally
#     try:
#         from sendgrid import SendGridAPIClient
#         from sendgrid.helpers.mail import Mail, From, To
#     except Exception as e:
#         log.warning("sendgrid package not installed; logging reset link. %s", e)
#         log.info("[PWD-RESET] %s", reset_link)
#         return None

#     subject = "Reset your OneTechly password"
#     html = f"""
#     <p>Hello,</p>
#     <p>We received a request to reset your password. Click the button below:</p>
#     <p><a href="{reset_link}"
#           style="display:inline-block;padding:10px 16px;background:#4f46e5;color:#fff;
#                  border-radius:8px;text-decoration:none">Reset password</a></p>
#     <p>If you didn’t request this, you can ignore this email.</p>
#     <p>— The OneTechly team</p>
#     """

#     msg = Mail(
#         from_email=From(SENDGRID_FROM, SENDGRID_NAME),
#         to_emails=To(to_email),
#         subject=subject,
#         html_content=html,
#     )

#     sg = SendGridAPIClient(SENDGRID_API_KEY)
#     resp = sg.send(msg)

#     if 200 <= resp.status_code < 300:
#         msg_id = resp.headers.get("X-Message-Id") or resp.headers.get("X-Message-ID")
#         log.info("Password reset email queued to %s (SendGrid %s)", to_email, msg_id or "OK")
#         return msg_id

#     # Non-2xx: raise so the caller can decide what to do
#     raise RuntimeError(f"SendGrid error {resp.status_code}: {resp.body}")
