# ============================================================================
# SUBSCRIPTION SYNC - STRIPE INTEGRATION (PRODUCTION READY)
# ============================================================================
# File: backend/subscription_sync.py
# Author: OneTechly
# Updated: May 2026
# ============================================================================
# ✅ PRODUCTION READY
# ✅ FIX (May 2026): Bulletproof tier detection — 5-layer fallback chain
#    Root cause of FREE tier bug:
#      1. lookup_key was read via dict .get() on a Stripe StripeObject,
#         which silently returned "" instead of the actual lookup_key value.
#      2. price_id fallback checked for "business"/"pro" as a substring of
#         the raw price ID (e.g. "price_1Abc123XYZ") — never matches.
#      3. Result: tier always stayed "free" after checkout.
#    Fix: Use a dedicated _resolve_tier_from_subscription() function that
#    walks all five resolution paths in order and logs each attempt.
# ✅ FIX (May 2026): Premium tier added to all tier-mapping paths
# ✅ FIX (May 2026): Stripe StripeObject attribute access (.attribute)
#    instead of dict .get() for nested price fields
# ✅ Existing: datetime comparison errors fixed via datetime_fix utilities
# ✅ Existing: proper handling of subscription expiration
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


# ── Tier keyword maps ────────────────────────────────────────────────────────
# Used by all fallback paths. Order matters — premium before business before pro
# so that a string containing both "premium" and "pro" resolves to premium.
_TIER_KEYWORDS = [
    ("premium",  "premium"),
    ("business", "business"),
    ("pro",      "pro"),
]

def _keyword_to_tier(s: str) -> Optional[str]:
    """Return the first matching tier keyword found in string s, or None."""
    s_lower = (s or "").lower()
    for keyword, tier in _TIER_KEYWORDS:
        if keyword in s_lower:
            return tier
    return None


def _resolve_tier_from_subscription(sub, user_id: int) -> str:
    """
    Walk five resolution paths to determine the user's subscription tier.

    Returns one of: "pro" | "business" | "premium" | "free"

    Resolution order (stops at first match):
      1. lookup_key on the price object (most reliable — set in Stripe dashboard)
      2. nickname on the price object (human-readable label)
      3. raw price_id substring check (last resort — almost never matches)
      4. subscription metadata["tier"] (set by checkout session creation)
      5. product metadata["tier"] (fallback if product metadata is set)

    Why this replaces the old code:
      The old code used sub.get("items", {}).get("data", [{}])[0].get("price", {})
      which returns {} on a Stripe StripeObject (not a plain dict), so lookup_key
      was always "". This function uses attribute access on the live objects.
    """
    # ── Extract price object via attribute access (not dict .get) ────────────
    price = None
    try:
        items_data = sub.items.data          # StripeObject attribute
        if items_data:
            price = items_data[0].price      # Price StripeObject
    except Exception as e:
        logger.warning("user %s: could not extract price from subscription: %s", user_id, e)

    # Path 1 — lookup_key (set by us in Stripe dashboard)
    if price:
        try:
            lk = price.lookup_key or ""
            if lk:
                tier = _keyword_to_tier(lk)
                if tier:
                    logger.info(
                        "user %s: tier '%s' resolved via lookup_key='%s'",
                        user_id, tier, lk,
                    )
                    return tier
                logger.warning(
                    "user %s: lookup_key='%s' found but no keyword matched — "
                    "check STRIPE_*_LOOKUP_KEY env vars match Stripe dashboard",
                    user_id, lk,
                )
        except Exception as e:
            logger.debug("user %s: lookup_key read error: %s", user_id, e)

    # Path 2 — price nickname (human-readable label, optional but useful)
    if price:
        try:
            nick = price.nickname or ""
            if nick:
                tier = _keyword_to_tier(nick)
                if tier:
                    logger.info(
                        "user %s: tier '%s' resolved via price.nickname='%s'",
                        user_id, tier, nick,
                    )
                    return tier
        except Exception as e:
            logger.debug("user %s: nickname read error: %s", user_id, e)

    # Path 3 — raw price_id substring (almost never matches, kept as belt+suspenders)
    if price:
        try:
            pid = price.id or ""
            tier = _keyword_to_tier(pid)
            if tier:
                logger.info(
                    "user %s: tier '%s' resolved via price.id='%s'",
                    user_id, tier, pid,
                )
                return tier
        except Exception as e:
            logger.debug("user %s: price.id read error: %s", user_id, e)

    # Path 4 — subscription metadata["tier"] (set during checkout session creation)
    try:
        meta_tier = (sub.metadata or {}).get("tier", "").lower().strip()
        if meta_tier in ("pro", "business", "premium"):
            logger.info(
                "user %s: tier '%s' resolved via subscription metadata",
                user_id, meta_tier,
            )
            return meta_tier
    except Exception as e:
        logger.debug("user %s: subscription metadata read error: %s", user_id, e)

    # Path 5 — product metadata["tier"] (optional extra safety net)
    if price:
        try:
            product = price.product
            if isinstance(product, str):
                # Not expanded — skip
                pass
            elif product:
                prod_meta_tier = (getattr(product, "metadata", {}) or {}).get("tier", "").lower().strip()
                if prod_meta_tier in ("pro", "business", "premium"):
                    logger.info(
                        "user %s: tier '%s' resolved via product metadata",
                        user_id, prod_meta_tier,
                    )
                    return prod_meta_tier
        except Exception as e:
            logger.debug("user %s: product metadata read error: %s", user_id, e)

    logger.warning(
        "user %s: could not resolve tier from any path — defaulting to free. "
        "Ensure the Stripe price has a lookup_key matching pixelperfect_pro_monthly / "
        "pixelperfect_business_monthly / pixelperfect_premium_monthly, OR that "
        "STRIPE_*_LOOKUP_KEY env vars on Render match the Stripe dashboard values.",
        user_id,
    )
    return "free"


