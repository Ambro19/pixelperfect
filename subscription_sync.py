# ============================================================================
# SUBSCRIPTION SYNC - STRIPE INTEGRATION (PRODUCTION READY)
# ============================================================================
# File: backend/subscription_sync.py
# Author: OneTechly
# Updated: February 2026
# ============================================================================
# ✅ PRODUCTION READY
# ✅ Fixed datetime comparison errors using datetime_fix utilities
# ✅ Fixed tier mapping from Stripe lookup_key
# ✅ Proper handling of subscription expiration
# ============================================================================

import logging
from typing import Optional
import os

# ✅ CRITICAL FIX: Import datetime utilities
from datetime_fix import make_aware, utc_now, is_expired, compare_datetimes

logger = logging.getLogger("pixelperfect")

# Import Stripe if available
try:
    import stripe
    STRIPE_AVAILABLE = bool(os.getenv("STRIPE_SECRET_KEY"))
except ImportError:
    stripe = None
    STRIPE_AVAILABLE = False


def sync_user_subscription_from_stripe(user, db) -> None:
    """
    Sync user's subscription status from Stripe.
    
    ✅ FIXED: Now correctly maps Stripe lookup_key to subscription tier
    ✅ FIXED: Uses timezone-aware datetime comparisons
    
    Args:
        user: User model instance
        db: SQLAlchemy session
    """
    if not STRIPE_AVAILABLE or not stripe:
        logger.debug("Stripe not available, skipping sync")
        return
    
    stripe_customer_id = getattr(user, "stripe_customer_id", None)
    if not stripe_customer_id:
        logger.debug(f"User {user.id} has no Stripe customer ID")
        return
    
    try:
        # Get active subscriptions from Stripe
        subscriptions = stripe.Subscription.list(
            customer=stripe_customer_id,
            status="active",
            limit=1,
            expand=["data.items.data.price"]  # ✅ Expand to get price details
        )
        
        if subscriptions.data:
            sub = subscriptions.data[0]
            
            # ✅ CRITICAL FIX: Get lookup_key from Price object
            price_obj = sub.get("items", {}).get("data", [{}])[0].get("price", {})
            lookup_key = price_obj.get("lookup_key", "")
            price_id = price_obj.get("id", "")
            
            logger.info(f"🔍 Stripe sync for user {user.id}: lookup_key={lookup_key}, price_id={price_id}")
            
            # ✅ Map lookup_key to tier (PRIMARY METHOD)
            tier = "free"  # Default fallback
            
            if lookup_key:
                # Match lookup_key patterns: pixelperfect_pro_monthly, pixelperfect_business_monthly, etc.
                lookup_lower = lookup_key.lower()
                
                if "premium" in lookup_lower:
                    tier = "premium"
                elif "business" in lookup_lower:
                    tier = "business"
                elif "pro" in lookup_lower:
                    tier = "pro"
                    
                logger.info(f"✅ Mapped lookup_key '{lookup_key}' → tier '{tier}'")
            
            # ✅ FALLBACK: Check price_id if lookup_key didn't match
            if tier == "free" and price_id:
                price_lower = price_id.lower()
                
                if "premium" in price_lower:
                    tier = "premium"
                elif "business" in price_lower:
                    tier = "business"
                elif "pro" in price_lower:
                    tier = "pro"
                    
                logger.info(f"✅ Fallback: Mapped price_id '{price_id}' → tier '{tier}'")
            
            # ✅ LAST FALLBACK: Check subscription metadata
            if tier == "free":
                metadata_tier = sub.get("metadata", {}).get("tier", "").lower()
                if metadata_tier in ["pro", "business", "premium"]:
                    tier = metadata_tier
                    logger.info(f"✅ Metadata fallback: tier '{tier}'")
            
            # ✅ Update user subscription tier
            old_tier = user.subscription_tier
            user.subscription_tier = tier
            
            # ✅ Update subscription metadata fields
            if hasattr(user, "stripe_subscription_status"):
                user.stripe_subscription_status = sub.get("status", "active")
            
            if hasattr(user, "subscription_status"):
                user.subscription_status = sub.get("status", "active")
            
            if hasattr(user, "subscription_updated_at"):
                # ✅ DATETIME FIX: Use timezone-aware datetime
                user.subscription_updated_at = utc_now()
            
            # ✅ DATETIME FIX: Update expires_at from current_period_end
            period_end = sub.get("current_period_end")
            if period_end:
                from datetime import datetime, timezone
                # Convert Unix timestamp to timezone-aware datetime
                expires_dt = datetime.fromtimestamp(period_end, tz=timezone.utc)
                
                if hasattr(user, "subscription_expires_at"):
                    user.subscription_expires_at = expires_dt
                    
                if hasattr(user, "subscription_ends_at"):
                    user.subscription_ends_at = expires_dt
            
            # ✅ Commit changes
            db.commit()
            
            logger.info(f"✅ Synced subscription for user {user.id}: {old_tier} → {tier}")
            
        else:
            # No active subscription - downgrade to free
            if user.subscription_tier != "free":
                logger.info(f"⚠️ No active Stripe subscription for user {user.id}, downgrading to free")
                user.subscription_tier = "free"
                
                if hasattr(user, "stripe_subscription_status"):
                    user.stripe_subscription_status = "inactive"
                    
                if hasattr(user, "subscription_status"):
                    user.subscription_status = "inactive"
                    
                db.commit()
                
    except Exception as e:
        logger.error(f"❌ Failed to sync subscription for user {user.id}: {e}")
        import traceback
        logger.error(traceback.format_exc())


