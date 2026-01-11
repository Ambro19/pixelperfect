# =================================================================================================
# backend/routers/payment.py
# PixelPerfect Payment & Billing Integration
# Converted from YCD payment.py - maintains same robustness and professional Stripe handling
# - Verified for screenshot usage tracking
# - Robust customer creation (handles stale IDs)
# - NO_PROXY for Stripe domains
# - Professional error handling and logging
# =================================================================================================

import os
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel

from auth_deps import get_current_user
from models import User, get_db

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])
logger = logging.getLogger("payment")

# ===== Stripe Initialization with NO_PROXY Support =====
stripe = None
try:
    import stripe as _stripe
    key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if key:
        _stripe.api_key = key
        stripe = _stripe
        
        # ✅ Ensure Stripe bypasses proxy (prevents 403 Forbidden errors)
        os.environ.setdefault("NO_PROXY", "")
        current_no_proxy = os.environ.get("NO_PROXY", "")
        stripe_domains = "api.stripe.com,files.stripe.com,checkout.stripe.com"
        
        if stripe_domains not in current_no_proxy:
            os.environ["NO_PROXY"] = f"{current_no_proxy},{stripe_domains}" if current_no_proxy else stripe_domains
            logger.info("✅ Stripe domains excluded from proxy in payment.py")
except Exception as e:
    logger.warning("⚠️ Stripe initialization issue in payment.py: %s", e)
    stripe = None

# Environment Configuration
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

# PixelPerfect Pricing Tiers (updated from YCD)
# These should match your pricing.py configuration
PRO_LOOKUP = os.getenv("STRIPE_PRO_LOOKUP_KEY", "pixelperfect_pro_monthly")
BUSINESS_LOOKUP = os.getenv("STRIPE_BUSINESS_LOOKUP_KEY", "pixelperfect_business_monthly")

# Legacy support for old environment variable names
if not os.getenv("STRIPE_PRO_LOOKUP_KEY"):
    PRO_LOOKUP = os.getenv("STRIPE_PREMIUM_LOOKUP_KEY", "pro_monthly")

# Request Models
class CheckoutPayload(BaseModel):
    """Checkout session request payload"""
    plan: Optional[str] = None
    tier: Optional[str] = None
    price_lookup_key: Optional[str] = None


# ===== Price Lookup with Error Handling =====
def _get_price_id(lookup_key: str) -> Optional[str]:
    """
    Get Stripe price ID from lookup key with detailed error logging
    
    Args:
        lookup_key: Stripe price lookup key
        
    Returns:
        Stripe price ID or None if not found
    """
    if not stripe:
        logger.warning("Stripe not configured - cannot fetch price for %s", lookup_key)
        return None
    
    try:
        lst = stripe.Price.list(active=True, lookup_keys=[lookup_key], limit=1)
        if lst.data:
            price_id = lst.data[0].id
            logger.info("✅ Found price for %s: %s", lookup_key, price_id)
            return price_id
        else:
            logger.warning("⚠️ No price found for lookup key: %s (check Stripe Dashboard)", lookup_key)
            return None
    except Exception as e:
        logger.error("❌ Stripe price lookup failed for %s: %s", lookup_key, e, exc_info=True)
        return None


