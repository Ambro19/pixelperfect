# ========================================
# BILLING ROUTES - PIXELPERFECT SCREENSHOT API
# ========================================
# File: backend/routes/billing.py
# Author: OneTechly
# Updated: January 2026 - FIXED 405 ERROR
#
# Fixes:
# 1) Added proper POST endpoint for /billing/create_checkout_session
# 2) Handles both monthly and yearly billing cycles
# 3) Maps plans to correct Stripe price IDs
# ========================================

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Optional
import stripe
import os
from datetime import datetime

# Assuming you have these imports from your existing code
from auth import get_current_user  # Your auth dependency
from database import get_db  # Your database dependency
from models import User  # Your user model

router = APIRouter(prefix="/billing", tags=["billing"])

# ✅ Load Stripe API key from environment
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# ✅ Your Stripe Price IDs (update these with your actual Stripe Price IDs)
STRIPE_PRICE_IDS = {
    "pro_monthly": "price_1QhoC3BtaMdG6BPFMGVGvtKF",      # Pro Monthly
    "pro_yearly": "price_1QhoC3BtaMdG6BPFvQWw5e7p",       # Pro Yearly
    "business_monthly": "price_1QhoC3BtaMdG6BPF8TLYjTGD",  # Business Monthly
    "business_yearly": "price_1QhoC3BtaMdG6BPFXCpE9TbN",   # Business Yearly
    "premium_monthly": "price_1QhoC3BtaMdG6BPFkLHNmRPQ",   # Premium Monthly
    "premium_yearly": "price_1QhoC3BtaMdG6BPFzQX7KnVm",    # Premium Yearly
}

# ✅ Request model for checkout
class CheckoutSessionRequest(BaseModel):
    plan: str  # "pro", "business", or "premium"
    billing_cycle: str = "monthly"  # "monthly" or "yearly"


# ✅ FIXED: Proper POST endpoint that was missing
@router.post("/create_checkout_session")
async def create_checkout_session(
    request: CheckoutSessionRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Create a Stripe Checkout Session for subscription purchase.
    
    This endpoint:
    1. Validates the plan and billing cycle
    2. Maps to correct Stripe Price ID
    3. Creates Stripe Checkout Session
    4. Returns checkout URL for redirect
    """
    
    try:
        # ✅ Validate plan
        valid_plans = ["pro", "business", "premium"]
        if request.plan.lower() not in valid_plans:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid plan. Must be one of: {', '.join(valid_plans)}"
            )
        
        # ✅ Validate billing cycle
        valid_cycles = ["monthly", "yearly"]
        if request.billing_cycle.lower() not in valid_cycles:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid billing cycle. Must be one of: {', '.join(valid_cycles)}"
            )
        
        # ✅ Build price key
        price_key = f"{request.plan.lower()}_{request.billing_cycle.lower()}"
        
        # ✅ Get Stripe Price ID
        price_id = STRIPE_PRICE_IDS.get(price_key)
        if not price_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Price configuration missing for {price_key}"
            )
        
        # ✅ Get or create Stripe customer
        if not current_user.stripe_customer_id:
            # Create new Stripe customer
            customer = stripe.Customer.create(
                email=current_user.email,
                name=current_user.username,
                metadata={
                    "user_id": str(current_user.id),
                    "username": current_user.username
                }
            )
            
            # Update user with Stripe customer ID
            # You'll need to implement this based on your database setup
            # db.query(User).filter(User.id == current_user.id).update({
            #     "stripe_customer_id": customer.id
            # })
            # db.commit()
            
            customer_id = customer.id
        else:
            customer_id = current_user.stripe_customer_id
        
        # ✅ Create Stripe Checkout Session
        # Get your domain from environment or config
        domain = os.getenv("FRONTEND_URL", "http://localhost:3000")
        
        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[
                {
                    "price": price_id,
                    "quantity": 1,
                }
            ],
            success_url=f"{domain}/dashboard?session_id={{CHECKOUT_SESSION_ID}}&success=true",
            cancel_url=f"{domain}/pricing?canceled=true",
            metadata={
                "user_id": str(current_user.id),
                "plan": request.plan,
                "billing_cycle": request.billing_cycle,
            },
            # ✅ Enable automatic tax calculation if you have it configured
            # automatic_tax={"enabled": True},
        )
        
        # ✅ Return checkout URL
        return {
            "url": checkout_session.url,
            "session_id": checkout_session.id
        }
        
    except stripe.error.StripeError as e:
        print(f"❌ Stripe error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Payment processing error: {str(e)}"
        )
    
    except Exception as e:
        print(f"❌ Checkout session error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create checkout session"
        )


# ✅ Optional: Endpoint to check if endpoint is working
@router.get("/test")
async def test_billing_endpoint():
    """Test endpoint to verify billing routes are loaded"""
    return {
        "status": "ok",
        "message": "Billing endpoint is working",
        "timestamp": datetime.utcnow().isoformat()
    }


# ✅ IMPORTANT: Export the router
__all__ = ["router"]