def sync_user_subscription_from_stripe(user, db) -> None:
    """
    Sync user's subscription status from Stripe.

    Fetches the user's active subscription from Stripe, resolves the tier
    using a 5-layer fallback chain, and updates the database.

    ✅ FIX (May 2026): Uses attribute access on StripeObject (not dict .get)
    ✅ FIX (May 2026): 5-layer tier resolution — lookup_key, nickname,
       price_id, subscription metadata, product metadata
    ✅ FIX (May 2026): Premium tier supported in all resolution paths
    ✅ Existing: timezone-aware datetime comparisons via datetime_fix

    Args:
        user: User model instance
        db: SQLAlchemy session
    """
    if not STRIPE_AVAILABLE or not stripe:
        logger.debug("Stripe not available, skipping sync")
        return

    stripe_customer_id = getattr(user, "stripe_customer_id", None)
    if not stripe_customer_id:
        logger.debug("user %s has no Stripe customer ID", user.id)
        return

    try:
        # ── Fetch active subscription — expand price so attributes are available ─
        subscriptions = stripe.Subscription.list(
            customer=stripe_customer_id,
            status="active",
            limit=1,
            expand=[
                "data.items.data.price",          # price object (lookup_key, nickname, id)
                "data.items.data.price.product",  # product object (product metadata)
            ],
        )

        if not subscriptions.data:
            # No active subscription — downgrade to free
            if (user.subscription_tier or "free") != "free":
                logger.info(
                    "user %s: no active Stripe subscription — downgrading to free",
                    user.id,
                )
                user.subscription_tier = "free"
                _set_status(user, "inactive")
                db.commit()
            return

        sub = subscriptions.data[0]

        # ── Resolve tier ──────────────────────────────────────────────────────
        tier = _resolve_tier_from_subscription(sub, user.id)

        old_tier = user.subscription_tier or "free"
        user.subscription_tier = tier

        # ── Update subscription metadata fields ───────────────────────────────
        _set_status(user, sub.status)

        if hasattr(user, "subscription_updated_at"):
            user.subscription_updated_at = utc_now()

        # ── Update expiry from current_period_end ─────────────────────────────
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
            logger.info("✅ user %s subscription synced: %s → %s", user.id, old_tier, tier)
        else:
            logger.info("✅ user %s subscription confirmed: %s (unchanged)", user.id, tier)

    except Exception as e:
        logger.error("❌ Failed to sync subscription for user %s: %s", user.id, e)
        import traceback
        logger.error(traceback.format_exc())


def _set_status(user, status: str) -> None:
    """Set subscription status fields that exist on the user model."""
    if hasattr(user, "stripe_subscription_status"):
        user.stripe_subscription_status = status
    if hasattr(user, "subscription_status"):
        user.subscription_status = status


