# ========================================
# PRICING CONFIGURATION - PIXELPERFECT (WITH PREMIUM)
# ========================================
# Production-ready pricing tiers and limits
# File: backend/config/pricing.py
# Author: OneTechly
# Updated: January 2026 - Added Premium Tier

from enum import Enum
from typing import Dict, Any

class PricingTier(str, Enum):
    """Pricing tier enumeration"""
    FREE = "free"
    PRO = "pro"
    BUSINESS = "business"
    PREMIUM = "premium"  # NEW: Added Premium tier

class PricingConfig:
    """
    Centralized pricing configuration for PixelPerfect
    
    Update these values to change pricing across the entire application
    """
    
    # ========================================
    # MONTHLY SUBSCRIPTION PRICES (USD)
    # ========================================
    
    PRICES = {
        PricingTier.FREE: {
            "price_monthly": 0,
            "price_yearly": 0,
            "name": "Free",
            "description": "Perfect for trying out PixelPerfect"
        },
        PricingTier.PRO: {
            "price_monthly": 49,
            "price_yearly": 490,  # ~$41/month (16% savings)
            "name": "Pro",
            "description": "For professionals and small teams",
            "badge": "MOST POPULAR"
        },
        PricingTier.BUSINESS: {
            "price_monthly": 199,
            "price_yearly": 1990,  # ~$166/month (16% savings)
            "name": "Business",
            "description": "For agencies and large teams"
        },
        PricingTier.PREMIUM: {
            "price_monthly": 499,
            "price_yearly": 4990,  # ~$416/month (16% savings)
            "name": "Premium",
            "description": "For enterprises and high-volume users"
        }
    }
    
    # ========================================
    # SCREENSHOT LIMITS
    # ========================================
    
    LIMITS = {
        PricingTier.FREE: {
            "screenshots_per_month": 100,
            "screenshots_per_day": 10,
            "screenshots_per_hour": 5,
            "batch_size_max": 5,
            "max_width": 1920,
            "max_height": 1080,
            "formats": ["png", "jpeg"],
            "features": [
                "100 screenshots per month",
                "Basic customization",
                "Community support",
                "Standard resolution (up to 1920x1080)"
            ]
        },
        PricingTier.PRO: {
            "screenshots_per_month": 5000,
            "screenshots_per_day": 500,
            "screenshots_per_hour": 100,
            "batch_size_max": 50,
            "max_width": 3840,  # 4K
            "max_height": 2160,  # 4K
            "formats": ["png", "jpeg", "webp"],
            "features": [
                "5,000 screenshots/month",
                "Full customization",
                "Batch processing (up to 50 URLs)",
                "Priority support",
                "4K resolution (up to 3840x2160)",
                "All image formats (PNG, JPEG, WebP)",
                "Dark mode screenshots",
                "Element removal",
                "Custom delays"
            ]
        },
        PricingTier.BUSINESS: {
            "screenshots_per_month": 50000,
            "screenshots_per_day": 5000,
            "screenshots_per_hour": 500,
            "batch_size_max": 100,
            "max_width": 3840,  # 4K
            "max_height": 2160,  # 4K
            "formats": ["png", "jpeg", "webp"],
            "features": [
                "50,000 screenshots/month",
                "Everything in Pro",
                "Batch processing (up to 100 URLs)",
                "Webhooks & change detection",
                "Dedicated support",
                "Change detection & monitoring",
                "Webhook notifications",
                "99.9% uptime SLA",
                "Priority processing queue"
            ]
        },
        PricingTier.PREMIUM: {
            "screenshots_per_month": -1,  # -1 = unlimited
            "screenshots_per_day": 50000,  # Reasonable daily cap to prevent abuse
            "screenshots_per_hour": 5000,  # Reasonable hourly cap
            "batch_size_max": 500,  # Much larger batch processing
            "max_width": 7680,  # 8K resolution
            "max_height": 4320,  # 8K resolution
            "formats": ["png", "jpeg", "webp"],
            "features": [
                "Unlimited screenshots",
                "Everything in Business",
                "API access with webhooks",
                "Custom integrations (S3, Slack, SSO)",
                "Dedicated support (<1h 24/7)",
                "Unlimited batch processing",
                "White-label options",
                "8K resolution support",
                "Custom SLA options",
                "Volume discounts available"
            ]
        }
    }
    
    # ========================================
    # PAY-AS-YOU-GO PRICING (Overages)
    # ========================================
    
    OVERAGE_PRICE_PER_SCREENSHOT = 0.002  # $0.002 per screenshot
    
    # Minimum overage charge (to prevent abuse)
    MINIMUM_OVERAGE_CHARGE = 5.00  # $5 minimum
    
    # Premium tier doesn't have overages (unlimited)
    TIERS_WITH_OVERAGES = [PricingTier.FREE, PricingTier.PRO, PricingTier.BUSINESS]
    
    # ========================================
    # STRIPE CONFIGURATION
    # ========================================
    
    STRIPE_PRICE_IDS = {
        PricingTier.PRO: {
            "monthly": "price_pro_monthly_49",
            "yearly": "price_pro_yearly_490"
        },
        PricingTier.BUSINESS: {
            "monthly": "price_business_monthly_199",
            "yearly": "price_business_yearly_1990"
        },
        PricingTier.PREMIUM: {
            "monthly": "price_premium_monthly_499",
            "yearly": "price_premium_yearly_4990"
        }
    }
    
    STRIPE_PRODUCT_IDS = {
        PricingTier.PRO: "prod_pixelperfect_pro",
        PricingTier.BUSINESS: "prod_pixelperfect_business",
        PricingTier.PREMIUM: "prod_pixelperfect_premium"
    }
    
    # ========================================
    # FEATURE FLAGS BY TIER
    # ========================================
    
    FEATURES = {
        PricingTier.FREE: {
            "batch_processing": False,
            "webhooks": False,
            "change_detection": False,
            "priority_support": False,
            "priority_queue": False,
            "custom_delays": False,
            "element_removal": False,
            "dark_mode": False,
            "api_access": True,
            "dashboard_access": True,
            "white_label": False,
            "custom_integrations": False,
            "dedicated_support": False
        },
        PricingTier.PRO: {
            "batch_processing": True,
            "webhooks": False,
            "change_detection": False,
            "priority_support": True,
            "priority_queue": True,
            "custom_delays": True,
            "element_removal": True,
            "dark_mode": True,
            "api_access": True,
            "dashboard_access": True,
            "white_label": False,
            "custom_integrations": False,
            "dedicated_support": False
        },
        PricingTier.BUSINESS: {
            "batch_processing": True,
            "webhooks": True,
            "change_detection": True,
            "priority_support": True,
            "priority_queue": True,
            "custom_delays": True,
            "element_removal": True,
            "dark_mode": True,
            "api_access": True,
            "dashboard_access": True,
            "white_label": False,
            "custom_integrations": False,
            "dedicated_support": False
        },
        PricingTier.PREMIUM: {
            "batch_processing": True,
            "webhooks": True,
            "change_detection": True,
            "priority_support": True,
            "priority_queue": True,
            "custom_delays": True,
            "element_removal": True,
            "dark_mode": True,
            "api_access": True,
            "dashboard_access": True,
            "white_label": True,
            "custom_integrations": True,
            "dedicated_support": True
        }
    }
    
    # ========================================
    # RATE LIMITING BY TIER
    # ========================================
    
    RATE_LIMITS = {
        PricingTier.FREE: {
            "requests_per_minute": 10,
            "requests_per_hour": 60,
            "requests_per_day": 200,
            "burst_allowance": 5
        },
        PricingTier.PRO: {
            "requests_per_minute": 100,
            "requests_per_hour": 1000,
            "requests_per_day": 10000,
            "burst_allowance": 50
        },
        PricingTier.BUSINESS: {
            "requests_per_minute": 500,
            "requests_per_hour": 10000,
            "requests_per_day": 100000,
            "burst_allowance": 200
        },
        PricingTier.PREMIUM: {
            "requests_per_minute": 2000,
            "requests_per_hour": 50000,
            "requests_per_day": 500000,
            "burst_allowance": 1000
        }
    }
    
    # ========================================
    # HELPER METHODS
    # ========================================
    
    @classmethod
    def get_tier_limits(cls, tier: PricingTier) -> Dict[str, Any]:
        """Get all limits for a tier"""
        return cls.LIMITS.get(tier, cls.LIMITS[PricingTier.FREE])
    
    @classmethod
    def get_tier_features(cls, tier: PricingTier) -> Dict[str, bool]:
        """Get feature flags for a tier"""
        return cls.FEATURES.get(tier, cls.FEATURES[PricingTier.FREE])
    
    @classmethod
    def get_tier_price(cls, tier: PricingTier, billing_cycle: str = "monthly") -> float:
        """Get price for a tier"""
        if tier == PricingTier.FREE:
            return 0
        
        tier_config = cls.PRICES.get(tier)
        if not tier_config:
            return 0
        
        if billing_cycle == "yearly":
            return tier_config.get("price_yearly", 0)
        return tier_config.get("price_monthly", 0)
    
    @classmethod
    def can_use_feature(cls, tier: PricingTier, feature: str) -> bool:
        """Check if a tier can use a specific feature"""
        tier_features = cls.get_tier_features(tier)
        return tier_features.get(feature, False)
    
    @classmethod
    def get_monthly_screenshot_limit(cls, tier: PricingTier) -> int:
        """
        Get monthly screenshot limit for a tier
        Returns -1 for unlimited (Premium tier)
        """
        limits = cls.get_tier_limits(tier)
        return limits.get("screenshots_per_month", 0)
    
    @classmethod
    def is_unlimited_tier(cls, tier: PricingTier) -> bool:
        """Check if tier has unlimited screenshots"""
        return cls.get_monthly_screenshot_limit(tier) == -1
    
    @classmethod
    def get_batch_size_limit(cls, tier: PricingTier) -> int:
        """Get batch processing limit for a tier"""
        limits = cls.get_tier_limits(tier)
        return limits.get("batch_size_max", 0)
    
    @classmethod
    def get_rate_limit(cls, tier: PricingTier, period: str) -> int:
        """
        Get rate limit for a tier
        
        Args:
            tier: Pricing tier
            period: 'minute', 'hour', or 'day'
        
        Returns:
            Rate limit for the specified period
        """
        rate_limits = cls.RATE_LIMITS.get(tier, cls.RATE_LIMITS[PricingTier.FREE])
        return rate_limits.get(f"requests_per_{period}", 0)
    
    @classmethod
    def calculate_overage_cost(cls, screenshots_used: int, tier: PricingTier) -> float:
        """
        Calculate overage cost
        Premium tier has no overages (unlimited)
        
        Args:
            screenshots_used: Total screenshots used
            tier: Current pricing tier
        
        Returns:
            Overage cost in USD (0 for Premium)
        """
        # Premium tier has unlimited screenshots
        if cls.is_unlimited_tier(tier):
            return 0.0
        
        tier_limit = cls.get_monthly_screenshot_limit(tier)
        
        if screenshots_used <= tier_limit:
            return 0.0
        
        overage = screenshots_used - tier_limit
        cost = overage * cls.OVERAGE_PRICE_PER_SCREENSHOT
        
        # Apply minimum charge if there's any overage
        if cost > 0 and cost < cls.MINIMUM_OVERAGE_CHARGE:
            cost = cls.MINIMUM_OVERAGE_CHARGE
        
        return round(cost, 2)
    
    @classmethod
    def get_pricing_table(cls) -> Dict[str, Any]:
        """
        Get complete pricing table for display
        
        Returns formatted pricing data for frontend
        """
        return {
            "tiers": {
                "free": {
                    "name": cls.PRICES[PricingTier.FREE]["name"],
                    "price_monthly": cls.PRICES[PricingTier.FREE]["price_monthly"],
                    "description": cls.PRICES[PricingTier.FREE]["description"],
                    "features": cls.LIMITS[PricingTier.FREE]["features"],
                    "screenshots": cls.LIMITS[PricingTier.FREE]["screenshots_per_month"],
                    "badge": None
                },
                "pro": {
                    "name": cls.PRICES[PricingTier.PRO]["name"],
                    "price_monthly": cls.PRICES[PricingTier.PRO]["price_monthly"],
                    "price_yearly": cls.PRICES[PricingTier.PRO]["price_yearly"],
                    "description": cls.PRICES[PricingTier.PRO]["description"],
                    "features": cls.LIMITS[PricingTier.PRO]["features"],
                    "screenshots": cls.LIMITS[PricingTier.PRO]["screenshots_per_month"],
                    "badge": cls.PRICES[PricingTier.PRO].get("badge")
                },
                "business": {
                    "name": cls.PRICES[PricingTier.BUSINESS]["name"],
                    "price_monthly": cls.PRICES[PricingTier.BUSINESS]["price_monthly"],
                    "price_yearly": cls.PRICES[PricingTier.BUSINESS]["price_yearly"],
                    "description": cls.PRICES[PricingTier.BUSINESS]["description"],
                    "features": cls.LIMITS[PricingTier.BUSINESS]["features"],
                    "screenshots": cls.LIMITS[PricingTier.BUSINESS]["screenshots_per_month"],
                    "badge": None
                },
                "premium": {
                    "name": cls.PRICES[PricingTier.PREMIUM]["name"],
                    "price_monthly": cls.PRICES[PricingTier.PREMIUM]["price_monthly"],
                    "price_yearly": cls.PRICES[PricingTier.PREMIUM]["price_yearly"],
                    "description": cls.PRICES[PricingTier.PREMIUM]["description"],
                    "features": cls.LIMITS[PricingTier.PREMIUM]["features"],
                    "screenshots": "Unlimited",  # Special handling for unlimited
                    "badge": None
                }
            },
            "overage": {
                "price_per_screenshot": cls.OVERAGE_PRICE_PER_SCREENSHOT,
                "minimum_charge": cls.MINIMUM_OVERAGE_CHARGE,
                "applies_to": ["free", "pro", "business"]  # Premium excluded
            }
        }