def _apply_local_overdue_downgrade_if_possible(user, db) -> None:
    """
    Check if user's subscription has expired and downgrade if needed.
    
    ✅ CRITICAL FIX: Uses timezone-aware datetime comparisons
    ✅ FIXED: No more "can't compare offset-naive and offset-aware" errors
    
    Args:
        user: User model instance
        db: SQLAlchemy session
    """
    try:
        # Check subscription_expires_at (primary)
        expires_at = getattr(user, "subscription_expires_at", None)
        
        # Fallback to subscription_ends_at if available
        if not expires_at:
            expires_at = getattr(user, "subscription_ends_at", None)
        
        if not expires_at:
            # No expiration date set
            return
        
        # ✅ CRITICAL FIX: Use timezone-aware comparison
        expires_at_aware = make_aware(expires_at)
        now = utc_now()
        
        # Check if subscription has expired
        if is_expired(expires_at_aware):
            current_tier = (getattr(user, "subscription_tier", "free") or "free").lower()
            
            # Only downgrade if currently on a paid tier
            if current_tier in ("pro", "business", "premium"):
                logger.info(f"⏰ Subscription expired for user {user.id} on {expires_at_aware}, downgrading from {current_tier} to free")
                
                user.subscription_tier = "free"
                
                if hasattr(user, "stripe_subscription_status"):
                    user.stripe_subscription_status = "expired"
                    
                if hasattr(user, "subscription_status"):
                    user.subscription_status = "expired"
                
                # ✅ Reset usage counters
                user.usage_screenshots = 0
                user.usage_batch_requests = 0
                user.usage_api_calls = 0
                
                # ✅ Update reset date
                if hasattr(user, "usage_reset_at"):
                    user.usage_reset_at = utc_now()
                
                db.commit()
                db.refresh(user)
                
                logger.info(f"✅ User {user.id} downgraded to free tier due to expiration")
        else:
            # Subscription is still active
            logger.debug(f"✅ Subscription for user {user.id} is active until {expires_at_aware}")
            
    except Exception as e:
        # ✅ IMPROVED ERROR HANDLING: No longer fails silently
        logger.error(f"❌ Local downgrade check failed for user {user.id}: {e}")
        import traceback
        logger.debug(traceback.format_exc())


