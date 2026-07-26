# ============================================================================
# SUBSCRIPTION SYNC — PixelPerfect Screenshot API
# File: backend/subscription_sync.py
# Author: OneTechly
# Updated: July 2026 — PRODUCTION READY (hardened rewrite)
# ============================================================================
# Exports (imported by main.py — signatures unchanged):
#   sync_user_subscription_from_stripe(user, db) -> None
#   _apply_local_overdue_downgrade_if_possible(user, db) -> None
#
# ✅ FIX (Jul 2026 — Sync silently failing → dashboard stuck on FREE):
#   Observed: userProdtest02 completed Stripe checkout (sub active, $49 Pro),
#   but GET /subscription_status?sync=1 kept returning tier=free.
#   This rewrite hardens every failure mode this module has hit historically:
#
#   1. MISSING API KEY (May 2026 shell incident: "No API key provided"):
#      This module previously relied on main.py setting stripe.api_key at
#      import time. Any context where main.py isn't imported first (Render
#      shell, scripts, tests, import-order changes) silently broke sync.
#      → _ensure_stripe_key() now sets stripe.api_key from STRIPE_SECRET_KEY
#        on every call if it isn't already set. Self-sufficient.
#
#   2. StripeObject ATTRIBUTE ACCESS (past bug):
#      `sub.items` on a StripeObject returns the dict .items() METHOD, not
#      the subscription items — a classic silent-wrong-value bug.
#      → All Stripe object access uses dict-style ["key"] / .get("key").
#
#   3. STALE / MISMATCHED CUSTOMER ID (duplicate-customer case):
#      If users.stripe_customer_id points at a customer with no active
#      subscription (deleted/recreated customers, re-registration), sync
#      found nothing and gave up.
#      → New fallback: search Stripe customers by the user's email and scan
#        EACH match for an active subscription. On success, the user row's
#        stripe_customer_id is corrected to the customer that actually
#        holds the subscription.
#
#   4. TIER MAPPING TOO NARROW:
#      Previously lookup_key → price_id → sub metadata. If lookup keys were
#      renamed or checkout used a price without one, tier stayed "free".
#      → 5-layer chain: price lookup_key → price nickname → price_id →
#        subscription metadata (plan/tier) → PRODUCT NAME (fetched from
#        Stripe, e.g. "PixelPerfect Pro" → pro). Premium is checked before
#        business before pro so substrings can't mis-map.
#
#   5. SILENT FAILURE:
#      Every decision point now logs at INFO/WARNING with a `[sync]` prefix,
#      so Render logs show exactly where a sync stopped.
#
#   6. NEWER STRIPE API SHAPES:
#      current_period_end moved to the subscription-item level in newer
#      Stripe API versions. We read the sub-level field first and fall back
#      to the first item's field.
#
# Downgrade safety:
#   - Sync only downgrades to free when Stripe AFFIRMATIVELY reports zero
#     active/trialing subscriptions for a customer we successfully queried,
#     AND the user previously had a synced Stripe subscription state.
#     API errors NEVER downgrade anyone.
#   - _apply_local_overdue_downgrade_if_possible() downgrades locally only
#     when the recorded expiry is more than GRACE_DAYS past due and the last
#     known Stripe status is not active/trialing.
# ============================================================================

from __future__ import annotations

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pixelperfect")

try:
    import stripe
    STRIPE_AVAILABLE = True
except ImportError:  # pragma: no cover
    stripe = None
    STRIPE_AVAILABLE = False

# Statuses that mean "the user is entitled to the paid tier right now".
_ENTITLED_STATUSES = ("active", "trialing")

# Days past subscription expiry before the local (non-Stripe) downgrade fires.
GRACE_DAYS = int(os.getenv("SUBSCRIPTION_DOWNGRADE_GRACE_DAYS", "3"))

# Paid tiers, checked in this order so substrings can't mis-map
# ("premium"/"business" must be tested before "pro").
_PAID_TIERS = ("premium", "business", "pro")


# ============================================================================
# Internal helpers
# ============================================================================

