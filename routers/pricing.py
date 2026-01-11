# ========================================
# PRICING API ENDPOINT - PIXELPERFECT
# ========================================
# Public API endpoint for pricing information
# File: backend/routers/pricing.py
# Author: OneTechly
# Created: January 2026

from fastapi import APIRouter, HTTPException
from typing import Dict, Any, Optional
from pydantic import BaseModel

from config.pricing import PricingConfig, PricingTier

router = APIRouter(prefix="/api/pricing", tags=["Pricing"])

class PricingResponse(BaseModel):
    """Pricing information response"""
    tiers: Dict[str, Any]
    overage: Dict[str, float]
    billing_cycles: Dict[str, Any]

class TierLimitsResponse(BaseModel):
    """Tier limits response"""
    tier: str
    limits: Dict[str, Any]
    features: Dict[str, bool]
    price: Dict[str, Any]

@router.get("/", response_model=PricingResponse)
async def get_pricing():
    """
    Get all pricing information
    
    Returns complete pricing table with all tiers, limits, and features.
    This is a public endpoint - no authentication required.
    
    **Example:**
    ```bash
    curl https://api.pixelperfectapi.net/api/pricing
    ```
    """
    pricing_table = PricingConfig.get_pricing_table()
    
    return {
        "tiers": pricing_table["tiers"],
        "overage": pricing_table["overage"],
        "billing_cycles": {
            "monthly": {
                "id": "monthly",
                "name": "Monthly",
                "description": "Billed monthly"
            },
            "yearly": {
                "id": "yearly",
                "name": "Yearly",
                "description": "Billed annually (save 16%)"
            }
        }
    }

@router.get("/tiers/{tier}", response_model=TierLimitsResponse)
async def get_tier_info(tier: str):
    """
    Get detailed information for a specific tier
    
    **Parameters:**
    - **tier**: Pricing tier (free, pro, or business)
    
    **Example:**
    ```bash
    curl https://api.pixelperfectapi.net/api/pricing/tiers/pro
    ```
    """
    # Validate tier
    try:
        pricing_tier = PricingTier(tier.lower())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tier '{tier}'. Must be one of: free, pro, business"
        )
    
    limits = PricingConfig.get_tier_limits(pricing_tier)
    features = PricingConfig.get_tier_features(pricing_tier)
    price_info = PricingConfig.PRICES[pricing_tier]
    
    return {
        "tier": tier.lower(),
        "limits": limits,
        "features": features,
        "price": {
            "monthly": price_info["price_monthly"],
            "yearly": price_info.get("price_yearly", 0),
            "name": price_info["name"],
            "description": price_info["description"]
        }
    }

@router.get("/compare")
async def compare_tiers():
    """
    Get side-by-side comparison of all tiers
    
    Returns feature matrix for easy comparison.
    
    **Example:**
    ```bash
    curl https://api.pixelperfectapi.net/api/pricing/compare
    ```
    """
    comparison = {}
    
    for tier in [PricingTier.FREE, PricingTier.PRO, PricingTier.BUSINESS]:
        limits = PricingConfig.get_tier_limits(tier)
        features = PricingConfig.get_tier_features(tier)
        price = PricingConfig.get_tier_price(tier, "monthly")
        
        comparison[tier.value] = {
            "name": PricingConfig.PRICES[tier]["name"],
            "price_monthly": price,
            "screenshots_per_month": limits["screenshots_per_month"],
            "batch_size_max": limits["batch_size_max"],
            "max_resolution": f"{limits['max_width']}x{limits['max_height']}",
            "formats": ", ".join(limits["formats"]),
            "features": {
                "batch_processing": features["batch_processing"],
                "webhooks": features["webhooks"],
                "change_detection": features["change_detection"],
                "priority_support": features["priority_support"],
                "dark_mode": features["dark_mode"]
            }
        }
    
    return {
        "comparison": comparison,
        "recommended": "pro"  # Highlight Pro as recommended
    }

@router.post("/calculate-overage")
async def calculate_overage(
    screenshots_used: int,
    tier: str
):
    """
    Calculate overage charges
    
    **Parameters:**
    - **screenshots_used**: Number of screenshots used
    - **tier**: Current pricing tier
    
    **Example:**
    ```bash
    curl -X POST https://api.pixelperfectapi.net/api/pricing/calculate-overage \
      -H "Content-Type: application/json" \
      -d '{"screenshots_used": 150, "tier": "free"}'
    ```
    """
    # Validate tier
    try:
        pricing_tier = PricingTier(tier.lower())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tier '{tier}'. Must be one of: free, pro, business"
        )
    
    tier_limit = PricingConfig.get_monthly_screenshot_limit(pricing_tier)
    overage_cost = PricingConfig.calculate_overage_cost(screenshots_used, tier_limit)
    
    return {
        "tier": tier.lower(),
        "screenshots_used": screenshots_used,
        "tier_limit": tier_limit,
        "overage_count": max(0, screenshots_used - tier_limit),
        "overage_cost": overage_cost,
        "overage_price_per_screenshot": PricingConfig.OVERAGE_PRICE_PER_SCREENSHOT,
        "minimum_charge": PricingConfig.MINIMUM_OVERAGE_CHARGE,
        "total_cost": PricingConfig.get_tier_price(pricing_tier, "monthly") + overage_cost
    }

@router.get("/features/{feature}")
async def get_feature_availability(feature: str):
    """
    Check which tiers have access to a specific feature
    
    **Parameters:**
    - **feature**: Feature name (batch_processing, webhooks, etc.)
    
    **Example:**
    ```bash
    curl https://api.pixelperfectapi.net/api/pricing/features/webhooks
    ```
    """
    availability = {}
    
    for tier in [PricingTier.FREE, PricingTier.PRO, PricingTier.BUSINESS]:
        features = PricingConfig.get_tier_features(tier)
        availability[tier.value] = features.get(feature, False)
    
    return {
        "feature": feature,
        "availability": availability,
        "minimum_tier_required": next(
            (tier.value for tier in [PricingTier.FREE, PricingTier.PRO, PricingTier.BUSINESS]
             if PricingConfig.get_tier_features(tier).get(feature, False)),
            None
        )
    }

# ========================================
# STRIPE INTEGRATION ENDPOINTS
# ========================================

@router.get("/stripe/products")
async def get_stripe_products():
    """
    Get Stripe product and price IDs
    
    Use these IDs when creating checkout sessions.
    
    **Example:**
    ```bash
    curl https://api.pixelperfectapi.net/api/pricing/stripe/products
    ```
    """
    return {
        "pro": {
            "product_id": PricingConfig.STRIPE_PRODUCT_IDS.get(PricingTier.PRO),
            "prices": {
                "monthly": PricingConfig.STRIPE_PRICE_IDS[PricingTier.PRO]["monthly"],
                "yearly": PricingConfig.STRIPE_PRICE_IDS[PricingTier.PRO]["yearly"]
            }
        },
        "business": {
            "product_id": PricingConfig.STRIPE_PRODUCT_IDS.get(PricingTier.BUSINESS),
            "prices": {
                "monthly": PricingConfig.STRIPE_PRICE_IDS[PricingTier.BUSINESS]["monthly"],
                "yearly": PricingConfig.STRIPE_PRICE_IDS[PricingTier.BUSINESS]["yearly"]
            }
        }
    }