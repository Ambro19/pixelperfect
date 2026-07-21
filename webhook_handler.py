# ============================================================================
# STRIPE WEBHOOK HANDLER (PRODUCTION READY)
# ============================================================================
# File: backend/webhook_handler.py
# Author: OneTechly
# Updated: July 2026
#
# ✅ FIX (Jul 2026 — Event set expanded to cover all payment success paths):
#   Both invoice.paid AND invoice.payment_succeeded are now listened to.
#   invoice.paid:               fires when an invoice reaches "paid" status —
#                               covers zero-amount invoices (free trials, 100%
#                               coupons) and manually-marked-paid invoices.
#   invoice.payment_succeeded:  fires when a card charge attempt succeeds —
#                               covers the first charge on a new subscription
#                               AND every recurring renewal charge.
#   Listening to both is correct and safe: the sync called by each is
#   idempotent (running it twice on the same user costs nothing and produces
#   the same result). Missing either event would leave a gap where a
#   successful payment doesn't trigger a tier update.
#   invoice.payment_failed is retained unchanged.
#
# ✅ FIX (Jul 2026 — customer.subscription.deleted now resets tier to free):
#   Previously, subscription.deleted only applied _set_stripe_fields() and
#   then called sync_user_subscription_from_stripe(), which is correct — BUT
#   if the sub was already cancelled in Stripe, the sync would find zero
#   active/trialing subs and (correctly) downgrade. Added explicit logging
#   so the cancel path is visible in Render logs.
#
# ✅ FIX (Jul 2026 — _ensure_stripe_key() called at handler entry):
#   Previously relied on module-level initialization. If STRIPE_SECRET_KEY
#   was set after module import (e.g. on Render cold start ordering), Stripe
#   calls inside the handler could fail. Now calls _ensure() on each event.
#
# Previous fixes (all retained):
# ✅ FIX (May 2026): checkout.session.completed user lookup hardened.
#   Customer-id collision guard prevents wrong-user attachment.
# ✅ FIX (May 2026): After webhook sync, immediately call
#   sync_user_subscription_from_stripe() with 5-layer tier resolution.
# ✅ Existing: signature verification and idempotency handled in main.py.
#
# Design:
#   main.py verifies Stripe signature + idempotency and stores the event in
#   request.state.verified_event. This handler processes the event and updates
#   the local user record by calling sync_user_subscription_from_stripe().
#
# Events handled (register exactly these 7 in your Stripe webhook endpoint):
#   checkout.session.completed
#   customer.subscription.created
#   customer.subscription.updated
#   customer.subscription.deleted
#   invoice.paid                    ← zero-amount / manually-paid invoices
#   invoice.payment_succeeded       ← card charge success (first + renewals)
#   invoice.payment_failed
# ============================================================================

import os
import logging
from datetime import datetime, timezone
from typing import Optional, Any, Dict

from fastapi import Request, HTTPException
from sqlalchemy.orm import Session

from models import User, SessionLocal
from subscription_sync import sync_user_subscription_from_stripe

logger = logging.getLogger("payment")

# Module-level Stripe init (best-effort; _ensure_stripe_key() hardens this)
stripe = None
try:
    import stripe as _stripe
    if os.getenv("STRIPE_SECRET_KEY"):
        _stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
        stripe = _stripe
except Exception:
    stripe = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ensure_stripe_key() -> bool:
    """Guarantee stripe.api_key is set. Mirrors subscription_sync._ensure_stripe_key()."""
    global stripe
    if not stripe:
        try:
            import stripe as _stripe
            stripe = _stripe
        except ImportError:
            return False
    key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if key:
        stripe.api_key = key
        return True
    return False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _new_db() -> Session:
    return SessionLocal()


def _find_by_customer_id(db: Session, customer_id: str) -> Optional[User]:
    return db.query(User).filter(User.stripe_customer_id == customer_id).first()


def _find_by_email(db: Session, email: str) -> Optional[User]:
    """Find user by email. Returns None if email is blank."""
    if not email:
        return None
    return db.query(User).filter(User.email == email.strip().lower()).first()


def _set_stripe_fields(
    user: User,
    *,
    status: Optional[str],
    period_end: Optional[datetime],
) -> None:
    now = _utcnow()
    if hasattr(user, "stripe_subscription_status") and status:
        user.stripe_subscription_status = status.lower()
    if hasattr(user, "subscription_status") and status:
        user.subscription_status = status.lower()
    if hasattr(user, "subscription_expires_at") and period_end:
        user.subscription_expires_at = period_end
    if hasattr(user, "subscription_ends_at") and period_end:
        user.subscription_ends_at = period_end
    if hasattr(user, "subscription_updated_at"):
        user.subscription_updated_at = now


def _extract_customer_id(obj: Dict[str, Any]) -> Optional[str]:
    cid = obj.get("customer")
    return str(cid) if cid else None


def _extract_email_from_checkout(obj: Dict[str, Any]) -> Optional[str]:
    cd = obj.get("customer_details") or {}
    email = cd.get("email") or obj.get("customer_email")
    return str(email).strip().lower() if email else None


def _extract_period_end(event_type: str, obj: Dict[str, Any]) -> Optional[datetime]:
    if event_type.startswith("customer.subscription."):
        return _to_dt(obj.get("current_period_end"))
    return None


def _extract_sub_status(event_type: str, obj: Dict[str, Any]) -> Optional[str]:
    if event_type.startswith("customer.subscription."):
        st = obj.get("status")
        return str(st) if st else None
    return None


