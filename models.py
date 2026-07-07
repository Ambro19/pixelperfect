# ============================================================================
# DATABASE MODELS - PixelPerfect Screenshot API
# File: backend/models.py
# Author: OneTechly
# Updated: July 2026
# ============================================================================
# PRODUCTION READY
#
# ✅ FIX (Jul 2026 — Tier Limits Source of Truth):
#   get_tier_limits() now reads ALL limit values from environment variables
#   with hardcoded fallbacks matching the documented defaults.
#   Root cause: business.batch_requests was hardcoded as 500 in this file,
#   while batch.py defines TIER_BATCH_LIMITS["business"] = 200 and .env
#   defines BUSINESS_BATCH_LIMIT=200. Three files had three different values.
#   Fix: .env is now the single source of truth for all tier limits.
#   Changing a limit now requires editing .env only — no code change needed.
#
# ✅ FIX (Mar 2026): SQLite WAL journal mode, FK enforcement, NORMAL sync
# ✅ FIX (Mar 2026): Added BatchJob model — fixes frozen batch counter
# ✅ FIX (Apr 2026): Added BatchJob.urls_json — enables job reconstruction
#     after server restart.
# ✅ FIX (Apr 2026): Added Screenshot.batch_job_id — links screenshots to
#     parent BatchJob. Required by _reconstruct_job_from_db() in batch.py.
# ✅ FIX (Apr 2026): add_missing_columns() adds urls_json and batch_job_id.
# ✅ NEW (May 2026 — Phase 1): TIER_FEATURES dict + has_feature() added.
# ============================================================================

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, sessionmaker

# ============================================================================
# DATABASE CONFIGURATION
# ============================================================================

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./pixelperfect.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg2" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

_IS_SQLITE = DATABASE_URL.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _IS_SQLITE else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