def _apply_local_overdue_downgrade_if_possible(user, db) -> None:
    """
    Check if user's subscription has expired and downgrade if needed.

    ✅ Uses timezone-aware datetime comparisons via datetime_fix utilities.

    Args:
        user: User model instance
        db: SQLAlchemy session
    """
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
                    "user %s: subscription expired on %s — downgrading from %s to free",
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
                logger.info("✅ user %s downgraded to free (expiry)", user.id)
        else:
            logger.debug("user %s subscription active until %s", user.id, expires_at_aware)

    except Exception as e:
        logger.error("❌ Local downgrade check failed for user %s: %s", user.id, e)
        import traceback
        logger.debug(traceback.format_exc())


# ============================================================================
# DEBUG HELPER
# ============================================================================

def debug_user_subscription(user_id: int, db) -> dict:
    """
    Debug helper — returns all subscription-related fields for a user.
    Also resolves tier from Stripe live data if possible.
    """
    from models import User

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"error": "User not found"}

    result = {
        "user_id":              user.id,
        "email":                user.email,
        "subscription_tier":    user.subscription_tier,
        "stripe_customer_id":   getattr(user, "stripe_customer_id", None),
    }

    datetime_fields = [
        "subscription_expires_at",
        "subscription_ends_at",
        "subscription_updated_at",
        "usage_reset_at",
        "created_at",
    ]
    for field in datetime_fields:
        if hasattr(user, field):
            value = getattr(user, field)
            if value is not None:
                aware_value = make_aware(value)
                result[field] = {
                    "value": str(aware_value),
                    "is_expired": is_expired(aware_value) if "expires" in field or "ends" in field else None,
                }

    for field in ("stripe_subscription_status", "subscription_status"):
        if hasattr(user, field):
            result[field] = getattr(user, field)

    for field in ("usage_screenshots", "usage_batch_requests", "usage_api_calls"):
        if hasattr(user, field):
            result[field] = getattr(user, field)

    # Live Stripe check
    if STRIPE_AVAILABLE and stripe and result.get("stripe_customer_id"):
        try:
            subs = stripe.Subscription.list(
                customer=result["stripe_customer_id"],
                status="active", limit=1,
                expand=["data.items.data.price"],
            )
            if subs.data:
                sub = subs.data[0]
                live_tier = _resolve_tier_from_subscription(sub, user_id)
                result["stripe_live_tier"]   = live_tier
                result["stripe_sub_status"]  = sub.status
                result["stripe_period_end"]  = sub.current_period_end
            else:
                result["stripe_live_tier"]  = "free (no active subscription)"
        except Exception as e:
            result["stripe_debug_error"] = str(e)

    return result


# ============================================================================
# END OF subscription_sync.py
# ============================================================================

# # ================================================================================================
# # ============================================================================
# # SUBSCRIPTION SYNC - STRIPE INTEGRATION (PRODUCTION READY)
# # ============================================================================
# # File: backend/subscription_sync.py
# # Author: OneTechly
# # Updated: February 2026
# # ============================================================================
# # ✅ PRODUCTION READY
# # ✅ Fixed datetime comparison errors using datetime_fix utilities
# # ✅ Fixed tier mapping from Stripe lookup_key
# # ✅ Proper handling of subscription expiration
# # ============================================================================

# import logging
# from typing import Optional
# import os

# # ✅ CRITICAL FIX: Import datetime utilities
# from datetime_fix import make_aware, utc_now, is_expired, compare_datetimes

# logger = logging.getLogger("pixelperfect")

# # Import Stripe if available
# try:
#     import stripe
#     STRIPE_AVAILABLE = bool(os.getenv("STRIPE_SECRET_KEY"))
# except ImportError:
#     stripe = None
#     STRIPE_AVAILABLE = False


# def sync_user_subscription_from_stripe(user, db) -> None:
#     """
#     Sync user's subscription status from Stripe.
    
#     ✅ FIXED: Now correctly maps Stripe lookup_key to subscription tier
#     ✅ FIXED: Uses timezone-aware datetime comparisons
    
#     Args:
#         user: User model instance
#         db: SQLAlchemy session
#     """
#     if not STRIPE_AVAILABLE or not stripe:
#         logger.debug("Stripe not available, skipping sync")
#         return
    
#     stripe_customer_id = getattr(user, "stripe_customer_id", None)
#     if not stripe_customer_id:
#         logger.debug(f"User {user.id} has no Stripe customer ID")
#         return
    
