# ============================================================================
# SUBSCRIPTION SYNC - STRIPE INTEGRATION (PRODUCTION READY)
# ============================================================================
# File: backend/subscription_sync.py
# Author: OneTechly
# Updated: May 2026 v2
# ============================================================================
# ✅ FIX (v2 - May 2026): Email fallback when stripe_customer_id is None
#    Root cause of "dashboard stays FREE after Stripe checkout" bug:
#      sync_user_subscription_from_stripe() returned early when
#      stripe_customer_id was None, even after a successful payment.
#      This happened because:
#        1. We cleared stripe_customer_id on test accounts (Stripe reset fix)
#        2. New accounts may not have customer_id stored yet at sync time
#      Fix: Search Stripe by email when customer_id is missing. If found,
#      store it back to DB and proceed with full tier sync.
#
# ✅ FIX (v1 - May 2026): Bulletproof tier detection — 5-layer fallback chain
#    Root cause: lookup_key read via dict .get() on StripeObject returned ""
#    Fix: attribute access (price.lookup_key) + 4 additional fallback paths
#
# ✅ FIX (v1 - May 2026): Stripe expand max depth (4 levels only)
#    Removed data.items.data.price.product (5 levels — Stripe rejects this)
#
# ✅ Existing: timezone-aware datetime comparisons via datetime_fix utilities
# ============================================================================

import logging
from typing import Optional
import os

from datetime_fix import make_aware, utc_now, is_expired

logger = logging.getLogger("pixelperfect")

try:
    import stripe
    STRIPE_AVAILABLE = bool(os.getenv("STRIPE_SECRET_KEY"))
except ImportError:
    stripe = None
    STRIPE_AVAILABLE = False


_TIER_KEYWORDS = [
    ("premium",  "premium"),
    ("business", "business"),
    ("pro",      "pro"),
]

def _keyword_to_tier(s: str) -> Optional[str]:
    s_lower = (s or "").lower()
    for keyword, tier in _TIER_KEYWORDS:
        if keyword in s_lower:
            return tier
    return None


def _resolve_tier_from_subscription(sub, user_id: int) -> str:
    """
    5-layer tier resolution from a Stripe subscription object.
    Returns: "pro" | "business" | "premium" | "free"
    """
    price = None
    try:
        items_data = sub.items.data
        if items_data:
            price = items_data[0].price
    except Exception as e:
        logger.warning("user %s: could not extract price: %s", user_id, e)

    # Path 1 — lookup_key (attribute access, not dict .get)
    if price:
        try:
            lk = price.lookup_key or ""
            if lk:
                tier = _keyword_to_tier(lk)
                if tier:
                    logger.info("user %s: tier='%s' via lookup_key='%s'", user_id, tier, lk)
                    return tier
                logger.warning(
                    "user %s: lookup_key='%s' found but no keyword matched — "
                    "check STRIPE_*_LOOKUP_KEY env vars match Stripe dashboard",
                    user_id, lk,
                )
        except Exception as e:
            logger.debug("user %s: lookup_key read error: %s", user_id, e)

    # Path 2 — price nickname
    if price:
        try:
            nick = price.nickname or ""
            if nick:
                tier = _keyword_to_tier(nick)
                if tier:
                    logger.info("user %s: tier='%s' via nickname='%s'", user_id, tier, nick)
                    return tier
        except Exception as e:
            logger.debug("user %s: nickname read error: %s", user_id, e)

    # Path 3 — price.id substring
    if price:
        try:
            pid = price.id or ""
            tier = _keyword_to_tier(pid)
            if tier:
                logger.info("user %s: tier='%s' via price.id='%s'", user_id, tier, pid)
                return tier
        except Exception as e:
            logger.debug("user %s: price.id read error: %s", user_id, e)

    # Path 4 — subscription metadata
    try:
        meta_tier = (sub.metadata or {}).get("tier", "").lower().strip()
        if meta_tier in ("pro", "business", "premium"):
            logger.info("user %s: tier='%s' via subscription metadata", user_id, meta_tier)
            return meta_tier
    except Exception as e:
        logger.debug("user %s: subscription metadata read error: %s", user_id, e)

    logger.warning(
        "user %s: could not resolve tier from any path — defaulting to free. "
        "Ensure Stripe price has lookup_key with pro/business/premium, "
        "or subscription metadata has tier=pro/business/premium.",
        user_id,
    )
    return "free"


def _find_stripe_customer_by_email(email: str) -> Optional[str]:
    """Search Stripe for a customer by email. Returns customer_id or None."""
    if not email or not stripe:
        return None
    try:
        customers = stripe.Customer.list(email=email.strip().lower(), limit=1)
        if customers.data:
            cid = customers.data[0].id
            logger.info("Found Stripe customer by email '%s': %s", email, cid)
            return cid
        logger.debug("No Stripe customer found for email '%s'", email)
        return None
    except Exception as e:
        logger.warning("Stripe customer email search failed: %s", e)
        return None


