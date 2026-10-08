# backend/payment.py - Stripe billing: checkout sessions, billing portal, customer records
import os
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel

from auth_deps import get_current_user
from models import User, get_db

router = APIRouter(prefix="/billing")
logger = logging.getLogger("payment")

# Stripe client setup. Stripe's domains are added to NO_PROXY so calls to Stripe bypass any proxy.
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

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
PRO_LOOKUP = os.getenv("STRIPE_PRO_LOOKUP_KEY", "pro_monthly")
PREM_LOOKUP = os.getenv("STRIPE_PREMIUM_LOOKUP_KEY", "premium_monthly")

# Written to each new Stripe customer's metadata so the Dashboard shows which product created it.
APP_NAME = "pixelperfect_screenshot_api"


# Models
class CheckoutPayload(BaseModel):
    plan: Optional[str] = None
    tier: Optional[str] = None
    price_lookup_key: Optional[str] = None


# Price lookup: Stripe lookup key -> price ID
def _get_price_id(lookup_key: str) -> Optional[str]:
    """Get Stripe price ID from lookup key (with detailed error logging)"""
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


# Customer lookup: reuse a valid stored ID, else find by email, else create one
def _get_or_create_customer(user: User, db: Session) -> str:
    """Get or create Stripe customer (handles all edge cases gracefully)"""
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
                    "created_by": APP_NAME
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


# Public endpoints
@router.get("/config")
def billing_config():
    """Expose billing configuration (safe for frontend)"""
    if not stripe:
        return {
            "mode": "test",
            "is_demo": True,
            "pro_price_id": None,
            "premium_price_id": None,
        }
    
    pro = _get_price_id(PRO_LOOKUP)
    prem = _get_price_id(PREM_LOOKUP)
    
    # ✅ Log if prices are missing (helps debugging)
    if not pro:
        logger.warning("⚠️ Pro price not found - check STRIPE_PRO_LOOKUP_KEY=%s in Stripe Dashboard", PRO_LOOKUP)
    if not prem:
        logger.warning("⚠️ Premium price not found - check STRIPE_PREMIUM_LOOKUP_KEY=%s", PREM_LOOKUP)
    
    return {
        "mode": "live" if (stripe.api_key or "").startswith("sk_live_") else "test",
        "is_demo": False,
        "pro_price_id": pro,
        "premium_price_id": prem,
        "pro_lookup_key": PRO_LOOKUP,
        "premium_lookup_key": PREM_LOOKUP,
    }