#     try:
#         # Get active subscriptions from Stripe
#         subscriptions = stripe.Subscription.list(
#             customer=stripe_customer_id,
#             status="active",
#             limit=1,
#             expand=["data.items.data.price"]  # ✅ Expand to get price details
#         )
        
#         if subscriptions.data:
#             sub = subscriptions.data[0]
            
#             # ✅ CRITICAL FIX: Get lookup_key from Price object
#             price_obj = sub.get("items", {}).get("data", [{}])[0].get("price", {})
#             lookup_key = price_obj.get("lookup_key", "")
#             price_id = price_obj.get("id", "")
            
#             logger.info(f"🔍 Stripe sync for user {user.id}: lookup_key={lookup_key}, price_id={price_id}")
            
#             # ✅ Map lookup_key to tier (PRIMARY METHOD)
#             tier = "free"  # Default fallback
            
#             if lookup_key:
#                 # Match lookup_key patterns: pixelperfect_pro_monthly, pixelperfect_business_monthly, etc.
#                 lookup_lower = lookup_key.lower()
                
#                 if "premium" in lookup_lower:
#                     tier = "premium"
#                 elif "business" in lookup_lower:
#                     tier = "business"
#                 elif "pro" in lookup_lower:
#                     tier = "pro"
                    
#                 logger.info(f"✅ Mapped lookup_key '{lookup_key}' → tier '{tier}'")
            
#             # ✅ FALLBACK: Check price_id if lookup_key didn't match
#             if tier == "free" and price_id:
#                 price_lower = price_id.lower()
                
#                 if "premium" in price_lower:
#                     tier = "premium"
#                 elif "business" in price_lower:
#                     tier = "business"
#                 elif "pro" in price_lower:
#                     tier = "pro"
                    
#                 logger.info(f"✅ Fallback: Mapped price_id '{price_id}' → tier '{tier}'")
            
#             # ✅ LAST FALLBACK: Check subscription metadata
#             if tier == "free":
#                 metadata_tier = sub.get("metadata", {}).get("tier", "").lower()
#                 if metadata_tier in ["pro", "business", "premium"]:
#                     tier = metadata_tier
#                     logger.info(f"✅ Metadata fallback: tier '{tier}'")
            
#             # ✅ Update user subscription tier
#             old_tier = user.subscription_tier
#             user.subscription_tier = tier
            
#             # ✅ Update subscription metadata fields
#             if hasattr(user, "stripe_subscription_status"):
#                 user.stripe_subscription_status = sub.get("status", "active")
            
#             if hasattr(user, "subscription_status"):
#                 user.subscription_status = sub.get("status", "active")
            
#             if hasattr(user, "subscription_updated_at"):
#                 # ✅ DATETIME FIX: Use timezone-aware datetime
#                 user.subscription_updated_at = utc_now()
            
#             # ✅ DATETIME FIX: Update expires_at from current_period_end
#             period_end = sub.get("current_period_end")
#             if period_end:
#                 from datetime import datetime, timezone
#                 # Convert Unix timestamp to timezone-aware datetime
#                 expires_dt = datetime.fromtimestamp(period_end, tz=timezone.utc)
                
#                 if hasattr(user, "subscription_expires_at"):
#                     user.subscription_expires_at = expires_dt
                    
#                 if hasattr(user, "subscription_ends_at"):
#                     user.subscription_ends_at = expires_dt
            
#             # ✅ Commit changes
#             db.commit()
            
#             logger.info(f"✅ Synced subscription for user {user.id}: {old_tier} → {tier}")
            
#         else:
#             # No active subscription - downgrade to free
#             if user.subscription_tier != "free":
#                 logger.info(f"⚠️ No active Stripe subscription for user {user.id}, downgrading to free")
#                 user.subscription_tier = "free"
                
#                 if hasattr(user, "stripe_subscription_status"):
#                     user.stripe_subscription_status = "inactive"
                    
#                 if hasattr(user, "subscription_status"):
#                     user.subscription_status = "inactive"
                    
#                 db.commit()
                
#     except Exception as e:
#         logger.error(f"❌ Failed to sync subscription for user {user.id}: {e}")
#         import traceback
#         logger.error(traceback.format_exc())


# def _apply_local_overdue_downgrade_if_possible(user, db) -> None:
#     """
#     Check if user's subscription has expired and downgrade if needed.
    
