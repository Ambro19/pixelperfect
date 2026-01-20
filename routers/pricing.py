# ========================================
# PRICING CONFIGURATION - PIXELPERFECT
# ========================================
# Production-ready pricing tiers and limits
# File: backend/config/pricing.py
# Author: OneTechly
# Created: January 2026

from enum import Enum
from typing import Dict, Any

class PricingTier(str, Enum):
    """Pricing tier enumeration"""
    FREE = "free"
    PRO = "pro"
    BUSINESS = "business"

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
                "Webhooks & change detection",
                "Dedicated support",
                "Batch processing (up to 100 URLs)",
                "Change detection & monitoring",
                "Webhook notifications",
                "99.9% uptime SLA",
                "Priority processing queue"
            ]
        }
    }
    
    # ========================================
    # PAY-AS-YOU-GO PRICING (Overages)
    # ========================================
    
    OVERAGE_PRICE_PER_SCREENSHOT = 0.002  # $0.002 per screenshot
    
    # Minimum overage charge (to prevent abuse)
    MINIMUM_OVERAGE_CHARGE = 5.00  # $5 minimum
    
    # ========================================
    # STRIPE CONFIGURATION
    # ========================================
    # You'll create these in Stripe Dashboard
    
    STRIPE_PRICE_IDS = {
        # Monthly subscriptions
        PricingTier.PRO: {
            "monthly": "price_pro_monthly_49",  # Replace with actual Stripe Price ID
            "yearly": "price_pro_yearly_490"    # Replace with actual Stripe Price ID
        },
        PricingTier.BUSINESS: {
            "monthly": "price_business_monthly_199",  # Replace with actual Stripe Price ID
            "yearly": "price_business_yearly_1990"    # Replace with actual Stripe Price ID
        }
    }
    
    # Stripe Product IDs
    STRIPE_PRODUCT_IDS = {
        PricingTier.PRO: "prod_pixelperfect_pro",        # Replace with actual Stripe Product ID
        PricingTier.BUSINESS: "prod_pixelperfect_business"  # Replace with actual Stripe Product ID
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
            "dashboard_access": True
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
            "dashboard_access": True
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
            "dashboard_access": True
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
        """Get monthly screenshot limit for a tier"""
        limits = cls.get_tier_limits(tier)
        return limits.get("screenshots_per_month", 0)
    
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
    def calculate_overage_cost(cls, screenshots_used: int, tier_limit: int) -> float:
        """
        Calculate overage cost
        
        Args:
            screenshots_used: Total screenshots used
            tier_limit: Tier's screenshot limit
        
        Returns:
            Overage cost in USD
        """
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
                }
            },
            "overage": {
                "price_per_screenshot": cls.OVERAGE_PRICE_PER_SCREENSHOT,
                "minimum_charge": cls.MINIMUM_OVERAGE_CHARGE
            }
        }


# ========================================
# USAGE TRACKING
# ========================================

class UsageTracker:
    """Track and enforce usage limits"""
    
    def __init__(self, db_session):
        self.db = db_session
    
    def can_use_screenshot(self, user, tier: PricingTier) -> tuple[bool, str]:
        """
        Check if user can create a screenshot
        
        Returns:
            (can_use: bool, message: str)
        """
        from datetime import datetime, timedelta
        from sqlalchemy import func
        from models import Screenshot
        
        limits = PricingConfig.get_tier_limits(tier)
        
        # Get usage for current month
        start_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        monthly_count = self.db.query(func.count(Screenshot.id)).filter(
            Screenshot.user_id == user.id,
            Screenshot.created_at >= start_of_month
        ).scalar()
        
        monthly_limit = limits["screenshots_per_month"]
        
        if monthly_count >= monthly_limit:
            return False, f"Monthly limit reached ({monthly_limit} screenshots). Upgrade your plan or wait for next month."
        
        # Check daily limit
        start_of_day = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        
        daily_count = self.db.query(func.count(Screenshot.id)).filter(
            Screenshot.user_id == user.id,
            Screenshot.created_at >= start_of_day
        ).scalar()
        
        daily_limit = limits["screenshots_per_day"]
        
        if daily_count >= daily_limit:
            return False, f"Daily limit reached ({daily_limit} screenshots). Try again tomorrow or upgrade your plan."
        
        # Check hourly limit (rate limiting)
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        
        hourly_count = self.db.query(func.count(Screenshot.id)).filter(
            Screenshot.user_id == user.id,
            Screenshot.created_at >= one_hour_ago
        ).scalar()
        
        hourly_limit = limits["screenshots_per_hour"]
        
        if hourly_count >= hourly_limit:
            return False, f"Hourly rate limit reached ({hourly_limit} screenshots/hour). Please slow down."
        
        return True, f"Usage: {monthly_count + 1}/{monthly_limit} this month"
    
    def get_usage_stats(self, user, tier: PricingTier) -> Dict[str, Any]:
        """Get detailed usage statistics for a user"""
        from datetime import datetime, timedelta
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
        
        return {
            "monthly": {
                "used": monthly_count,
                "limit": limits["screenshots_per_month"],
                "percentage": round((monthly_count / limits["screenshots_per_month"]) * 100, 1)
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
                "features": PricingConfig.get_tier_features(tier)
            }
        }