# ============================================================================
# TESTING & DEBUG HELPER
# ============================================================================

def debug_user_subscription(user_id: int, db) -> dict:
    """
    Debug helper to check user's subscription status.
    
    Returns dict with all subscription-related fields and datetime info.
    
    Args:
        user_id: User ID to debug
        db: SQLAlchemy session
        
    Returns:
        dict: Subscription debug information
    """
    from models import User
    
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"error": "User not found"}
    
    result = {
        "user_id": user.id,
        "email": user.email,
        "subscription_tier": user.subscription_tier,
        "stripe_customer_id": getattr(user, "stripe_customer_id", None),
    }
    
    # Check datetime fields and convert to aware
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
                # ✅ Convert to timezone-aware for display
                aware_value = make_aware(value)
                result[field] = {
                    "value": str(aware_value),
                    "is_expired": is_expired(aware_value) if "expires" in field or "ends" in field else None,
                    "iso": aware_value.isoformat(),
                }
    
    # Check status fields
    status_fields = [
        "stripe_subscription_status",
        "subscription_status",
    ]
    
    for field in status_fields:
        if hasattr(user, field):
            result[field] = getattr(user, field)
    
    # Check usage fields
    usage_fields = [
        "usage_screenshots",
        "usage_batch_requests", 
        "usage_api_calls",
    ]
    
    for field in usage_fields:
        if hasattr(user, field):
            result[field] = getattr(user, field)
    
    return result


# ============================================================================
# END OF subscription_sync.py
# ============================================================================

# # ============================================================================
# # SUBSCRIPTION SYNC - STRIPE INTEGRATION (FIXED)
# # File: backend/subscription_sync.py
# # Author: OneTechly
# # Updated: February 2026 - Fixed lookup_key mapping
# # ============================================================================
# # ✅ PRODUCTION READY
# # ✅ Fixed tier mapping from Stripe lookup_key
# # ✅ Proper handling of pixelperfect_pro_monthly → "pro"
# # ============================================================================

# import logging
# from datetime import datetime, timezone
# from typing import Optional
# import os

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
#     Sync user's subscription status from Stripe
    
#     ✅ FIXED: Now correctly maps Stripe lookup_key to subscription tier
    
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
#                 user.subscription_updated_at = datetime.now(timezone.utc)
            
#             # ✅ Update expires_at from current_period_end
#             period_end = sub.get("current_period_end")
#             if period_end:
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
#     Check if user's subscription has expired and downgrade if needed
    
#     Args:
#         user: User model instance
#         db: SQLAlchemy session
#     """
#     # Check subscription_expires_at (primary)
#     expires_at = getattr(user, "subscription_expires_at", None)
    
#     # Fallback to subscription_ends_at if available
#     if not expires_at:
#         expires_at = getattr(user, "subscription_ends_at", None)
    
#     if expires_at and datetime.now(timezone.utc) > expires_at:
#         if user.subscription_tier != "free":
#             logger.info(f"⏰ Subscription expired for user {user.id}, downgrading to free")
#             user.subscription_tier = "free"
            
#             if hasattr(user, "stripe_subscription_status"):
#                 user.stripe_subscription_status = "expired"
                
#             if hasattr(user, "subscription_status"):
#                 user.subscription_status = "expired"
            
#             # Reset usage
#             user.usage_screenshots = 0
#             user.usage_batch_requests = 0
#             user.usage_api_calls = 0
            
#             db.commit()


# # ============================================================================
# # TESTING & DEBUG HELPER
# # ============================================================================

# def debug_user_subscription(user_id: int, db) -> dict:
#     """
#     Debug helper to check user's subscription status
    
#     Returns dict with all subscription-related fields
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
    
#     # Check optional fields
#     for field in ["stripe_subscription_status", "subscription_status", 
#                   "subscription_expires_at", "subscription_ends_at", 
#                   "subscription_updated_at"]:
#         if hasattr(user, field):
#             result[field] = getattr(user, field)
    
#     return result

