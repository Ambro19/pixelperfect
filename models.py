# backend/models.py
# PixelPerfect Screenshot API - Database Models
# Updated for screenshot functionality while keeping auth/subscription infrastructure
# UPDATED: January 2026 - Production-ready with batch processing limits

import os
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, DateTime, Boolean, Float, Text, ForeignKey,
    create_engine, Index
)
from sqlalchemy.orm import sessionmaker, relationship

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./pixelperfect.db")

engine = create_engine(
    DATABASE_URL,
    future=True,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

from sqlalchemy import event

if "sqlite" in DATABASE_URL:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)

# SQLAlchemy 2.0 compatible base with fallback
try:
    from sqlalchemy.orm import DeclarativeBase
    class Base(DeclarativeBase):
        pass
except Exception:  # SQLAlchemy < 2
    from sqlalchemy.ext.declarative import declarative_base
    Base = declarative_base()


# ============================================================================
# USER MODEL (Reused from YCD with minor updates)
# ============================================================================

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)

    # Authentication
    username = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)

    # Force password change flag
    must_change_password = Column(Boolean, nullable=False, default=False, server_default="0")

    # Subscription tier (free, starter, pro, business)
    subscription_tier = Column(String, default="free")
    subscription_status = Column(  # legacy column for compatibility
        String, nullable=False, default="inactive", server_default="inactive"
    )

    # Stripe integration
    stripe_customer_id = Column(String, nullable=True, unique=True)

    # ------------------------------------------------------------------
    # Stripe synchronization fields
    # ------------------------------------------------------------------
    stripe_subscription_status = Column(String, nullable=True)
    stripe_current_period_end = Column(Integer, nullable=True)
    stripe_current_period_end_dt = Column(DateTime, nullable=True)
    subscription_expires_at = Column(DateTime, nullable=True)
    subscription_updated_at = Column(DateTime, nullable=True)

    # ------------------------------------------------------------------
    # Usage tracking (UPDATED FOR SCREENSHOTS)
    # ------------------------------------------------------------------
    # Remove YouTube-specific fields, add screenshot tracking
    usage_screenshots = Column(Integer, default=0)  # NEW: Screenshot usage
    usage_batch_requests = Column(Integer, default=0)  # NEW: Batch request usage
    usage_api_calls = Column(Integer, default=0)  # NEW: Total API calls
    
    # Keep these for backwards compatibility during transition
    usage_clean_transcripts = Column(Integer, default=0)  # LEGACY: Will remove later
    usage_unclean_transcripts = Column(Integer, default=0)  # LEGACY: Will remove later
    usage_audio_downloads = Column(Integer, default=0)  # LEGACY: Will remove later
    usage_video_downloads = Column(Integer, default=0)  # LEGACY: Will remove later

    usage_reset_date = Column(DateTime, default=datetime.utcnow)
    usage_reset_at = Column(DateTime, nullable=True)

    # API Key for programmatic access (NEW)
    api_key = Column(String, nullable=True, unique=True, index=True)
    api_key_created_at = Column(DateTime, nullable=True)
    
    # Webhook URL for notifications (NEW)
    webhook_url = Column(String(500), nullable=True)

    # Relationships
    subscriptions = relationship("Subscription", back_populates="user")
    screenshots = relationship("Screenshot", back_populates="user", cascade="all, delete-orphan")
    
    # Keep transcript_downloads for now (backwards compatibility)
    transcript_downloads = relationship("TranscriptDownload", back_populates="user")


# ============================================================================
# SUBSCRIPTION MODEL (Reused from YCD - no changes needed)
# ============================================================================

class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    tier = Column(String, nullable=False)  # free, starter, pro, business
    status = Column(String, default="active")

    stripe_subscription_id = Column(String, nullable=True, unique=True)
    stripe_customer_id = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cancelled_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    extra_data = Column(Text, nullable=True)

    user = relationship("User", back_populates="subscriptions")


# ============================================================================
# SCREENSHOT MODEL (NEW FOR PIXELPERFECT)
# ============================================================================