@router.post("/create_checkout_session")
def create_checkout_session(
    payload: CheckoutPayload,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create Stripe checkout session (with comprehensive error handling)"""
    if not stripe:
        raise HTTPException(
            status_code=503,
            detail="Payments are not configured. Please contact support."
        )

    # Resolve lookup key
    plan_or_tier = (payload.plan or payload.tier or "").strip().lower()
    lookup_key = payload.price_lookup_key
    
    if not lookup_key:
        if plan_or_tier == "pro":
            lookup_key = PRO_LOOKUP
        elif plan_or_tier == "premium":
            lookup_key = PREM_LOOKUP

    if not lookup_key:
        raise HTTPException(
            status_code=400,
            detail="Missing plan/price_lookup_key. Please specify 'pro' or 'premium'."
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
        }

        session = stripe.checkout.Session.create(**session_params)
        logger.info("✅ Created checkout session %s for customer %s", session.id, customer_id)
        return {"url": session.url}
        
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
    """Create Stripe billing portal session"""
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

##--------- End payment.py Module -----------

#=======================================
# IMPORTANT: DO NOT DELETE THIS FILE BELOW
#======================================

# # backend/payment.py - FIXED: Robust customer creation + NO_PROXY for Stripe
# import os
# import logging
# from typing import Optional

# from fastapi import APIRouter, HTTPException, Depends
# from sqlalchemy.orm import Session
# from pydantic import BaseModel

# from auth_deps import get_current_user
# from models import User, get_db

# router = APIRouter(prefix="/billing")
# logger = logging.getLogger("payment")

# # ===== FIX: Stripe init with NO_PROXY support =====
# stripe = None
# try:
#     import stripe as _stripe
#     key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
#     if key:
#         _stripe.api_key = key
#         stripe = _stripe
        
#         # ✅ Ensure Stripe bypasses proxy (prevents 403 Forbidden errors)
#         os.environ.setdefault("NO_PROXY", "")
#         current_no_proxy = os.environ.get("NO_PROXY", "")
#         stripe_domains = "api.stripe.com,files.stripe.com,checkout.stripe.com"
        
#         if stripe_domains not in current_no_proxy:
#             os.environ["NO_PROXY"] = f"{current_no_proxy},{stripe_domains}" if current_no_proxy else stripe_domains
#             logger.info("✅ Stripe domains excluded from proxy in payment.py")
# except Exception as e:
#     logger.warning("⚠️ Stripe initialization issue in payment.py: %s", e)
#     stripe = None

# FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
# PRO_LOOKUP = os.getenv("STRIPE_PRO_LOOKUP_KEY", "pro_monthly")
# PREM_LOOKUP = os.getenv("STRIPE_PREMIUM_LOOKUP_KEY", "premium_monthly")


# # Models
# class CheckoutPayload(BaseModel):
#     plan: Optional[str] = None
#     tier: Optional[str] = None
#     price_lookup_key: Optional[str] = None


# # ===== FIX: Better price lookup with error handling =====
# def _get_price_id(lookup_key: str) -> Optional[str]:
#     """Get Stripe price ID from lookup key (with detailed error logging)"""
#     if not stripe:
#         logger.warning("Stripe not configured - cannot fetch price for %s", lookup_key)
#         return None
    
#     try:
#         lst = stripe.Price.list(active=True, lookup_keys=[lookup_key], limit=1)
#         if lst.data:
#             price_id = lst.data[0].id
#             logger.info("✅ Found price for %s: %s", lookup_key, price_id)
#             return price_id
#         else:
#             logger.warning("⚠️ No price found for lookup key: %s (check Stripe Dashboard)", lookup_key)
#             return None
#     except Exception as e:
#         logger.error("❌ Stripe price lookup failed for %s: %s", lookup_key, e, exc_info=True)
#         return None


# # ===== FIX: Robust customer creation (handles stale IDs, retries) =====
# def _get_or_create_customer(user: User, db: Session) -> str:
#     """Get or create Stripe customer (handles all edge cases gracefully)"""
#     if not stripe:
#         raise HTTPException(status_code=503, detail="Stripe is not configured")
    
#     stored_customer_id = (user.stripe_customer_id or "").strip() or None
#     valid_customer_id = None
    
#     # Step 1: Verify existing customer ID is still valid
#     if stored_customer_id:
#         try:
#             customer = stripe.Customer.retrieve(stored_customer_id)
#             if customer and not customer.get('deleted', False):
#                 valid_customer_id = stored_customer_id
#                 logger.info("✅ Using existing valid customer: %s", valid_customer_id)
#             else:
#                 logger.warning("⚠️ Stored customer %s is deleted", stored_customer_id)
#         except stripe.error.InvalidRequestError as e:
#             if "No such customer" in str(e):
#                 logger.warning("⚠️ Stored customer %s does not exist", stored_customer_id)
#             else:
#                 logger.warning("⚠️ Error verifying customer %s: %s", stored_customer_id, e)
#         except Exception as e:
#             logger.warning("⚠️ Unexpected error verifying customer %s: %s", stored_customer_id, e)
    
#     # Step 2: Search by email if no valid customer
#     user_email = (user.email or "").strip().lower()
#     if not valid_customer_id and user_email:
#         try:
#             customers = stripe.Customer.list(email=user_email, limit=1)
#             if customers.data:
#                 valid_customer_id = customers.data[0].id
#                 logger.info("✅ Found existing customer by email: %s", valid_customer_id)
#         except Exception as e:
#             logger.warning("⚠️ Error searching customer by email %s: %s", user_email, e)
    
#     # Step 3: Create new customer if none found
#     if not valid_customer_id:
#         try:
#             customer_data = {
#                 "email": user_email,
#                 "name": (user.username or user.email or "User").strip(),
#                 "metadata": {
#                     "user_id": str(user.id),
#                     "created_by": "youtube_downloader_app"
#                 }
#             }
            
#             customer = stripe.Customer.create(**customer_data)
#             valid_customer_id = customer.id
#             logger.info("✅ Created new Stripe customer: %s", valid_customer_id)
#         except Exception as e:
#             msg = getattr(e, "user_message", None) or str(e)
#             logger.error("❌ Failed to create Stripe customer: %s", e, exc_info=True)
#             raise HTTPException(status_code=500, detail=f"Could not create Stripe customer: {msg}")
    
#     # Step 4: Update user record if customer ID changed
#     if valid_customer_id != stored_customer_id:
#         try:
#             user.stripe_customer_id = valid_customer_id
#             db.commit()
#             db.refresh(user)
#             logger.info("✅ Updated user %s with customer ID: %s", user.id, valid_customer_id)
#         except Exception as e:
#             logger.error("❌ Failed to update user with customer ID: %s", e)
#             db.rollback()
#             # Don't fail - we can still proceed with checkout
    
#     return valid_customer_id


# # Public endpoints
# @router.get("/config")
# def billing_config():
#     """Expose billing configuration (safe for frontend)"""
#     if not stripe:
#         return {
#             "mode": "test",
#             "is_demo": True,
#             "pro_price_id": None,
#             "premium_price_id": None,
#         }
    
#     pro = _get_price_id(PRO_LOOKUP)
#     prem = _get_price_id(PREM_LOOKUP)
    
#     # ✅ Log if prices are missing (helps debugging)
#     if not pro:
#         logger.warning("⚠️ Pro price not found - check STRIPE_PRO_LOOKUP_KEY=%s in Stripe Dashboard", PRO_LOOKUP)
#     if not prem:
#         logger.warning("⚠️ Premium price not found - check STRIPE_PREMIUM_LOOKUP_KEY=%s", PREM_LOOKUP)
    
#     return {
#         "mode": "live" if (stripe.api_key or "").startswith("sk_live_") else "test",
#         "is_demo": False,
#         "pro_price_id": pro,
#         "premium_price_id": prem,
#         "pro_lookup_key": PRO_LOOKUP,
#         "premium_lookup_key": PREM_LOOKUP,
#     }


# @router.post("/create_checkout_session")
# def create_checkout_session(
#     payload: CheckoutPayload,
#     user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     """Create Stripe checkout session (with comprehensive error handling)"""
#     if not stripe:
#         raise HTTPException(
#             status_code=503,
#             detail="Payments are not configured. Please contact support."
#         )

#     # Resolve lookup key
#     plan_or_tier = (payload.plan or payload.tier or "").strip().lower()
#     lookup_key = payload.price_lookup_key
    
#     if not lookup_key:
#         if plan_or_tier == "pro":
#             lookup_key = PRO_LOOKUP
#         elif plan_or_tier == "premium":
#             lookup_key = PREM_LOOKUP

#     if not lookup_key:
#         raise HTTPException(
#             status_code=400,
#             detail="Missing plan/price_lookup_key. Please specify 'pro' or 'premium'."
#         )

#     # Get price ID
#     price_id = _get_price_id(lookup_key)
#     if not price_id:
#         logger.error("❌ No price found for lookup key: %s (user: %s)", lookup_key, user.email)
#         raise HTTPException(
#             status_code=400,
#             detail=(
#                 f"Unknown price for lookup key: {lookup_key}. "
#                 "This may indicate a configuration issue. Please contact support."
#             )
#         )

#     # Get or create customer (handles all edge cases)
#     try:
#         customer_id = _get_or_create_customer(user, db)
#     except HTTPException:
#         raise  # Re-raise HTTP exceptions
#     except Exception as e:
#         logger.error("❌ Unexpected error getting customer: %s", e, exc_info=True)
#         raise HTTPException(
#             status_code=500,
#             detail="Error setting up customer account. Please try again or contact support."
#         )

#     # Create checkout session
#     try:
#         session_params = {
#             "mode": "subscription",
#             "customer": customer_id,
#             "line_items": [{"price": price_id, "quantity": 1}],
#             "success_url": f"{FRONTEND_URL}/subscription?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
#             "cancel_url": f"{FRONTEND_URL}/subscription",
#             "payment_method_types": ["card"],
#             "phone_number_collection": {"enabled": False},
#             "billing_address_collection": "auto",
#             "allow_promotion_codes": True,
#             "automatic_tax": {"enabled": False},
#         }

#         session = stripe.checkout.Session.create(**session_params)
#         logger.info("✅ Created checkout session %s for customer %s", session.id, customer_id)
#         return {"url": session.url}
        
#     except Exception as e:
#         logger.error("❌ Failed to create checkout session: %s", e, exc_info=True)
#         msg = getattr(e, "user_message", None) or str(e)
#         raise HTTPException(
#             status_code=500,
#             detail=f"Failed to create checkout session: {msg}"
#         )


# @router.post("/create_portal_session")
# def create_portal_session(
#     user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     """Create Stripe billing portal session"""
#     if not stripe:
#         raise HTTPException(
#             status_code=503,
#             detail="Billing portal is not configured. Please contact support."
#         )
    
#     # Ensure valid customer ID
#     try:
#         customer_id = _get_or_create_customer(user, db)
#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error("❌ Error getting customer for portal: %s", e, exc_info=True)
#         raise HTTPException(
#             status_code=500,
#             detail="Error accessing customer account. Please try again."
#         )

#     try:
#         session = stripe.billing_portal.Session.create(
#             customer=customer_id,
#             return_url=f"{FRONTEND_URL}/subscription",
#         )
#         logger.info("✅ Created portal session for customer %s", customer_id)
#         return {"url": session.url}
        
#     except Exception as e:
#         logger.error("❌ Failed to create portal session: %s", e, exc_info=True)
#         msg = getattr(e, "user_message", None) or str(e)
#         raise HTTPException(
#             status_code=500,
#             detail=f"Could not open billing portal: {msg}"
#         )

# ##--------- End payment.py Module -----------

