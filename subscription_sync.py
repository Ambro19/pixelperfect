# subscription_sync.py - Stripe subscription sync utilities
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
            limit=1
        )
        
        if subscriptions.data:
            sub = subscriptions.data[0]
            
            # Update user tier based on Stripe subscription
            # Map Stripe price IDs to tiers
            price_id = sub.get("items", {}).get("data", [{}])[0].get("price", {}).get("id", "")
            
            # Determine tier from price ID or metadata
            if "starter" in price_id.lower() or sub.get("metadata", {}).get("tier") == "starter":
                user.subscription_tier = "starter"
            elif "pro" in price_id.lower() or sub.get("metadata", {}).get("tier") == "pro":
                user.subscription_tier = "pro"
            elif "business" in price_id.lower() or sub.get("metadata", {}).get("tier") == "business":
                user.subscription_tier = "business"
            
            # Update subscription metadata
            user.stripe_subscription_status = sub.get("status")
            user.subscription_updated_at = datetime.now(timezone.utc)
            
            # Update expires_at if available
            if sub.get("current_period_end"):
                user.subscription_expires_at = datetime.fromtimestamp(
                    sub["current_period_end"], 
                    tz=timezone.utc
                )
            
            db.commit()
            logger.info(f"Synced subscription for user {user.id}: {user.subscription_tier}")
        else:
            # No active subscription - downgrade to free
            if user.subscription_tier != "free":
                logger.info(f"No active Stripe subscription for user {user.id}, downgrading to free")
                user.subscription_tier = "free"
                user.stripe_subscription_status = "inactive"
                db.commit()
                
    except Exception as e:
        logger.warning(f"Failed to sync subscription for user {user.id}: {e}")


def _apply_local_overdue_downgrade_if_possible(user, db) -> None:
    """
    Check if user's subscription has expired and downgrade if needed
    
    Args:
        user: User model instance
        db: SQLAlchemy session
    """
    # Check if subscription has expired
    expires_at = getattr(user, "subscription_expires_at", None)
    
    if expires_at and datetime.now(timezone.utc) > expires_at:
        if user.subscription_tier != "free":
            logger.info(f"Subscription expired for user {user.id}, downgrading to free")
            user.subscription_tier = "free"
            user.stripe_subscription_status = "expired"
            
            # Reset usage
            user.usage_screenshots = 0
            user.usage_batch_requests = 0
            user.usage_api_calls = 0
            
            db.commit()