#     ✅ CRITICAL FIX: Uses timezone-aware datetime comparisons
#     ✅ FIXED: No more "can't compare offset-naive and offset-aware" errors
    
#     Args:
#         user: User model instance
#         db: SQLAlchemy session
#     """
#     try:
#         # Check subscription_expires_at (primary)
#         expires_at = getattr(user, "subscription_expires_at", None)
        
#         # Fallback to subscription_ends_at if available
#         if not expires_at:
#             expires_at = getattr(user, "subscription_ends_at", None)
        
#         if not expires_at:
#             # No expiration date set
#             return
        
#         # ✅ CRITICAL FIX: Use timezone-aware comparison
#         expires_at_aware = make_aware(expires_at)
#         now = utc_now()
        
#         # Check if subscription has expired
#         if is_expired(expires_at_aware):
#             current_tier = (getattr(user, "subscription_tier", "free") or "free").lower()
            
#             # Only downgrade if currently on a paid tier
#             if current_tier in ("pro", "business", "premium"):
#                 logger.info(f"⏰ Subscription expired for user {user.id} on {expires_at_aware}, downgrading from {current_tier} to free")
                
#                 user.subscription_tier = "free"
                
#                 if hasattr(user, "stripe_subscription_status"):
#                     user.stripe_subscription_status = "expired"
                    
#                 if hasattr(user, "subscription_status"):
#                     user.subscription_status = "expired"
                
#                 # ✅ Reset usage counters
#                 user.usage_screenshots = 0
#                 user.usage_batch_requests = 0
#                 user.usage_api_calls = 0
                
#                 # ✅ Update reset date
#                 if hasattr(user, "usage_reset_at"):
#                     user.usage_reset_at = utc_now()
                
#                 db.commit()
#                 db.refresh(user)
                
#                 logger.info(f"✅ User {user.id} downgraded to free tier due to expiration")
#         else:
#             # Subscription is still active
#             logger.debug(f"✅ Subscription for user {user.id} is active until {expires_at_aware}")
            
#     except Exception as e:
#         # ✅ IMPROVED ERROR HANDLING: No longer fails silently
#         logger.error(f"❌ Local downgrade check failed for user {user.id}: {e}")
#         import traceback
#         logger.debug(traceback.format_exc())


# # ============================================================================
# # TESTING & DEBUG HELPER
# # ============================================================================

# def debug_user_subscription(user_id: int, db) -> dict:
#     """
#     Debug helper to check user's subscription status.
    
#     Returns dict with all subscription-related fields and datetime info.
    
#     Args:
#         user_id: User ID to debug
#         db: SQLAlchemy session
        
#     Returns:
#         dict: Subscription debug information
#     """
#     from models import User
    
#     user = db.query(User).filter(User.id == user_id).first()
#     if not user:
#         return {"error": "User not found"}
    
#     result = {
#         "user_id": user.id,
#         "email": user.email,
#         "subscription_tier": user.subscription_tier,
#         "stripe_customer_id": getattr(user, "stripe_customer_id", None),
#     }
    
#     # Check datetime fields and convert to aware
#     datetime_fields = [
#         "subscription_expires_at",
#         "subscription_ends_at", 
#         "subscription_updated_at",
#         "usage_reset_at",
#         "created_at",
#     ]
    
#     for field in datetime_fields:
#         if hasattr(user, field):
#             value = getattr(user, field)
#             if value is not None:
#                 # ✅ Convert to timezone-aware for display
#                 aware_value = make_aware(value)
#                 result[field] = {
#                     "value": str(aware_value),
#                     "is_expired": is_expired(aware_value) if "expires" in field or "ends" in field else None,
#                     "iso": aware_value.isoformat(),
#                 }
    
#     # Check status fields
#     status_fields = [
#         "stripe_subscription_status",
#         "subscription_status",
#     ]
    
#     for field in status_fields:
#         if hasattr(user, field):
#             result[field] = getattr(user, field)
    
#     # Check usage fields
#     usage_fields = [
#         "usage_screenshots",
#         "usage_batch_requests", 
#         "usage_api_calls",
#     ]
    
#     for field in usage_fields:
#         if hasattr(user, field):
#             result[field] = getattr(user, field)
    
#     return result


# # ============================================================================
# # END OF subscription_sync.py
# # ============================================================================