# ========================================
# USAGE TRACKING (UPDATED FOR PREMIUM)
# ========================================

class UsageTracker:
    """Track and enforce usage limits"""
    
    def __init__(self, db_session):
        self.db = db_session
    
    def can_use_screenshot(self, user, tier: PricingTier) -> tuple[bool, str]:
        """
        Check if user can create a screenshot
        Premium tier has unlimited screenshots (only rate limits apply)
        
        Returns:
            (can_use: bool, message: str)
        """
        from datetime import datetime, timedelta
        from sqlalchemy import func
        from models import Screenshot
        
        limits = PricingConfig.get_tier_limits(tier)
        
        # Premium tier: Skip monthly limits (unlimited)
        if not PricingConfig.is_unlimited_tier(tier):
            # Get usage for current month
            start_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            
            monthly_count = self.db.query(func.count(Screenshot.id)).filter(
                Screenshot.user_id == user.id,
                Screenshot.created_at >= start_of_month
            ).scalar()
            
            monthly_limit = limits["screenshots_per_month"]
            
            if monthly_count >= monthly_limit:
                return False, f"Monthly limit reached ({monthly_limit} screenshots). Upgrade your plan or wait for next month."
        else:
            # Premium tier - no monthly limit
            start_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            monthly_count = self.db.query(func.count(Screenshot.id)).filter(
                Screenshot.user_id == user.id,
                Screenshot.created_at >= start_of_month
            ).scalar()
        
        # Check daily limit (applies to all tiers, including Premium)
        start_of_day = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        
        daily_count = self.db.query(func.count(Screenshot.id)).filter(
            Screenshot.user_id == user.id,
            Screenshot.created_at >= start_of_day
        ).scalar()
        
        daily_limit = limits["screenshots_per_day"]
        
        if daily_count >= daily_limit:
            return False, f"Daily limit reached ({daily_limit} screenshots). Try again tomorrow."
        
        # Check hourly limit (rate limiting - applies to all tiers)
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        
        hourly_count = self.db.query(func.count(Screenshot.id)).filter(
            Screenshot.user_id == user.id,
            Screenshot.created_at >= one_hour_ago
        ).scalar()
        
        hourly_limit = limits["screenshots_per_hour"]
        
        if hourly_count >= hourly_limit:
            return False, f"Hourly rate limit reached ({hourly_limit} screenshots/hour). Please slow down."
        
        # Success message
        if PricingConfig.is_unlimited_tier(tier):
            return True, f"Usage: {monthly_count + 1} this month (unlimited plan)"
        else:
            monthly_limit = limits["screenshots_per_month"]
            return True, f"Usage: {monthly_count + 1}/{monthly_limit} this month"
    
    def get_usage_stats(self, user, tier: PricingTier) -> Dict[str, Any]:
        """Get detailed usage statistics for a user"""
        from datetime import datetime
        from sqlalchemy import func
        from models import Screenshot
        
        limits = PricingConfig.get_tier_limits(tier)
        
        # Monthly usage
        start_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        monthly_count = self.db.query(func.count(Screenshot.id)).filter(
            Screenshot.user_id == user.id,
            Screenshot.created_at >= start_of_month
        ).scalar()
        
        # Daily usage
        start_of_day = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        daily_count = self.db.query(func.count(Screenshot.id)).filter(
            Screenshot.user_id == user.id,
            Screenshot.created_at >= start_of_day
        ).scalar()
        
        # Total usage
        total_count = self.db.query(func.count(Screenshot.id)).filter(
            Screenshot.user_id == user.id
        ).scalar()
        
        # Handle unlimited tier
        monthly_limit = limits["screenshots_per_month"]
        is_unlimited = PricingConfig.is_unlimited_tier(tier)
        
        return {
            "monthly": {
                "used": monthly_count,
                "limit": "unlimited" if is_unlimited else monthly_limit,
                "percentage": 0 if is_unlimited else round((monthly_count / monthly_limit) * 100, 1)
            },
            "daily": {
                "used": daily_count,
                "limit": limits["screenshots_per_day"],
                "percentage": round((daily_count / limits["screenshots_per_day"]) * 100, 1)
            },
            "total": {
                "screenshots": total_count
            },
            "tier": {
                "name": tier.value,
                "is_unlimited": is_unlimited,
                "features": PricingConfig.get_tier_features(tier)
            }
        }