# ===== Robust Customer Creation (Handles All Edge Cases) =====
def _get_or_create_customer(user: User, db: Session) -> str:
    """
    Get or create Stripe customer with comprehensive error handling
    
    Handles:
    - Stale customer IDs
    - Deleted customers
    - Duplicate customers by email
    - Database sync issues
    
    Args:
        user: User model instance
        db: Database session
        
    Returns:
        Valid Stripe customer ID
        
    Raises:
        HTTPException: If customer creation fails
    """
    if not stripe:
        raise HTTPException(status_code=503, detail="Stripe is not configured")
    
    stored_customer_id = (user.stripe_customer_id or "").strip() or None
    valid_customer_id = None
    
    # Step 1: Verify existing customer ID is still valid
    if stored_customer_id:
        try:
            customer = stripe.Customer.retrieve(stored_customer_id)
            if customer and not customer.get('deleted', False):
                valid_customer_id = stored_customer_id
                logger.info("✅ Using existing valid customer: %s", valid_customer_id)
            else:
                logger.warning("⚠️ Stored customer %s is deleted", stored_customer_id)
        except stripe.error.InvalidRequestError as e:
            if "No such customer" in str(e):
                logger.warning("⚠️ Stored customer %s does not exist", stored_customer_id)
            else:
                logger.warning("⚠️ Error verifying customer %s: %s", stored_customer_id, e)
        except Exception as e:
            logger.warning("⚠️ Unexpected error verifying customer %s: %s", stored_customer_id, e)
    
    # Step 2: Search by email if no valid customer
    user_email = (user.email or "").strip().lower()
    if not valid_customer_id and user_email:
        try:
            customers = stripe.Customer.list(email=user_email, limit=1)
            if customers.data:
                valid_customer_id = customers.data[0].id
                logger.info("✅ Found existing customer by email: %s", valid_customer_id)
        except Exception as e:
            logger.warning("⚠️ Error searching customer by email %s: %s", user_email, e)
    
    # Step 3: Create new customer if none found
    if not valid_customer_id:
        try:
            customer_data = {
                "email": user_email,
                "name": (user.username or user.email or "User").strip(),
                "metadata": {
                    "user_id": str(user.id),
                    "created_by": "pixelperfect_screenshot_api",
                    "tier": user.subscription_tier or "free"
                }
            }
            
            customer = stripe.Customer.create(**customer_data)
            valid_customer_id = customer.id
            logger.info("✅ Created new Stripe customer: %s", valid_customer_id)
        except Exception as e:
            msg = getattr(e, "user_message", None) or str(e)
            logger.error("❌ Failed to create Stripe customer: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Could not create Stripe customer: {msg}")
    
    # Step 4: Update user record if customer ID changed
    if valid_customer_id != stored_customer_id:
        try:
            user.stripe_customer_id = valid_customer_id
            db.commit()
            db.refresh(user)
            logger.info("✅ Updated user %s with customer ID: %s", user.id, valid_customer_id)
        except Exception as e:
            logger.error("❌ Failed to update user with customer ID: %s", e)
            db.rollback()
            # Don't fail - we can still proceed with checkout
    
    return valid_customer_id


# ===== Public API Endpoints =====

@router.get("/config")
def billing_config():
    """
    Expose billing configuration (safe for frontend)
    
    Returns available pricing tiers and Stripe configuration.
    """
    if not stripe:
        return {
            "mode": "test",
            "is_demo": True,
            "pro_price_id": None,
            "business_price_id": None,
            "configured": False
        }
    
    pro = _get_price_id(PRO_LOOKUP)
    business = _get_price_id(BUSINESS_LOOKUP)
    
    # ✅ Log if prices are missing (helps debugging)
    if not pro:
        logger.warning("⚠️ Pro price not found - check STRIPE_PRO_LOOKUP_KEY=%s in Stripe Dashboard", PRO_LOOKUP)
    if not business:
        logger.warning("⚠️ Business price not found - check STRIPE_BUSINESS_LOOKUP_KEY=%s", BUSINESS_LOOKUP)
    
    return {
        "mode": "live" if (stripe.api_key or "").startswith("sk_live_") else "test",
        "is_demo": False,
        "configured": True,
        "pro_price_id": pro,
        "business_price_id": business,
        "pro_lookup_key": PRO_LOOKUP,
        "business_lookup_key": BUSINESS_LOOKUP,
        "tiers": {
            "free": {
                "name": "Free",
                "price": 0,
                "screenshots": 100
            },
            "pro": {
                "name": "Pro",
                "price": 49,
                "screenshots": 5000,
                "lookup_key": PRO_LOOKUP
            },
            "business": {
                "name": "Business",
                "price": 199,
                "screenshots": 50000,
                "lookup_key": BUSINESS_LOOKUP
            }
        }
    }