class Screenshot(Base):
    """Screenshot model - stores metadata for captured screenshots"""
    __tablename__ = "screenshots"

    id = Column(String, primary_key=True)  # UUID
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Screenshot details
    url = Column(Text, nullable=False)  # Original URL
    width = Column(Integer, nullable=False)
    height = Column(Integer, nullable=False)
    full_page = Column(Boolean, default=False)
    format = Column(String(10), nullable=False)  # png, jpeg, webp
    quality = Column(Integer, nullable=True)  # JPEG quality (0-100)
    
    # File information
    size_bytes = Column(Integer, nullable=False)
    storage_url = Column(Text, nullable=False)  # R2/S3 URL
    storage_key = Column(String, nullable=True)  # S3 object key
    
    # Processing information
    processing_time_ms = Column(Float, nullable=True)  # Time to capture (milliseconds)
    status = Column(String, default="completed")  # completed, failed, processing
    error_message = Column(Text, nullable=True)
    
    # Advanced options (stored as flags/values)
    dark_mode = Column(Boolean, default=False)
    delay_seconds = Column(Integer, default=0)
    removed_elements = Column(Text, nullable=True)  # JSON array of CSS selectors
    
    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, nullable=True)  # Auto-delete after X days
    
    # Change detection (for monitoring features)
    is_baseline = Column(Boolean, default=False)
    baseline_screenshot_id = Column(String, ForeignKey("screenshots.id"), nullable=True)
    difference_percentage = Column(Float, nullable=True)
    has_changes = Column(Boolean, nullable=True)

    # Relationships
    user = relationship("User", back_populates="screenshots")
    
    # Composite indexes for common queries
    __table_args__ = (
        Index("ix_screenshots_user_created", "user_id", "created_at"),
        Index("ix_screenshots_user_url", "user_id", "url"),
        Index("ix_screenshots_status", "status"),
    )


# ============================================================================
# LEGACY MODEL (Keep for backwards compatibility during transition)
# ============================================================================

class TranscriptDownload(Base):
    """
    LEGACY: YouTube transcript download tracking
    Keep this table for now to avoid breaking existing users
    Will be removed in future version
    """
    __tablename__ = "transcript_downloads"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    youtube_id = Column(String, nullable=False, index=True)
    transcript_type = Column(String, nullable=False)

    quality = Column(String, nullable=True)
    file_format = Column(String, nullable=True)
    file_size = Column(Integer, nullable=True)
    file_path = Column(String, nullable=True)

    processing_time = Column(Float, nullable=True)
    status = Column(String, default="completed")
    language = Column(String, default="en")

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    error_message = Column(Text, nullable=True)

    user = relationship("User", back_populates="transcript_downloads")

    __table_args__ = (
        Index("ix_transcripts_user_created", "user_id", "created_at"),
    )


# ============================================================================
# DATABASE HELPERS
# ============================================================================