# ── Events we handle ─────────────────────────────────────────────────────────
# Register EXACTLY these 7 event types on your Stripe webhook endpoint.
# ✅ FIX (Jul 2026): Both invoice.paid AND invoice.payment_succeeded included.
#   invoice.paid             — covers zero-amount + manually-paid invoices
#   invoice.payment_succeeded — covers card charge success (first + renewals)
#   Both are idempotent: the sync runs twice at most, producing the same result.

RELEVANT_EVENTS = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",                  # zero-amount / manually-paid invoices
    "invoice.payment_succeeded",     # card charge success — first + renewals
    "invoice.payment_failed",
}


# ── Main handler ─────────────────────────────────────────────────────────────

async def handle_stripe_webhook(request: Request):
    """
    Main webhook entry point.

    Expects main.py to have already:
      - verified the Stripe signature
      - performed idempotency check
      - stored the event in request.state.verified_event
    """
    # ✅ FIX: ensure key is set regardless of module-import ordering
    if not _ensure_stripe_key():
        raise HTTPException(status_code=503, detail="Stripe is not configured")

    event = getattr(request.state, "verified_event", None)
    if not event:
        raise HTTPException(status_code=400, detail="Missing verified webhook event")

    event_type = event.get("type")
    if not event_type:
        raise HTTPException(status_code=400, detail="Invalid event type")

    obj: Dict[str, Any] = (event.get("data") or {}).get("object") or {}
    logger.info("✅ Stripe webhook received: %s", event_type)

    if event_type not in RELEVANT_EVENTS:
        return {"status": "ok", "ignored": True, "event_type": event_type}

    db = _new_db()
    try:
        return await _process_event(db, event_type, obj)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("❌ Webhook error (%s): %s", event_type, e, exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Webhook processing failed")
    finally:
        db.close()


async def _process_event(db: Session, event_type: str, obj: Dict[str, Any]) -> dict:
    """Route the event to the appropriate handler."""

    customer_id = _extract_customer_id(obj)
    user: Optional[User] = None

    # ── Step 1: Find user by Stripe customer_id (most reliable) ──────────────
    if customer_id:
        user = _find_by_customer_id(db, customer_id)
        if user:
            logger.info(
                "Webhook %s: matched user %s (id=%s) via customer_id=%s",
                event_type, user.username, user.id, customer_id,
            )

    # ── Step 2: Checkout fallback — find by email, validate before writing ────
    if not user and event_type == "checkout.session.completed":
        email = _extract_email_from_checkout(obj)
        if email:
            candidate = _find_by_email(db, email)
            if candidate:
                # Guard: if another user already owns this customer_id, don't
                # attach it here — that would corrupt the wrong account.
                existing_owner = (
                    _find_by_customer_id(db, customer_id)
                    if customer_id else None
                )
                if existing_owner and existing_owner.id != candidate.id:
                    logger.warning(
                        "Webhook %s: customer_id=%s already belongs to user %s — "
                        "NOT re-attaching to email match user %s. "
                        "Manual intervention may be needed.",
                        event_type, customer_id,
                        existing_owner.id, candidate.id,
                    )
                else:
                    user = candidate
                    if customer_id and not (user.stripe_customer_id or "").strip():
                        logger.info(
                            "Webhook %s: attaching customer_id=%s to user %s (email match)",
                            event_type, customer_id, user.id,
                        )
                        user.stripe_customer_id = customer_id
                    elif customer_id and (user.stripe_customer_id or "") != customer_id:
                        # User found by email but has a DIFFERENT customer_id stored.
                        # Trust the new customer_id from Stripe over the stale DB value.
                        logger.warning(
                            "Webhook %s: user %s has customer_id=%s in DB but Stripe "
                            "reports customer_id=%s — updating to match Stripe.",
                            event_type, user.id,
                            user.stripe_customer_id, customer_id,
                        )
                        user.stripe_customer_id = customer_id
            else:
                logger.warning(
                    "Webhook %s: no user found for email=%s (customer_id=%s)",
                    event_type, email, customer_id,
                )

    # ── No user found ─────────────────────────────────────────────────────────
    if not user:
        logger.warning(
            "Webhook %s: could not map to any user "
            "(customer_id=%s). Event acknowledged but no DB update.",
            event_type, customer_id,
        )
        return {"status": "ok", "processed": event_type, "mapped_user": False}

    # ── Step 3: Apply local status/expiry from the webhook payload ────────────
    sub_status = _extract_sub_status(event_type, obj)
    period_end = _extract_period_end(event_type, obj)

    # ✅ FIX (Jul 2026): Log cancellation path explicitly for Render log visibility
    if event_type == "customer.subscription.deleted":
        logger.info(
            "Webhook %s: subscription cancelled for user %s — "
            "sync will downgrade tier to free.",
            event_type, user.id,
        )

    _set_stripe_fields(user, status=sub_status, period_end=period_end)
    db.commit()
    db.refresh(user)

    # ── Step 4: Full Stripe sync — authoritative tier resolution ──────────────
    # Calls subscription_sync.py 5-layer chain:
    #   lookup_key → nickname → price_id → sub metadata → product name
    # On subscription.deleted, sync finds zero active/trialing subs → downgrades to free.
    # On invoice.paid / invoice.payment_succeeded, sync confirms renewal → tier active.
    try:
        sync_user_subscription_from_stripe(user, db)
        db.refresh(user)
        logger.info(
            "✅ Webhook %s: user %s tier=%s after sync",
            event_type, user.id, user.subscription_tier,
        )
    except Exception as e:
        logger.warning(
            "Webhook %s: Stripe sync failed (non-fatal): %s", event_type, e
        )

    return {
        "status":      "ok",
        "processed":   event_type,
        "mapped_user": True,
        "user_id":     user.id,
        "tier":        user.subscription_tier,
    }

# ============================================================================
# END OF webhook_handler.py
# ============================================================================