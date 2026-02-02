# ============================================================================
# SUBSCRIPTION SYNC - STRIPE INTEGRATION (FIXED)
# File: backend/subscription_sync.py
# Author: OneTechly
# Updated: February 2026 - Fixed lookup_key mapping
# ============================================================================
# ✅ PRODUCTION READY
# ✅ Fixed tier mapping from Stripe lookup_key
# ✅ Proper handling of pixelperfect_pro_monthly → "pro"
# ============================================================================

import logging
from datetime import datetime, timezone
from typing import Optional
import os

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
    Sync user's subscription status from Stripe
    
    ✅ FIXED: Now correctly maps Stripe lookup_key to subscription tier
    
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
                user.subscription_updated_at = datetime.now(timezone.utc)
            
            # ✅ Update expires_at from current_period_end
            period_end = sub.get("current_period_end")
            if period_end:
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
    Check if user's subscription has expired and downgrade if needed
    
    Args:
        user: User model instance
        db: SQLAlchemy session
    """
    # Check subscription_expires_at (primary)
    expires_at = getattr(user, "subscription_expires_at", None)
    
    # Fallback to subscription_ends_at if available
    if not expires_at:
        expires_at = getattr(user, "subscription_ends_at", None)
    
    if expires_at and datetime.now(timezone.utc) > expires_at:
        if user.subscription_tier != "free":
            logger.info(f"⏰ Subscription expired for user {user.id}, downgrading to free")
            user.subscription_tier = "free"
            
            if hasattr(user, "stripe_subscription_status"):
                user.stripe_subscription_status = "expired"
                
            if hasattr(user, "subscription_status"):
                user.subscription_status = "expired"
            
            # Reset usage
            user.usage_screenshots = 0
            user.usage_batch_requests = 0
            user.usage_api_calls = 0
            
            db.commit()


# ============================================================================
# TESTING & DEBUG HELPER
# ============================================================================

def debug_user_subscription(user_id: int, db) -> dict:
    """
    Debug helper to check user's subscription status
    
    Returns dict with all subscription-related fields
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
    
    # Check optional fields
    for field in ["stripe_subscription_status", "subscription_status", 
                  "subscription_expires_at", "subscription_ends_at", 
                  "subscription_updated_at"]:
        if hasattr(user, field):
            result[field] = getattr(user, field)
    
    return result

# =====================================================================================================
# subscription_sync.py - Stripe subscription sync utilities
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
#             limit=1
#         )
        
#         if subscriptions.data:
#             sub = subscriptions.data[0]
            
#             # Update user tier based on Stripe subscription
#             # Map Stripe price IDs to tiers
#             price_id = sub.get("items", {}).get("data", [{}])[0].get("price", {}).get("id", "")
            
#             # Determine tier from price ID or metadata
#             if "starter" in price_id.lower() or sub.get("metadata", {}).get("tier") == "starter":
#                 user.subscription_tier = "starter"
#             elif "pro" in price_id.lower() or sub.get("metadata", {}).get("tier") == "pro":
#                 user.subscription_tier = "pro"
#             elif "business" in price_id.lower() or sub.get("metadata", {}).get("tier") == "business":
#                 user.subscription_tier = "business"
            
#             # Update subscription metadata
#             user.stripe_subscription_status = sub.get("status")
#             user.subscription_updated_at = datetime.now(timezone.utc)
            
#             # Update expires_at if available
#             if sub.get("current_period_end"):
#                 user.subscription_expires_at = datetime.fromtimestamp(
#                     sub["current_period_end"], 
#                     tz=timezone.utc
#                 )
            
#             db.commit()
#             logger.info(f"Synced subscription for user {user.id}: {user.subscription_tier}")
#         else:
#             # No active subscription - downgrade to free
#             if user.subscription_tier != "free":
#                 logger.info(f"No active Stripe subscription for user {user.id}, downgrading to free")
#                 user.subscription_tier = "free"
#                 user.stripe_subscription_status = "inactive"
#                 db.commit()
                
#     except Exception as e:
#         logger.warning(f"Failed to sync subscription for user {user.id}: {e}")


# def _apply_local_overdue_downgrade_if_possible(user, db) -> None:
#     """
#     Check if user's subscription has expired and downgrade if needed
    
#     Args:
#         user: User model instance
#         db: SQLAlchemy session
#     """
#     # Check if subscription has expired
#     expires_at = getattr(user, "subscription_expires_at", None)
    
#     if expires_at and datetime.now(timezone.utc) > expires_at:
#         if user.subscription_tier != "free":
#             logger.info(f"Subscription expired for user {user.id}, downgrading to free")
#             user.subscription_tier = "free"
#             user.stripe_subscription_status = "expired"
            
#             # Reset usage
#             user.usage_screenshots = 0
#             user.usage_batch_requests = 0
#             user.usage_api_calls = 0
            
#             db.commit()