def get_db():
    """Dependency for FastAPI routes"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def initialize_database():
    """
    Create tables and apply SQLite compatibility patches
    Handles both fresh installs and migrations from YCD
    """
    try:
        db_path = DATABASE_URL.replace("sqlite:///", "").replace("./", "")
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)
            print(f"📁 Created database directory: {db_dir}")

        # Create all tables
        Base.metadata.create_all(bind=engine)

        # SQLite-specific migrations (add columns if missing)
        if DATABASE_URL.startswith("sqlite"):
            with engine.begin() as conn:
                # Get existing columns
                cols = conn.exec_driver_sql("PRAGMA table_info(users)").fetchall()
                colnames = {c[1] for c in cols}

                # --- Legacy fields (keep for backwards compatibility) ---
                if "subscription_status" not in colnames:
                    conn.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN subscription_status TEXT NOT NULL DEFAULT 'inactive'"
                    )

                if "must_change_password" not in colnames:
                    conn.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"
                    )

                # --- Stripe sync fields ---
                if "stripe_subscription_status" not in colnames:
                    conn.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN stripe_subscription_status TEXT"
                    )

                if "stripe_current_period_end" not in colnames:
                    conn.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN stripe_current_period_end INTEGER"
                    )

                if "stripe_current_period_end_dt" not in colnames:
                    conn.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN stripe_current_period_end_dt DATETIME"
                    )

                if "subscription_expires_at" not in colnames:
                    conn.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN subscription_expires_at DATETIME"
                    )

                if "subscription_updated_at" not in colnames:
                    conn.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN subscription_updated_at DATETIME"
                    )

                if "usage_reset_at" not in colnames:
                    conn.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN usage_reset_at DATETIME"
                    )

                # --- NEW: Screenshot usage fields ---
                if "usage_screenshots" not in colnames:
                    conn.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN usage_screenshots INTEGER DEFAULT 0"
                    )
                    conn.exec_driver_sql(
                        "UPDATE users SET usage_screenshots = 0 WHERE usage_screenshots IS NULL"
                    )

                if "usage_batch_requests" not in colnames:
                    conn.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN usage_batch_requests INTEGER DEFAULT 0"
                    )
                    conn.exec_driver_sql(
                        "UPDATE users SET usage_batch_requests = 0 WHERE usage_batch_requests IS NULL"
                    )

                if "usage_api_calls" not in colnames:
                    conn.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN usage_api_calls INTEGER DEFAULT 0"
                    )
                    conn.exec_driver_sql(
                        "UPDATE users SET usage_api_calls = 0 WHERE usage_api_calls IS NULL"
                    )

                # --- NEW: API Key fields ---
                if "api_key" not in colnames:
                    conn.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN api_key TEXT UNIQUE"
                    )

                if "api_key_created_at" not in colnames:
                    conn.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN api_key_created_at DATETIME"
                    )

                # --- NEW: Webhook URL ---
                if "webhook_url" not in colnames:
                    conn.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN webhook_url TEXT"
                    )

                # Backfill usage_reset_at if needed
                try:
                    conn.exec_driver_sql(
                        "UPDATE users SET usage_reset_at = usage_reset_date "
                        "WHERE usage_reset_at IS NULL AND usage_reset_date IS NOT NULL"
                    )
                except Exception:
                    pass

        print(f"✅ Database initialized successfully")
        print(f"📊 Tables created:")
        print(f"   - users (auth + subscriptions)")
        print(f"   - subscriptions (Stripe sync)")
        print(f"   - screenshots (NEW - PixelPerfect)")
        print(f"   - transcript_downloads (legacy - will remove later)")
        return True

    except Exception as e:
        print(f"❌ Error initializing database: {e}")
        raise


# ============================================================================
# USAGE LIMIT HELPERS (UPDATED FOR BATCH PROCESSING)
# ============================================================================

def get_tier_limits(tier: str) -> dict:
    """
    Get usage limits for a subscription tier
    
    PRODUCTION-READY - matches pricing.py and batch.py:
    - Free: 100 screenshots/month, NO batch processing
    - Pro: 5,000 screenshots/month ($49), 50 URLs per batch
    - Business: 50,000 screenshots/month ($199), 100 URLs per batch
    """
    limits = {
        "free": {
            "screenshots": 100,  # ✅ Matches pricing.py
            "batch_requests": 0,  # ✅ No batch processing for free tier
            "api_calls_per_minute": 10,
            "max_batch_size": 0,  # ✅ Critical: 0 means no batch processing
            "screenshot_retention_days": 7,
            "max_width": 1920,
            "max_height": 1080,
            "formats": ["png", "jpeg"],
            "webhooks": False,
            "change_detection": False,
        },
        "starter": {
            "screenshots": 1000,
            "batch_requests": 100,
            "api_calls_per_minute": 60,
            "max_batch_size": 25,
            "screenshot_retention_days": 30,
            "max_width": 3840,
            "max_height": 2160,
            "formats": ["png", "jpeg", "webp"],
            "webhooks": False,
            "change_detection": False,
        },
        "pro": {
            "screenshots": 5000,  # ✅ Matches pricing.py ($49/month)
            "batch_requests": 500,
            "api_calls_per_minute": 100,
            "max_batch_size": 50,  # ✅ Matches batch.py PRO_MAX_BATCH
            "screenshot_retention_days": 90,
            "max_width": 3840,
            "max_height": 2160,
            "formats": ["png", "jpeg", "webp"],
            "webhooks": True,
            "change_detection": True,
        },
        "business": {
            "screenshots": 50000,  # ✅ Matches pricing.py ($199/month)
            "batch_requests": 5000,
            "api_calls_per_minute": 500,
            "max_batch_size": 100,  # ✅ Matches batch.py BUSINESS_MAX_BATCH
            "screenshot_retention_days": 365,
            "max_width": 3840,
            "max_height": 2160,
            "formats": ["png", "jpeg", "webp", "pdf"],
            "webhooks": True,
            "change_detection": True,
            "priority_processing": True,
        },
    }
    return limits.get(tier.lower(), limits["free"])


def check_usage_limits(user: User, usage_type: str = "screenshots") -> tuple[bool, str]:
    """
    Check if user can perform an action based on their tier limits
    
    Returns: (can_use, message)
    """
    tier_limits = get_tier_limits(user.subscription_tier)
    
    if usage_type == "screenshots":
        current = user.usage_screenshots
        limit = tier_limits["screenshots"]
        
        if current >= limit:
            return False, f"Monthly screenshot limit reached ({limit}). Upgrade to increase limit."
        return True, f"Usage: {current}/{limit} screenshots this month"
    
    elif usage_type == "batch_requests":
        current = user.usage_batch_requests
        limit = tier_limits["batch_requests"]
        
        if current >= limit:
            return False, f"Monthly batch request limit reached ({limit}). Upgrade for more."
        return True, f"Usage: {current}/{limit} batch requests this month"
    
    return True, "OK"


def increment_usage(user: User, db, usage_type: str = "screenshots"):
    """Increment usage counter for user"""
    if usage_type == "screenshots":
        user.usage_screenshots += 1
    elif usage_type == "batch_requests":
        user.usage_batch_requests += 1
    
    user.usage_api_calls += 1
    db.commit()


def reset_monthly_usage(user: User, db):
    """Reset all usage counters for a user"""
    user.usage_screenshots = 0
    user.usage_batch_requests = 0
    user.usage_api_calls = 0
    user.usage_reset_at = datetime.utcnow()
    user.usage_reset_date = datetime.utcnow()  # Keep for backwards compatibility
    db.commit()
    print(f"✅ Usage reset for user {user.username}")