def sync_user_subscription_from_stripe(user, db) -> None:
    """
    Sync user subscription tier from Stripe.

    v2 FIX: If stripe_customer_id is None, search Stripe by email as fallback.
    This fixes "dashboard stays FREE after checkout" when customer_id is not
    stored in the DB at the time of sync.
    """
    if not STRIPE_AVAILABLE or not stripe:
        logger.debug("Stripe not available, skipping sync")
        return

    stripe_customer_id = getattr(user, "stripe_customer_id", None)

    # ── v2 FIX: Email fallback ────────────────────────────────────────────────
    if not stripe_customer_id:
        email = getattr(user, "email", None)
        logger.info(
            "user %s: no stripe_customer_id — searching Stripe by email '%s'",
            user.id, email,
        )
        stripe_customer_id = _find_stripe_customer_by_email(email)

        if stripe_customer_id:
            user.stripe_customer_id = stripe_customer_id
            try:
                db.commit()
                logger.info(
                    "user %s: stored stripe_customer_id=%s (email lookup)",
                    user.id, stripe_customer_id,
                )
            except Exception as e:
                logger.warning("user %s: could not persist customer_id: %s", user.id, e)
                db.rollback()
        else:
            logger.info("user %s: no Stripe customer found — skipping sync", user.id)
            return

    # ── Fetch active subscription ─────────────────────────────────────────────
    try:
        subscriptions = stripe.Subscription.list(
            customer=stripe_customer_id,
            status="active",
            limit=1,
            expand=["data.items.data.price"],   # 4 levels — Stripe maximum
        )

        if not subscriptions.data:
            current = (user.subscription_tier or "free").lower()
            if current != "free":
                logger.info(
                    "user %s: no active subscription for %s — downgrading to free",
                    user.id, stripe_customer_id,
                )
                user.subscription_tier = "free"
                _set_status(user, "inactive")
                db.commit()
            return

        sub = subscriptions.data[0]
        tier = _resolve_tier_from_subscription(sub, user.id)
        old_tier = user.subscription_tier or "free"
        user.subscription_tier = tier
        _set_status(user, sub.status)

        if hasattr(user, "subscription_updated_at"):
            user.subscription_updated_at = utc_now()

        period_end = getattr(sub, "current_period_end", None)
        if period_end:
            from datetime import datetime, timezone
            expires_dt = datetime.fromtimestamp(int(period_end), tz=timezone.utc)
            if hasattr(user, "subscription_expires_at"):
                user.subscription_expires_at = expires_dt
            if hasattr(user, "subscription_ends_at"):
                user.subscription_ends_at = expires_dt

        db.commit()

        if old_tier != tier:
            logger.info("✅ user %s synced: %s → %s", user.id, old_tier, tier)
        else:
            logger.info("✅ user %s confirmed: %s", user.id, tier)

    except Exception as e:
        logger.error("❌ Sync failed for user %s: %s", user.id, e)
        import traceback
        logger.error(traceback.format_exc())


def _set_status(user, status: str) -> None:
    if hasattr(user, "stripe_subscription_status"):
        user.stripe_subscription_status = status
    if hasattr(user, "subscription_status"):
        user.subscription_status = status


def _apply_local_overdue_downgrade_if_possible(user, db) -> None:
    """Downgrade expired subscriptions using timezone-aware comparisons."""
    try:
        expires_at = (
            getattr(user, "subscription_expires_at", None) or
            getattr(user, "subscription_ends_at", None)
        )
        if not expires_at:
            return

        expires_at_aware = make_aware(expires_at)

        if is_expired(expires_at_aware):
            current_tier = (getattr(user, "subscription_tier", "free") or "free").lower()
            if current_tier in ("pro", "business", "premium"):
                logger.info(
                    "user %s: expired on %s — downgrading from %s to free",
                    user.id, expires_at_aware, current_tier,
                )
                user.subscription_tier = "free"
                _set_status(user, "expired")
                user.usage_screenshots    = 0
                user.usage_batch_requests = 0
                user.usage_api_calls      = 0
                if hasattr(user, "usage_reset_at"):
                    user.usage_reset_at = utc_now()
                db.commit()
                db.refresh(user)
    except Exception as e:
        logger.error("❌ Downgrade check failed for user %s: %s", user.id, e)


def debug_user_subscription(user_id: int, db) -> dict:
    """Debug helper — returns all subscription fields + live Stripe data."""
    from models import User

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"error": "User not found"}

    result = {
        "user_id":            user.id,
        "email":              user.email,
        "subscription_tier":  user.subscription_tier,
        "stripe_customer_id": getattr(user, "stripe_customer_id", None),
    }

    for field in ("stripe_subscription_status", "subscription_status",
                  "usage_screenshots", "usage_batch_requests", "usage_api_calls"):
        if hasattr(user, field):
            result[field] = getattr(user, field)

    customer_id = result.get("stripe_customer_id")
    if not customer_id:
        customer_id = _find_stripe_customer_by_email(user.email)
        if customer_id:
            result["stripe_customer_id_from_email"] = customer_id

    if STRIPE_AVAILABLE and stripe and customer_id:
        try:
            subs = stripe.Subscription.list(
                customer=customer_id, status="active", limit=1,
                expand=["data.items.data.price"],
            )
            if subs.data:
                live_tier = _resolve_tier_from_subscription(subs.data[0], user_id)
                result["stripe_live_tier"]  = live_tier
                result["stripe_sub_status"] = subs.data[0].status
            else:
                result["stripe_live_tier"] = "free (no active subscription)"
        except Exception as e:
            result["stripe_debug_error"] = str(e)

    return result

# ============================================================================
# END OF subscription_sync.py
# ============================================================================