@router.post("/create_checkout_session")
def create_checkout_session(
    payload: CheckoutPayload,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create Stripe checkout session for subscription
    
    Handles Pro and Business tier subscriptions with comprehensive error handling.
    """
    if not stripe:
        raise HTTPException(
            status_code=503,
            detail="Payments are not configured. Please contact support."
        )

    # Resolve lookup key from plan/tier
    plan_or_tier = (payload.plan or payload.tier or "").strip().lower()
    lookup_key = payload.price_lookup_key
    
    if not lookup_key:
        if plan_or_tier in ["pro", "professional"]:
            lookup_key = PRO_LOOKUP
        elif plan_or_tier in ["business", "premium", "enterprise"]:
            lookup_key = BUSINESS_LOOKUP

    if not lookup_key:
        raise HTTPException(
            status_code=400,
            detail="Missing plan/price_lookup_key. Please specify 'pro' or 'business'."
        )

    # Get price ID
    price_id = _get_price_id(lookup_key)
    if not price_id:
        logger.error("❌ No price found for lookup key: %s (user: %s)", lookup_key, user.email)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown price for lookup key: {lookup_key}. "
                "This may indicate a configuration issue. Please contact support."
            )
        )

    # Get or create customer (handles all edge cases)
    try:
        customer_id = _get_or_create_customer(user, db)
    except HTTPException:
        raise  # Re-raise HTTP exceptions
    except Exception as e:
        logger.error("❌ Unexpected error getting customer: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Error setting up customer account. Please try again or contact support."
        )

    # Create checkout session
    try:
        session_params = {
            "mode": "subscription",
            "customer": customer_id,
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": f"{FRONTEND_URL}/subscription?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{FRONTEND_URL}/subscription",
            "payment_method_types": ["card"],
            "phone_number_collection": {"enabled": False},
            "billing_address_collection": "auto",
            "allow_promotion_codes": True,
            "automatic_tax": {"enabled": False},
            "metadata": {
                "user_id": str(user.id),
                "product": "pixelperfect_screenshots",
                "tier": plan_or_tier
            }
        }

        session = stripe.checkout.Session.create(**session_params)
        logger.info("✅ Created checkout session %s for customer %s (tier: %s)", session.id, customer_id, plan_or_tier)
        return {"url": session.url, "session_id": session.id}
        
    except Exception as e:
        logger.error("❌ Failed to create checkout session: %s", e, exc_info=True)
        msg = getattr(e, "user_message", None) or str(e)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create checkout session: {msg}"
        )


@router.post("/create_portal_session")
def create_portal_session(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create Stripe billing portal session
    
    Allows users to manage their subscription, update payment methods, and view invoices.
    """
    if not stripe:
        raise HTTPException(
            status_code=503,
            detail="Billing portal is not configured. Please contact support."
        )
    
    # Ensure valid customer ID
    try:
        customer_id = _get_or_create_customer(user, db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("❌ Error getting customer for portal: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Error accessing customer account. Please try again."
        )

    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{FRONTEND_URL}/subscription",
        )
        logger.info("✅ Created portal session for customer %s", customer_id)
        return {"url": session.url}
        
    except Exception as e:
        logger.error("❌ Failed to create portal session: %s", e, exc_info=True)
        msg = getattr(e, "user_message", None) or str(e)
        raise HTTPException(
            status_code=500,
            detail=f"Could not open billing portal: {msg}"
        )


@router.get("/subscription")
def get_subscription_info(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get current subscription information
    
    Returns user's current tier, usage stats, and subscription details.
    """
    try:
        # Get tier limits
        from models import get_tier_limits
        tier = user.subscription_tier or "free"
        limits = get_tier_limits(tier)
        
        # Get current usage
        usage = {
            "screenshots": getattr(user, "usage_screenshots", 0) or 0,
            "batch_requests": getattr(user, "usage_batch_requests", 0) or 0,
            "api_calls": getattr(user, "usage_api_calls", 0) or 0,
        }
        
        # Calculate remaining capacity
        remaining = {}
        for key, limit in limits.items():
            if isinstance(limit, (int, float)) and not key.startswith("max_"):
                current = usage.get(key, 0)
                if limit == float("inf"):
                    remaining[key] = "unlimited"
                else:
                    remaining[key] = max(0, limit - current)
        
        response = {
            "tier": tier,
            "limits": limits,
            "usage": usage,
            "remaining": remaining,
            "usage_reset_date": user.usage_reset_date.isoformat() if user.usage_reset_date else None,
            "stripe_customer_id": user.stripe_customer_id
        }
        
        # Get active Stripe subscription if available
        if stripe and user.stripe_customer_id:
            try:
                subscriptions = stripe.Subscription.list(
                    customer=user.stripe_customer_id,
                    status="active",
                    limit=1
                )
                if subscriptions.data:
                    sub = subscriptions.data[0]
                    response["stripe_subscription"] = {
                        "id": sub.id,
                        "status": sub.status,
                        "current_period_end": sub.current_period_end,
                        "cancel_at_period_end": sub.cancel_at_period_end
                    }
            except Exception as e:
                logger.warning(f"Could not fetch Stripe subscription: {e}")
        
        return response
        
    except Exception as e:
        logger.error(f"Error fetching subscription info: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch subscription information"
        )

# ============= End Payment Module =============