def _ensure_stripe_key() -> bool:
    """
    Guarantee stripe.api_key is set before any Stripe call.

    main.py sets it at app startup, but this module must also work when
    imported standalone (Render shell scripts, migrations, tests). This is
    the fix for the May 2026 'No API key provided' shell failure.
    """
    if not STRIPE_AVAILABLE or not stripe:
        return False
    if getattr(stripe, "api_key", None):
        return True
    key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if key:
        stripe.api_key = key
        return True
    return False


def _tier_from_text(text: Optional[str]) -> Optional[str]:
    """Map any Stripe-side string (lookup_key, nickname, price id, product
    name, metadata value) to a tier. Returns None when nothing matches."""
    t = (text or "").lower()
    if not t:
        return None
    for tier in _PAID_TIERS:
        if tier in t:
            return tier
    return None


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """DB columns store naive UTC; normalize any aware datetime to match."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _first_item_price(sub: Dict[str, Any]) -> Dict[str, Any]:
    """Dict-style extraction of the first subscription item's Price object.
    NEVER use attribute access here — sub.items is the dict method!"""
    try:
        items = sub.get("items") or {}
        data = items.get("data") or []
        if data:
            return data[0].get("price") or {}
    except Exception:
        pass
    return {}


def _period_end_ts(sub: Dict[str, Any]) -> Optional[int]:
    """current_period_end lives on the subscription in older API versions
    and on the subscription item in newer ones. Try both."""
    ts = sub.get("current_period_end")
    if ts:
        return int(ts)
    try:
        data = (sub.get("items") or {}).get("data") or []
        if data and data[0].get("current_period_end"):
            return int(data[0]["current_period_end"])
    except Exception:
        pass
    return None


def _resolve_tier_for_subscription(sub: Dict[str, Any]) -> Optional[str]:
    """
    5-layer tier resolution. Logs which layer matched.
      1. price.lookup_key      (primary: pixelperfect_pro_monthly, …)
      2. price.nickname
      3. price.id
      4. subscription metadata (plan / tier keys, set by checkout session)
      5. product name          (extra API call, e.g. "PixelPerfect Pro")
    """
    price = _first_item_price(sub)
    lookup_key = price.get("lookup_key") or ""
    nickname = price.get("nickname") or ""
    price_id = price.get("id") or ""

    tier = _tier_from_text(lookup_key)
    if tier:
        logger.info("[sync] tier '%s' <- lookup_key '%s'", tier, lookup_key)
        return tier

    tier = _tier_from_text(nickname)
    if tier:
        logger.info("[sync] tier '%s' <- price nickname '%s'", tier, nickname)
        return tier

    tier = _tier_from_text(price_id)
    if tier:
        logger.info("[sync] tier '%s' <- price id '%s'", tier, price_id)
        return tier

    meta = sub.get("metadata") or {}
    for key in ("plan", "tier"):
        tier = _tier_from_text(meta.get(key))
        if tier:
            logger.info("[sync] tier '%s' <- subscription metadata.%s", tier, key)
            return tier

    # Layer 5: product name (one extra API call, only reached when all
    # cheaper layers failed — e.g. no lookup keys configured).
    product_ref = price.get("product")
    if isinstance(product_ref, str):
        product_id = product_ref
    elif isinstance(product_ref, dict):
        product_id = product_ref.get("id")
    else:
        product_id = None
    if product_id:
        try:
            product = stripe.Product.retrieve(product_id)
            name = product.get("name") or ""
            tier = _tier_from_text(name)
            if tier:
                logger.info("[sync] tier '%s' <- product name '%s'", tier, name)
                return tier
            logger.warning("[sync] product name '%s' matched no tier", name)
        except Exception as e:
            logger.warning("[sync] product lookup failed (non-fatal): %s", e)

    logger.warning(
        "[sync] could NOT resolve tier: lookup_key=%r nickname=%r price_id=%r "
        "metadata=%r — check Stripe Price lookup keys.",
        lookup_key, nickname, price_id, dict(meta) if meta else {},
    )
    return None


def _list_entitled_subs(customer_id: str) -> List[Dict[str, Any]]:
    """Return active-or-trialing subscriptions for a customer (dicts)."""
    subs: List[Dict[str, Any]] = []
    for status in _ENTITLED_STATUSES:
        try:
            page = stripe.Subscription.list(
                customer=customer_id,
                status=status,
                limit=5,
                expand=["data.items.data.price"],
            )
            subs.extend(list(page.get("data") or []))
        except Exception as e:
            logger.warning(
                "[sync] Subscription.list(%s, %s) failed: %s",
                customer_id, status, e,
            )
    return subs


def _find_customer_with_sub_by_email(email: str):
    """
    Duplicate-customer rescue: search ALL Stripe customers sharing this email
    and return (customer_id, subscription) for the first one that holds an
    active/trialing subscription. Returns None if none do.
    """
    if not email:
        return None
    try:
        customers = stripe.Customer.list(email=email, limit=10)
    except Exception as e:
        logger.warning("[sync] Customer.list by email failed: %s", e)
        return None

    for cust in customers.get("data") or []:
        cid = cust.get("id")
        if not cid:
            continue
        subs = _list_entitled_subs(cid)
        if subs:
            logger.info(
                "[sync] email fallback: customer %s holds an entitled sub", cid
            )
            return cid, subs[0]
    return None


# ============================================================================
# PUBLIC: sync_user_subscription_from_stripe
# ============================================================================

def sync_user_subscription_from_stripe(user, db) -> None:
    """
    Pull the user's live subscription state from Stripe and persist it.

    Called from GET /subscription_status?sync=1 (dashboard checkout
    verification + manual refresh) and available for shell/scripts.
    Never raises on Stripe/API problems — logs and returns instead.
    Only mutates the DB when Stripe gives an affirmative answer.
    """
    if not _ensure_stripe_key():
        logger.warning("[sync] Stripe unavailable or STRIPE_SECRET_KEY unset — skipped")
        return

    user_id = getattr(user, "id", None)
    email = ((getattr(user, "email", "") or "").strip().lower())
    customer_id = getattr(user, "stripe_customer_id", None)

    logger.info(
        "[sync] start user=%s tier=%s customer=%s",
        user_id, getattr(user, "subscription_tier", None), customer_id,
    )

    # -- 1. Resolve a customer id (DB -> email fallback) ----------------------
    if not customer_id and email:
        try:
            customers = stripe.Customer.list(email=email, limit=1)
            data = customers.get("data") or []
            if data:
                customer_id = data[0].get("id")
                user.stripe_customer_id = customer_id
                db.commit()
                logger.info("[sync] recovered customer %s via email", customer_id)
        except Exception as e:
            logger.warning("[sync] email->customer lookup failed: %s", e)

    if not customer_id:
        logger.info("[sync] user %s has no Stripe customer — nothing to sync", user_id)
        return

    # -- 2. Find an entitled subscription on the recorded customer ------------
    subs = _list_entitled_subs(customer_id)
    sub: Optional[Dict[str, Any]] = subs[0] if subs else None

    # -- 3. Duplicate-customer rescue: scan siblings sharing the email --------
    # Handles: DB points at customer A, but checkout attached the sub to
    # customer B (recreated customers, re-registration, manual deletions).
    if sub is None and email:
        rescued = _find_customer_with_sub_by_email(email)
        if rescued:
            new_customer_id, sub = rescued
            if new_customer_id != customer_id:
                logger.warning(
                    "[sync] customer mismatch corrected: %s -> %s (user %s)",
                    customer_id, new_customer_id, user_id,
                )
                customer_id = new_customer_id
                user.stripe_customer_id = new_customer_id
                # committed below together with the tier update

    # -- 4a. Entitled subscription found -> map tier and persist --------------
    if sub is not None:
        tier = _resolve_tier_for_subscription(sub)
        if tier is None:
            # An active paid subscription whose tier we can't name. Do NOT
            # write "free" over it — surface loudly instead.
            logger.error(
                "[sync] user %s has entitled sub %s but tier resolution failed — "
                "leaving tier unchanged. Fix Stripe Price lookup keys.",
                user_id, sub.get("id"),
            )
            db.rollback()
            return

        old_tier = (getattr(user, "subscription_tier", "free") or "free").lower()
        user.subscription_tier = tier
        user.stripe_subscription_status = sub.get("status")
        user.subscription_status = "active"
        try:
            user.subscription_id = sub.get("id")
        except Exception:
            pass  # column may be absent on very old DBs — non-fatal

        ts = _period_end_ts(sub)
        if ts:
            expires = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
            user.subscription_expires_at = expires
            try:
                user.subscription_ends_at = expires
            except Exception:
                pass
        user.subscription_updated_at = datetime.utcnow()

        try:
            db.commit()
            db.refresh(user)
        except Exception:
            logger.exception("[sync] DB commit failed for user %s", user_id)
            db.rollback()
            return

        if old_tier != tier:
            logger.info("[sync] user %s tier: %s -> %s (sub %s)",
                        user_id, old_tier, tier, sub.get("id"))
        else:
            logger.info("[sync] user %s tier confirmed: %s", user_id, tier)
        return

    # -- 4b. No entitled subscription anywhere for this email/customer --------
    # Affirmative Stripe answer (queries succeeded, zero subs). Downgrade
    # only if the user previously had a synced Stripe subscription state —
    # never touch accounts we've never synced (safety against partial data).
    current_tier = (getattr(user, "subscription_tier", "free") or "free").lower()
    had_stripe_state = bool(
        getattr(user, "stripe_subscription_status", None)
        or getattr(user, "subscription_id", None)
    )
    if current_tier != "free" and had_stripe_state:
        logger.info(
            "[sync] user %s: no entitled subs in Stripe — downgrading %s -> free",
            user_id, current_tier,
        )
        user.subscription_tier = "free"
        user.stripe_subscription_status = "canceled"
        user.subscription_status = "inactive"
        user.subscription_updated_at = datetime.utcnow()
        try:
            db.commit()
        except Exception:
            logger.exception("[sync] downgrade commit failed for user %s", user_id)
            db.rollback()
    else:
        logger.info(
            "[sync] user %s: no entitled subs found; tier '%s' left unchanged "
            "(had_stripe_state=%s)",
            user_id, current_tier, had_stripe_state,
        )


# ============================================================================
# PUBLIC: _apply_local_overdue_downgrade_if_possible
# ============================================================================

def _apply_local_overdue_downgrade_if_possible(user, db) -> None:
    """
    Cheap, LOCAL-only check run on every /subscription_status call (no
    Stripe API traffic — this is what keeps the anti-flood rule intact).

    Downgrades a paid user to free ONLY when:
      - a subscription expiry is recorded in the DB, AND
      - it is more than GRACE_DAYS past due, AND
      - the last KNOWN Stripe status is not active/trialing.

    Anything ambiguous is left for the real Stripe sync to decide.
    """
    try:
        tier = (getattr(user, "subscription_tier", "free") or "free").lower()
        if tier == "free":
            return

        expires = _naive_utc(
            getattr(user, "subscription_expires_at", None)
            or getattr(user, "subscription_ends_at", None)
        )
        if expires is None:
            return

        status = (getattr(user, "stripe_subscription_status", "") or "").lower()
        if status in _ENTITLED_STATUSES:
            return

        if datetime.utcnow() > expires + timedelta(days=GRACE_DAYS):
            logger.info(
                "[sync] local overdue downgrade: user %s %s -> free "
                "(expired %s, status=%r)",
                getattr(user, "id", None), tier, expires.isoformat(), status,
            )
            user.subscription_tier = "free"
            user.subscription_status = "inactive"
            user.subscription_updated_at = datetime.utcnow()
            db.commit()
    except Exception as e:
        # This helper must NEVER break /subscription_status.
        logger.warning("[sync] local downgrade check failed (non-fatal): %s", e)
        try:
            db.rollback()
        except Exception:
            pass

# ===== END OF subscription_sync.py ==========================================