if _IS_SQLITE:
    @event.listens_for(Engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ============================================================================
# USER MODEL
# ============================================================================

class User(Base):
    __tablename__ = "users"

    id              = Column(Integer,     primary_key=True, index=True)
    username        = Column(String(50),  unique=True, index=True, nullable=False)
    email           = Column(String(100), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)

    stripe_customer_id = Column(String(100), unique=True, nullable=True)

    subscription_tier = Column(String(20), default="free", nullable=False)

    stripe_subscription_status = Column(String(20), nullable=True)
    subscription_status        = Column(String(20), default="active", nullable=True)

    subscription_id = Column(String(100), unique=True, nullable=True)

    subscription_expires_at = Column(DateTime, nullable=True)
    subscription_ends_at    = Column(DateTime, nullable=True)
    subscription_updated_at = Column(DateTime, nullable=True)

    usage_screenshots    = Column(Integer, default=0)
    usage_batch_requests = Column(Integer, default=0)
    usage_api_calls      = Column(Integer, default=0)
    usage_reset_at       = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    is_active  = Column(Boolean,  default=True,            nullable=False)

    __table_args__ = (
        Index("idx_user_email",    "email"),
        Index("idx_user_username", "username"),
        Index("idx_user_stripe",   "stripe_customer_id"),
        Index("idx_user_tier",     "subscription_tier"),
    )


# ============================================================================
# API KEY MODEL
# ============================================================================

class ApiKey(Base):
    __tablename__ = "api_keys"

    id      = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    key_hash   = Column(String(64), unique=True, nullable=False, index=True)
    key_prefix = Column(String(16), nullable=False)

    name         = Column(String(100), default="Default API Key", nullable=False)
    is_active    = Column(Boolean,     default=True,              nullable=False)
    last_used_at = Column(DateTime,    nullable=True)
    created_at   = Column(DateTime,    default=datetime.utcnow,  nullable=False)

    __table_args__ = (
        Index("idx_api_key_hash",   "key_hash"),
        Index("idx_api_key_user",   "user_id"),
        Index("idx_api_key_active", "is_active"),
    )


# ============================================================================
# SCREENSHOT MODEL
# ============================================================================

class Screenshot(Base):
    __tablename__ = "screenshots"

    id = Column(String, primary_key=True, index=True, default=lambda: str(uuid4()))

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    url = Column(Text, nullable=False)

    width     = Column(Integer,    nullable=False, default=1920)
    height    = Column(Integer,    nullable=False, default=1080)
    full_page = Column(Boolean,    nullable=True)
    format    = Column(String(10), nullable=False, default="png")
    quality   = Column(Integer,    nullable=True)

    size_bytes = Column(Integer, nullable=False, default=0)

    storage_url = Column(Text,   nullable=False, default="")
    storage_key = Column(String, nullable=True)

    processing_time_ms = Column(Float, nullable=True)

    status        = Column(String, nullable=True, default="completed")
    error_message = Column(Text,   nullable=True)

    dark_mode       = Column(Boolean, nullable=True)
    delay_seconds   = Column(Integer, nullable=True)
    remove_elements = Column(Text,    nullable=True)

    created_at = Column(DateTime, nullable=True, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)

    is_baseline              = Column(Boolean, nullable=True)
    baseline_screenshot_id   = Column(String, ForeignKey("screenshots.id"), nullable=True)
    difference_percentage    = Column(Float,   nullable=True)
    has_changes              = Column(Boolean, nullable=True)

    screenshot_path = Column(Text, nullable=True)

    batch_job_id = Column(
        String(32),
        ForeignKey("batch_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    __table_args__ = (
        Index("idx_screenshot_user",      "user_id"),
        Index("idx_screenshot_created",   "created_at"),
        Index("idx_screenshot_status",    "status"),
        Index("idx_screenshot_format",    "format"),
        Index("idx_screenshot_batch_job", "batch_job_id"),
    )


# ============================================================================
# BATCH JOB MODEL
# ============================================================================

class BatchJob(Base):
    __tablename__ = "batch_jobs"

    id = Column(String(32), primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    status    = Column(String(20),  nullable=False, default="queued")
    format    = Column(String(10),  nullable=False, default="png")
    width     = Column(Integer,     nullable=False, default=1920)
    height    = Column(Integer,     nullable=False, default=1080)
    full_page = Column(Boolean,     nullable=False, default=False)

    total_urls = Column(Integer, nullable=False, default=0)

    completed_count = Column(Integer, nullable=True, default=0)
    failed_count    = Column(Integer, nullable=True, default=0)

    urls_json = Column(Text, nullable=True)

    created_at   = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_batch_job_user",    "user_id"),
        Index("idx_batch_job_created", "created_at"),
        Index("idx_batch_job_status",  "status"),
    )


# ============================================================================
# SUBSCRIPTION MODEL
# ============================================================================

class Subscription(Base):
    __tablename__ = "subscriptions"

    id      = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    stripe_subscription_id = Column(String(100), unique=True, nullable=False)
    stripe_customer_id     = Column(String(100), nullable=False)

    tier   = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False)

    current_period_start = Column(DateTime, nullable=True)
    current_period_end   = Column(DateTime, nullable=True)
    cancel_at_period_end = Column(Boolean,  default=False, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    __table_args__ = (
        Index("idx_subscription_user",   "user_id"),
        Index("idx_subscription_stripe", "stripe_subscription_id"),
    )


# ============================================================================
# TIER LIMITS CONFIGURATION
# ============================================================================
# ✅ FIX (Jul 2026): All limit values now read from environment variables.
#    .env is the single source of truth. Defaults match documented values.
#
#    Confirmed correct values from .env:
#      FREE_SCREENSHOTS_LIMIT=100    FREE_BATCH_LIMIT=0
#      PRO_SCREENSHOTS_LIMIT=5000    PRO_BATCH_LIMIT=50
#      BUSINESS_SCREENSHOTS_LIMIT=50000   BUSINESS_BATCH_LIMIT=200  ← was 500
#      PREMIUM_SCREENSHOTS_LIMIT=999999999  PREMIUM_BATCH_LIMIT=999999999
#
#    To change any limit: edit .env only. No code change required.
# ============================================================================

def _env_int(key: str, default: int) -> int:
    """Read an integer from env; fall back to default if missing or invalid."""
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_limit(key: str, default: int):
    """
    Read a limit from env. Returns 'unlimited' if the value is >= 999_999_999
    (the sentinel used in .env for Premium), otherwise returns the integer.
    """
    val = _env_int(key, default)
    return "unlimited" if val >= 999_999_999 else val


def get_tier_limits(tier: str) -> Dict[str, Any]:
    """
    Return usage limits for a subscription tier.
    All values are read from environment variables; the .env file is the
    single source of truth so limits can be changed without a code deploy.
    """
    tier = (tier or "free").lower()

    limits: Dict[str, Dict[str, Any]] = {
        "free": {
            "screenshots":    _env_int("FREE_SCREENSHOTS_LIMIT",    100),
            "batch_requests": _env_int("FREE_BATCH_LIMIT",          0),
            "api_calls":      _env_int("FREE_API_CALLS_LIMIT",      1_000),
            "features": ["basic_customization", "community_support"],
        },
        "pro": {
            "screenshots":    _env_int("PRO_SCREENSHOTS_LIMIT",     5_000),
            "batch_requests": _env_int("PRO_BATCH_LIMIT",           50),
            "api_calls":      _env_int("PRO_API_CALLS_LIMIT",       10_000),
            "features": ["full_customization", "batch_processing", "priority_support"],
        },
        "business": {
            # ✅ FIXED: env default is 200 (was hardcoded 500).
            # Confirmed by BUSINESS_BATCH_LIMIT=200 in .env and
            # TIER_BATCH_LIMITS["business"] = 200 in batch.py.
            "screenshots":    _env_int("BUSINESS_SCREENSHOTS_LIMIT", 50_000),
            "batch_requests": _env_int("BUSINESS_BATCH_LIMIT",       200),
            "api_calls":      _env_int("BUSINESS_API_CALLS_LIMIT",   100_000),
            "features": ["webhooks", "change_detection", "dedicated_support", "batch_processing"],
        },
        "premium": {
            "screenshots":    _env_limit("PREMIUM_SCREENSHOTS_LIMIT", 999_999_999),
            "batch_requests": _env_limit("PREMIUM_BATCH_LIMIT",       999_999_999),
            "api_calls":      _env_limit("PREMIUM_API_CALLS_LIMIT",   999_999_999),
            "features": ["white_label", "custom_sla", "account_manager", "webhooks", "change_detection"],
        },
    }

    return limits.get(tier, limits["free"])


# ============================================================================
# ✅ NEW (May 2026 — Phase 1): TIER_FEATURES + has_feature()
# ============================================================================

TIER_FEATURES: Dict[str, Dict[str, bool]] = {
    "free": {
        "custom_js":         False,
        "device_emulation":  False,
        "element_selection": False,
        "webhooks":          False,
        "white_label":       False,
        "pdf":               False,
    },
    "pro": {
        "custom_js":         True,
        "device_emulation":  True,
        "element_selection": False,
        "webhooks":          False,
        "white_label":       False,
        "pdf":               False,
    },
    "business": {
        "custom_js":         True,
        "device_emulation":  True,
        "element_selection": True,
        "webhooks":          True,
        "white_label":       False,
        "pdf":               True,
    },
    "premium": {
        "custom_js":         True,
        "device_emulation":  True,
        "element_selection": True,
        "webhooks":          True,
        "white_label":       True,
        "pdf":               True,
    },
}


def has_feature(user: "User", feature_name: str) -> bool:
    """
    Return True if the user's subscription tier includes the named feature.
    This is the authoritative check — TIER_FEATURES is the only place
    feature-to-tier mappings live. Do not duplicate this logic elsewhere.
    """
    tier = (getattr(user, "subscription_tier", None) or "free").lower()
    return TIER_FEATURES.get(tier, {}).get(feature_name, False)


# ============================================================================
# USAGE RESET HELPER
# ============================================================================

def reset_monthly_usage(user: User, db: Session) -> None:
    """
    Reset user's monthly usage counters.
    Called when a Stripe billing cycle renews (via webhook_handler.py on
    invoice.paid events), NOT on the calendar 1st of the month.
    The reset_at timestamp is set to 30 days from now as a rolling fallback
    for users who have no active Stripe subscription.
    """
    user.usage_screenshots    = 0
    user.usage_batch_requests = 0
    user.usage_api_calls      = 0
    user.usage_reset_at       = datetime.utcnow() + timedelta(days=30)
    db.commit()


# ============================================================================
# DATABASE INITIALIZATION
# ============================================================================

def initialize_database() -> None:
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created successfully")


# ============================================================================
# MIGRATION HELPER
# ============================================================================

def add_missing_columns() -> None:
    """
    Add columns that are new in this version to existing live databases.
    Idempotent — safe to call on every startup.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    is_sqlite = _IS_SQLITE

    user_cols = [col["name"] for col in inspector.get_columns("users")]
    with engine.begin() as conn:
        if "stripe_subscription_status" not in user_cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN stripe_subscription_status VARCHAR(20)"
            ))
            print("✅ Added users.stripe_subscription_status")

        if "subscription_expires_at" not in user_cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN subscription_expires_at TIMESTAMP"
            ))
            print("✅ Added users.subscription_expires_at")

        if "subscription_updated_at" not in user_cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN subscription_updated_at TIMESTAMP"
            ))
            print("✅ Added users.subscription_updated_at")

    try:
        batch_cols = [col["name"] for col in inspector.get_columns("batch_jobs")]
        with engine.begin() as conn:
            if "urls_json" not in batch_cols:
                conn.execute(text("ALTER TABLE batch_jobs ADD COLUMN urls_json TEXT"))
                print("✅ Added batch_jobs.urls_json")
    except Exception as e:
        print(f"ℹ️  batch_jobs migration skipped: {e}")

    try:
        ss_cols = [col["name"] for col in inspector.get_columns("screenshots")]
        with engine.begin() as conn:
            if "batch_job_id" not in ss_cols:
                if is_sqlite:
                    conn.execute(text(
                        "ALTER TABLE screenshots ADD COLUMN batch_job_id VARCHAR(32)"
                    ))
                else:
                    conn.execute(text(
                        "ALTER TABLE screenshots "
                        "ADD COLUMN batch_job_id VARCHAR(32) "
                        "REFERENCES batch_jobs(id) ON DELETE SET NULL"
                    ))
                print("✅ Added screenshots.batch_job_id")
    except Exception as e:
        print(f"ℹ️  screenshots migration skipped: {e}")

    print("✅ DB migrations completed")

# ===== END OF models.py ======================================================


# # ============================================================================
# # DATABASE MODELS - PixelPerfect Screenshot API
# # File: backend/models.py
# # Author: OneTechly
# # Updated: May 2026
# # ============================================================================
# # PRODUCTION READY
# #
# # ✅ FIX (Mar 2026): SQLite WAL journal mode, FK enforcement, NORMAL sync
# # ✅ FIX (Mar 2026): Added BatchJob model — fixes frozen batch counter
# # ✅ FIX (Apr 2026): Added BatchJob.urls_json — enables job reconstruction
# #     after server restart. batch.py reads this column to know which URLs
# #     were in the job so it can rebuild per-item state from DB.
# # ✅ FIX (Apr 2026): Added Screenshot.batch_job_id — links every screenshot
# #     captured inside a batch back to its parent BatchJob. Required by
# #     _reconstruct_job_from_db() in batch.py to recover completed URLs.
# # ✅ FIX (Apr 2026): add_missing_columns() now adds urls_json and
# #     batch_job_id to existing live DBs that predate these columns.
# # ✅ NEW (May 2026 — Phase 1): TIER_FEATURES dict + has_feature() added
# #     after get_tier_limits(). These are the single source of truth for all
# #     advanced-feature tier gating used by routers/screenshot.py.
# # ============================================================================

# from __future__ import annotations

# import os
# from datetime import datetime, timedelta
# from typing import Any, Dict, Optional
# from uuid import uuid4

# from sqlalchemy import (
#     Boolean,
#     Column,
#     DateTime,
#     Float,
#     ForeignKey,
#     Index,
#     Integer,
#     String,
#     Text,
#     create_engine,
#     event,
# )
# from sqlalchemy.engine import Engine
# from sqlalchemy.ext.declarative import declarative_base
# from sqlalchemy.orm import Session, sessionmaker

# # ============================================================================
# # DATABASE CONFIGURATION
# # ============================================================================

# DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./pixelperfect.db")

# # PostgreSQL URL normalization (Render-friendly)
# if DATABASE_URL.startswith("postgres://"):
#     DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
# elif DATABASE_URL.startswith("postgresql://") and "+psycopg2" not in DATABASE_URL:
#     DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

# _IS_SQLITE = DATABASE_URL.startswith("sqlite")

# _connect_args = {"check_same_thread": False} if _IS_SQLITE else {}

# engine = create_engine(
#     DATABASE_URL,
#     connect_args=_connect_args,
#     pool_pre_ping=True,
#     future=True,
# )

# # ============================================================================
# # SQLite reliability improvements (dev only — no-op on PostgreSQL)
# # ============================================================================
# if _IS_SQLITE:
#     @event.listens_for(Engine, "connect")
#     def _set_sqlite_pragmas(dbapi_connection, connection_record):
#         cursor = dbapi_connection.cursor()
#         cursor.execute("PRAGMA foreign_keys=ON;")
#         cursor.execute("PRAGMA journal_mode=WAL;")
#         cursor.execute("PRAGMA synchronous=NORMAL;")
#         cursor.close()

# SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
# Base = declarative_base()

# # ============================================================================
# # DATABASE DEPENDENCY
# # ============================================================================

# def get_db():
#     """Database session dependency for FastAPI"""
#     db = SessionLocal()
#     try:
#         yield db
#     finally:
#         db.close()

# # ============================================================================
# # USER MODEL
# # ============================================================================

# class User(Base):
#     """User account model"""
#     __tablename__ = "users"

#     id              = Column(Integer,     primary_key=True, index=True)
#     username        = Column(String(50),  unique=True, index=True, nullable=False)
#     email           = Column(String(100), unique=True, index=True, nullable=False)
#     hashed_password = Column(String(255), nullable=False)

#     # Stripe integration
#     stripe_customer_id = Column(String(100), unique=True, nullable=True)

#     # Subscription tier (primary field used everywhere)
#     subscription_tier = Column(String(20), default="free", nullable=False)

#     # Subscription status fields (for webhook_handler.py)
#     stripe_subscription_status = Column(String(20), nullable=True)
#     subscription_status        = Column(String(20), default="active", nullable=True)

#     # Subscription ID tracking
#     subscription_id = Column(String(100), unique=True, nullable=True)

#     # Subscription expiry tracking (for subscription_sync.py)
#     subscription_expires_at = Column(DateTime, nullable=True)
#     subscription_ends_at    = Column(DateTime, nullable=True)

#     # Subscription update tracking
#     subscription_updated_at = Column(DateTime, nullable=True)

#     # Usage tracking
#     usage_screenshots    = Column(Integer, default=0)
#     usage_batch_requests = Column(Integer, default=0)
#     usage_api_calls      = Column(Integer, default=0)
#     usage_reset_at       = Column(DateTime, nullable=True)

#     # Metadata
#     created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
#     is_active  = Column(Boolean,  default=True,            nullable=False)

#     __table_args__ = (
#         Index("idx_user_email",    "email"),
#         Index("idx_user_username", "username"),
#         Index("idx_user_stripe",   "stripe_customer_id"),
#         Index("idx_user_tier",     "subscription_tier"),
#     )

# # ============================================================================
# # API KEY MODEL
# # ============================================================================

# class ApiKey(Base):
#     """API Keys for programmatic access (stored as hashes — never plaintext)."""
#     __tablename__ = "api_keys"

#     id      = Column(Integer, primary_key=True, index=True)
#     user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

#     key_hash   = Column(String(64), unique=True, nullable=False, index=True)
#     key_prefix = Column(String(16), nullable=False)

#     name         = Column(String(100), default="Default API Key", nullable=False)
#     is_active    = Column(Boolean,     default=True,              nullable=False)
#     last_used_at = Column(DateTime,    nullable=True)
#     created_at   = Column(DateTime,    default=datetime.utcnow,  nullable=False)

#     __table_args__ = (
#         Index("idx_api_key_hash",   "key_hash"),
#         Index("idx_api_key_user",   "user_id"),
#         Index("idx_api_key_active", "is_active"),
#     )

# # ============================================================================
# # SCREENSHOT MODEL
# # ============================================================================

# class Screenshot(Base):
#     """Screenshot capture record."""
#     __tablename__ = "screenshots"

#     id = Column(String, primary_key=True, index=True, default=lambda: str(uuid4()))

#     user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

#     url = Column(Text, nullable=False)

#     width     = Column(Integer, nullable=False, default=1920)
#     height    = Column(Integer, nullable=False, default=1080)
#     full_page = Column(Boolean, nullable=True)
#     format    = Column(String(10), nullable=False, default="png")
#     quality   = Column(Integer, nullable=True)

#     size_bytes = Column(Integer, nullable=False, default=0)

#     storage_url = Column(Text,   nullable=False, default="")
#     storage_key = Column(String, nullable=True)

#     processing_time_ms = Column(Float, nullable=True)

#     status        = Column(String, nullable=True, default="completed")
#     error_message = Column(Text,   nullable=True)

#     dark_mode       = Column(Boolean, nullable=True)
#     delay_seconds   = Column(Integer, nullable=True)
#     remove_elements = Column(Text,    nullable=True)

#     created_at = Column(DateTime, nullable=True, default=datetime.utcnow)
#     expires_at = Column(DateTime, nullable=True)

#     is_baseline              = Column(Boolean, nullable=True)
#     baseline_screenshot_id   = Column(String, ForeignKey("screenshots.id"), nullable=True)
#     difference_percentage    = Column(Float,   nullable=True)
#     has_changes              = Column(Boolean, nullable=True)

#     screenshot_path = Column(Text, nullable=True)

#     # ── ✅ FIX (Apr 2026): batch_job_id ──────────────────────────────────────
#     # Links this screenshot back to the BatchJob that created it.
#     # Required by _reconstruct_job_from_db() in batch.py after a server restart
#     # to know which URLs were in the job so it can rebuild per-item state.
#     # NULL for screenshots captured outside of batch jobs (single captures).
#     batch_job_id = Column(
#         String(32),
#         ForeignKey("batch_jobs.id", ondelete="SET NULL"),
#         nullable=True,
#         index=True,
#     )

#     __table_args__ = (
#         Index("idx_screenshot_user",      "user_id"),
#         Index("idx_screenshot_created",   "created_at"),
#         Index("idx_screenshot_status",    "status"),
#         Index("idx_screenshot_format",    "format"),
#         Index("idx_screenshot_batch_job", "batch_job_id"),
#     )

# # ============================================================================
# # BATCH JOB MODEL
# # ============================================================================

# class BatchJob(Base):
#     """Batch screenshot job record — one row per submitted batch job."""
#     __tablename__ = "batch_jobs"

#     # Same hex job_id that batch.py generates: uuid4().hex[:16]
#     id = Column(String(32), primary_key=True, index=True)

#     user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

#     # Job-level metadata
#     status    = Column(String(20),  nullable=False, default="queued")
#     # 'queued' | 'processing' | 'completed' | 'partial' | 'failed' | 'cancelled'

#     format    = Column(String(10),  nullable=False, default="png")
#     width     = Column(Integer,     nullable=False, default=1920)
#     height    = Column(Integer,     nullable=False, default=1080)
#     full_page = Column(Boolean,     nullable=False, default=False)

#     # URL count — set at submission time
#     total_urls = Column(Integer, nullable=False, default=0)

#     # Result counts — updated when job finishes
#     completed_count = Column(Integer, nullable=True, default=0)
#     failed_count    = Column(Integer, nullable=True, default=0)

#     # ── ✅ FIX (Apr 2026): urls_json ─────────────────────────────────────────
#     # JSON array of the original submitted URLs, stored at submit time.
#     # Used by _reconstruct_job_from_db() in batch.py after a server restart.
#     urls_json = Column(Text, nullable=True)

#     # Timestamps
#     created_at   = Column(DateTime, nullable=False, default=datetime.utcnow)
#     completed_at = Column(DateTime, nullable=True)

#     __table_args__ = (
#         Index("idx_batch_job_user",    "user_id"),
#         Index("idx_batch_job_created", "created_at"),
#         Index("idx_batch_job_status",  "status"),
#     )

# # ============================================================================
# # SUBSCRIPTION MODEL
# # ============================================================================

# class Subscription(Base):
#     """Stripe subscription details"""
#     __tablename__ = "subscriptions"

#     id      = Column(Integer, primary_key=True, index=True)
#     user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

#     stripe_subscription_id = Column(String(100), unique=True, nullable=False)
#     stripe_customer_id     = Column(String(100), nullable=False)

#     tier   = Column(String(20), nullable=False)
#     status = Column(String(20), nullable=False)

#     current_period_start = Column(DateTime, nullable=True)
#     current_period_end   = Column(DateTime, nullable=True)
#     cancel_at_period_end = Column(Boolean,  default=False, nullable=False)

#     created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
#     updated_at = Column(
#         DateTime,
#         default=datetime.utcnow,
#         onupdate=datetime.utcnow,
#         nullable=False,
#     )

#     __table_args__ = (
#         Index("idx_subscription_user",   "user_id"),
#         Index("idx_subscription_stripe", "stripe_subscription_id"),
#     )

# # ============================================================================
# # TIER LIMITS CONFIGURATION
# # ============================================================================

# def get_tier_limits(tier: str) -> Dict[str, Any]:
#     """Get usage limits for subscription tier"""
#     tier = (tier or "free").lower()

#     limits = {
#         "free": {
#             "screenshots":    100,
#             "batch_requests": 0,
#             "api_calls":      1000,
#             "features":       ["basic_customization", "community_support"],
#         },
#         "pro": {
#             "screenshots":    5000,
#             "batch_requests": 50,
#             "api_calls":      10000,
#             "features":       ["full_customization", "batch_processing", "priority_support"],
#         },
#         "business": {
#             "screenshots":    50000,
#             "batch_requests": 500,
#             "api_calls":      100000,
#             "features":       ["webhooks", "change_detection", "dedicated_support", "batch_processing"],
#         },
#         "premium": {
#             "screenshots":    "unlimited",
#             "batch_requests": "unlimited",
#             "api_calls":      "unlimited",
#             "features":       ["white_label", "custom_sla", "account_manager", "webhooks", "change_detection"],
#         },
#     }

#     return limits.get(tier, limits["free"])

# # ============================================================================
# # ✅ NEW (May 2026 — Phase 1): TIER_FEATURES + has_feature()
# # ============================================================================
# # Single source of truth for advanced-feature tier gating.
# # routers/screenshot.py imports has_feature() and calls it instead of the
# # inline feature_access dict that existed in the old check_feature_access().
# #
# # Adding a new feature: add a key to every tier dict below, then use
# # has_feature(user, "new_feature") in the router. That's it.
# #
# # Phase alignment:
# #   Phase 1 (active):  custom_js, device_emulation
# #   Phase 2 (stub):    element_selection  ← gate exists, service not yet active
# #   Phase 3 (stub):    webhooks           ← gate exists, retry/HMAC in router
# #   Phase 4 (stub):    white_label        ← gate exists, implementation deferred
# #   Bonus Phase 1:     pdf                ← Business+, gated alongside Business features
# # ============================================================================

# TIER_FEATURES: Dict[str, Dict[str, bool]] = {
#     "free": {
#         "custom_js":         False,
#         "device_emulation":  False,
#         "element_selection": False,
#         "webhooks":          False,
#         "white_label":       False,
#         "pdf":               False,
#     },
#     "pro": {
#         "custom_js":         True,
#         "device_emulation":  True,
#         "element_selection": False,
#         "webhooks":          False,
#         "white_label":       False,
#         "pdf":               False,
#     },
#     "business": {
#         "custom_js":         True,
#         "device_emulation":  True,
#         "element_selection": True,
#         "webhooks":          True,
#         "white_label":       False,
#         "pdf":               True,
#     },
#     "premium": {
#         "custom_js":         True,
#         "device_emulation":  True,
#         "element_selection": True,
#         "webhooks":          True,
#         "white_label":       True,
#         "pdf":               True,
#     },
# }


# def has_feature(user: "User", feature_name: str) -> bool:
#     """
#     Return True if the user's subscription tier includes the named feature.

#     Usage:
#         from models import has_feature
#         if not has_feature(current_user, "custom_js"):
#             raise HTTPException(403, "Custom JS requires Pro+")

#     This is the authoritative check — TIER_FEATURES is the only place
#     feature-to-tier mappings live. Do not duplicate this logic elsewhere.
#     """
#     tier = (getattr(user, "subscription_tier", None) or "free").lower()
#     return TIER_FEATURES.get(tier, {}).get(feature_name, False)

# # ============================================================================
# # USAGE RESET HELPER
# # ============================================================================

# def reset_monthly_usage(user: User, db: Session) -> None:
#     """Reset user's monthly usage counters"""
#     user.usage_screenshots    = 0
#     user.usage_batch_requests = 0
#     user.usage_api_calls      = 0
#     user.usage_reset_at       = datetime.utcnow() + timedelta(days=30)
#     db.commit()

# # ============================================================================
# # DATABASE INITIALIZATION
# # ============================================================================

# def initialize_database() -> None:
#     """
#     Create all database tables.
#     Safe to call on every startup — create_all() is idempotent (skips
#     tables that already exist).
#     """
#     Base.metadata.create_all(bind=engine)
#     print("✅ Database tables created successfully")

# # ============================================================================
# # MIGRATION HELPER
# # ============================================================================

# def add_missing_columns() -> None:
#     """
#     Add columns that are new in this version to existing live databases.
#     Idempotent — safe to call on every startup; skips columns that already
#     exist. Called from db_migrations.py → run_startup_migrations().

#     Columns managed here:
#       users table:
#         - stripe_subscription_status
#         - subscription_expires_at
#         - subscription_updated_at

#       batch_jobs table:
#         - urls_json          ← NEW Apr 2026

#       screenshots table:
#         - batch_job_id       ← NEW Apr 2026
#     """
#     from sqlalchemy import inspect, text

#     inspector = inspect(engine)
#     is_sqlite = _IS_SQLITE

#     # ── users table ──────────────────────────────────────────────────────────
#     user_cols = [col["name"] for col in inspector.get_columns("users")]
#     with engine.begin() as conn:
#         if "stripe_subscription_status" not in user_cols:
#             conn.execute(text(
#                 "ALTER TABLE users ADD COLUMN stripe_subscription_status VARCHAR(20)"
#             ))
#             print("✅ Added users.stripe_subscription_status")

#         if "subscription_expires_at" not in user_cols:
#             conn.execute(text(
#                 "ALTER TABLE users ADD COLUMN subscription_expires_at TIMESTAMP"
#             ))
#             print("✅ Added users.subscription_expires_at")

#         if "subscription_updated_at" not in user_cols:
#             conn.execute(text(
#                 "ALTER TABLE users ADD COLUMN subscription_updated_at TIMESTAMP"
#             ))
#             print("✅ Added users.subscription_updated_at")

#     # ── batch_jobs table ─────────────────────────────────────────────────────
#     try:
#         batch_cols = [col["name"] for col in inspector.get_columns("batch_jobs")]
#         with engine.begin() as conn:
#             if "urls_json" not in batch_cols:
#                 conn.execute(text(
#                     "ALTER TABLE batch_jobs ADD COLUMN urls_json TEXT"
#                 ))
#                 print("✅ Added batch_jobs.urls_json")
#     except Exception as e:
#         print(f"ℹ️  batch_jobs migration skipped (table may be new): {e}")

#     # ── screenshots table ─────────────────────────────────────────────────────
#     try:
#         ss_cols = [col["name"] for col in inspector.get_columns("screenshots")]
#         with engine.begin() as conn:
#             if "batch_job_id" not in ss_cols:
#                 if is_sqlite:
#                     conn.execute(text(
#                         "ALTER TABLE screenshots ADD COLUMN batch_job_id VARCHAR(32)"
#                     ))
#                 else:
#                     conn.execute(text(
#                         "ALTER TABLE screenshots "
#                         "ADD COLUMN batch_job_id VARCHAR(32) "
#                         "REFERENCES batch_jobs(id) ON DELETE SET NULL"
#                     ))
#                 print("✅ Added screenshots.batch_job_id")
#     except Exception as e:
#         print(f"ℹ️  screenshots migration skipped: {e}")

#     print("✅ DB migrations completed")

# # ===== END OF models.py ======================================================

