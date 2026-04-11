# ============================================================================
# DATABASE MODELS - PixelPerfect Screenshot API
# File: backend/models.py
# Author: OneTechly
# Updated: April 2026
# ============================================================================
# PRODUCTION READY
#
# ✅ FIX (Mar 2026): SQLite WAL journal mode, FK enforcement, NORMAL sync
# ✅ FIX (Mar 2026): Added BatchJob model — fixes frozen batch counter
# ✅ FIX (Apr 2026): Added BatchJob.urls_json — enables job reconstruction
#     after server restart. batch.py reads this column to know which URLs
#     were in the job so it can rebuild per-item state from DB.
# ✅ FIX (Apr 2026): Added Screenshot.batch_job_id — links every screenshot
#     captured inside a batch back to its parent BatchJob. Required by
#     _reconstruct_job_from_db() in batch.py to recover completed URLs.
# ✅ FIX (Apr 2026): add_missing_columns() now adds urls_json and
#     batch_job_id to existing live DBs that predate these columns.
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

# PostgreSQL URL normalization (Render-friendly)
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

# ============================================================================
# SQLite reliability improvements (dev only — no-op on PostgreSQL)
# ============================================================================
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

# ============================================================================
# DATABASE DEPENDENCY
# ============================================================================

def get_db():
    """Database session dependency for FastAPI"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ============================================================================
# USER MODEL
# ============================================================================

class User(Base):
    """User account model"""
    __tablename__ = "users"

    id              = Column(Integer,     primary_key=True, index=True)
    username        = Column(String(50),  unique=True, index=True, nullable=False)
    email           = Column(String(100), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)

    # Stripe integration
    stripe_customer_id = Column(String(100), unique=True, nullable=True)

    # Subscription tier (primary field used everywhere)
    subscription_tier = Column(String(20), default="free", nullable=False)

    # Subscription status fields (for webhook_handler.py)
    stripe_subscription_status = Column(String(20), nullable=True)
    subscription_status        = Column(String(20), default="active", nullable=True)

    # Subscription ID tracking
    subscription_id = Column(String(100), unique=True, nullable=True)

    # Subscription expiry tracking (for subscription_sync.py)
    subscription_expires_at = Column(DateTime, nullable=True)
    subscription_ends_at    = Column(DateTime, nullable=True)

    # Subscription update tracking
    subscription_updated_at = Column(DateTime, nullable=True)

    # Usage tracking
    usage_screenshots    = Column(Integer, default=0)
    usage_batch_requests = Column(Integer, default=0)
    usage_api_calls      = Column(Integer, default=0)
    usage_reset_at       = Column(DateTime, nullable=True)

    # Metadata
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
    """API Keys for programmatic access (stored as hashes — never plaintext)."""
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
    """Screenshot capture record."""
    __tablename__ = "screenshots"

    id = Column(String, primary_key=True, index=True, default=lambda: str(uuid4()))

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    url = Column(Text, nullable=False)

    width     = Column(Integer, nullable=False, default=1920)
    height    = Column(Integer, nullable=False, default=1080)
    full_page = Column(Boolean, nullable=True)
    format    = Column(String(10), nullable=False, default="png")
    quality   = Column(Integer, nullable=True)

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

    # ── ✅ FIX (Apr 2026): batch_job_id ──────────────────────────────────────
    # Links this screenshot back to the BatchJob that created it.
    # Required by _reconstruct_job_from_db() in batch.py so that after a
    # server restart it can find all completed screenshots for a given job
    # and rebuild the per-item state with their CDN URLs intact.
    # NULL for screenshots captured outside of batch jobs (single captures).
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
    """Batch screenshot job record — one row per submitted batch job."""
    __tablename__ = "batch_jobs"

    # Same hex job_id that batch.py generates: uuid4().hex[:16]
    id = Column(String(32), primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # Job-level metadata
    status    = Column(String(20),  nullable=False, default="queued")
    # 'queued' | 'processing' | 'completed' | 'partial' | 'failed' | 'cancelled'

    format    = Column(String(10),  nullable=False, default="png")
    width     = Column(Integer,     nullable=False, default=1920)
    height    = Column(Integer,     nullable=False, default=1080)
    full_page = Column(Boolean,     nullable=False, default=False)

    # URL count — set at submission time
    total_urls = Column(Integer, nullable=False, default=0)

    # Result counts — updated when job finishes
    completed_count = Column(Integer, nullable=True, default=0)
    failed_count    = Column(Integer, nullable=True, default=0)

    # ── ✅ FIX (Apr 2026): urls_json ─────────────────────────────────────────
    # JSON array of the original submitted URLs, stored at submit time.
    # Format: '["https://example.com", "https://other.com", ...]'
    # Used by _reconstruct_job_from_db() in batch.py after a server restart
    # to know which URLs were in the job and in what order, so it can rebuild
    # the full per-item job dict from DB records.
    # Without this column, restart = total loss of all job item state.
    urls_json = Column(Text, nullable=True)

    # Timestamps
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
    """Stripe subscription details"""
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

def get_tier_limits(tier: str) -> Dict[str, Any]:
    """Get usage limits for subscription tier"""
    tier = (tier or "free").lower()

    limits = {
        "free": {
            "screenshots":    100,
            "batch_requests": 0,
            "api_calls":      1000,
            "features":       ["basic_customization", "community_support"],
        },
        "pro": {
            "screenshots":    5000,
            "batch_requests": 50,
            "api_calls":      10000,
            "features":       ["full_customization", "batch_processing", "priority_support"],
        },
        "business": {
            "screenshots":    50000,
            "batch_requests": 500,
            "api_calls":      100000,
            "features":       ["webhooks", "change_detection", "dedicated_support", "batch_processing"],
        },
        "premium": {
            "screenshots":    "unlimited",
            "batch_requests": "unlimited",
            "api_calls":      "unlimited",
            "features":       ["white_label", "custom_sla", "account_manager", "webhooks", "change_detection"],
        },
    }

    return limits.get(tier, limits["free"])

# ============================================================================
# USAGE RESET HELPER
# ============================================================================

def reset_monthly_usage(user: User, db: Session) -> None:
    """Reset user's monthly usage counters"""
    user.usage_screenshots    = 0
    user.usage_batch_requests = 0
    user.usage_api_calls      = 0
    user.usage_reset_at       = datetime.utcnow() + timedelta(days=30)
    db.commit()

# ============================================================================
# DATABASE INITIALIZATION
# ============================================================================

def initialize_database() -> None:
    """
    Create all database tables.
    Safe to call on every startup — create_all() is idempotent (skips
    tables that already exist).
    """
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created successfully")

# ============================================================================
# MIGRATION HELPER
# ============================================================================

def add_missing_columns() -> None:
    """
    Add columns that are new in this version to existing live databases.
    Idempotent — safe to call on every startup; skips columns that already
    exist. Called from db_migrations.py → run_startup_migrations().

    Columns managed here:
      users table:
        - stripe_subscription_status
        - subscription_expires_at
        - subscription_updated_at

      batch_jobs table:
        - urls_json          ← NEW Apr 2026

      screenshots table:
        - batch_job_id       ← NEW Apr 2026
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    is_sqlite = _IS_SQLITE

    # ── users table ──────────────────────────────────────────────────────────
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

    # ── batch_jobs table ─────────────────────────────────────────────────────
    # The batch_jobs table may not exist yet on very old deployments;
    # initialize_database() (called before this) will have created it,
    # but if it existed without urls_json we add it here.
    try:
        batch_cols = [col["name"] for col in inspector.get_columns("batch_jobs")]
        with engine.begin() as conn:
            if "urls_json" not in batch_cols:
                # TEXT / CLOB — works on both SQLite and PostgreSQL
                conn.execute(text(
                    "ALTER TABLE batch_jobs ADD COLUMN urls_json TEXT"
                ))
                print("✅ Added batch_jobs.urls_json")
    except Exception as e:
        # Table may not exist yet (first deploy) — initialize_database() handles it
        print(f"ℹ️  batch_jobs migration skipped (table may be new): {e}")

    # ── screenshots table ─────────────────────────────────────────────────────
    try:
        ss_cols = [col["name"] for col in inspector.get_columns("screenshots")]
        with engine.begin() as conn:
            if "batch_job_id" not in ss_cols:
                if is_sqlite:
                    # SQLite: ADD COLUMN supports nullable columns; FK constraint
                    # syntax is accepted but not enforced (SQLite limitation).
                    conn.execute(text(
                        "ALTER TABLE screenshots ADD COLUMN batch_job_id VARCHAR(32)"
                    ))
                else:
                    # PostgreSQL: full FK constraint
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

# # =============================================================================================
# # ============================================================================
# # DATABASE MODELS - PixelPerfect Screenshot API (FIXED)
# # File: backend/models.py
# # Author: OneTechly
# # Updated: March 2026 - SQLite WAL + FK pragma reliability improvements
# # ============================================================================
# # PRODUCTION READY
# # Added missing subscription fields for webhook_handler.py
# # Aligned with subscription_sync.py requirements
# # FIX (Mar 2026): SQLite WAL journal mode, FK enforcement, NORMAL sync
# #   Prevents "it works then randomly breaks" SQLite dev behavior,
# #   especially with concurrent FastAPI async requests and WAL files.
# #
# # ✅ FIX (Mar 2026 — Batch counter frozen at wrong value):
# #   Added BatchJob model. Without it, main.py's subscription_status endpoint
# #   silently fell back to user.usage_batch_requests which batch.py never
# #   incremented — so the dashboard Batch Requests counter never updated.
# #   Fix: BatchJob table now records every submitted batch job so
# #   db.query(BatchJob).filter(...).count() returns the correct number.
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

#     id = Column(Integer, primary_key=True, index=True)
#     username = Column(String(50), unique=True, index=True, nullable=False)
#     email = Column(String(100), unique=True, index=True, nullable=False)
#     hashed_password = Column(String(255), nullable=False)

#     # Stripe integration
#     stripe_customer_id = Column(String(100), unique=True, nullable=True)

#     # Subscription tier (primary field used everywhere)
#     subscription_tier = Column(String(20), default="free", nullable=False)

#     # Subscription status fields (for webhook_handler.py)
#     stripe_subscription_status = Column(String(20), nullable=True)
#     subscription_status = Column(String(20), default="active", nullable=True)

#     # Subscription ID tracking
#     subscription_id = Column(String(100), unique=True, nullable=True)

#     # Subscription expiry tracking (for subscription_sync.py)
#     subscription_expires_at = Column(DateTime, nullable=True)
#     subscription_ends_at = Column(DateTime, nullable=True)

#     # Subscription update tracking
#     subscription_updated_at = Column(DateTime, nullable=True)

#     # Usage tracking
#     usage_screenshots = Column(Integer, default=0)
#     usage_batch_requests = Column(Integer, default=0)
#     usage_api_calls = Column(Integer, default=0)
#     usage_reset_at = Column(DateTime, nullable=True)

#     # Metadata
#     created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
#     is_active = Column(Boolean, default=True, nullable=False)

#     __table_args__ = (
#         Index("idx_user_email", "email"),
#         Index("idx_user_username", "username"),
#         Index("idx_user_stripe", "stripe_customer_id"),
#         Index("idx_user_tier", "subscription_tier"),
#     )

# # ============================================================================
# # API KEY MODEL
# # ============================================================================

# class ApiKey(Base):
#     """
#     API Keys for programmatic access.
#     Keys are stored as hashes (never store plaintext).
#     """
#     __tablename__ = "api_keys"

#     id = Column(Integer, primary_key=True, index=True)
#     user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

#     key_hash = Column(String(64), unique=True, nullable=False, index=True)
#     key_prefix = Column(String(16), nullable=False)

#     name = Column(String(100), default="Default API Key", nullable=False)
#     is_active = Column(Boolean, default=True, nullable=False)
#     last_used_at = Column(DateTime, nullable=True)
#     created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

#     __table_args__ = (
#         Index("idx_api_key_hash", "key_hash"),
#         Index("idx_api_key_user", "user_id"),
#         Index("idx_api_key_active", "is_active"),
#     )

# # ============================================================================
# # SCREENSHOT MODEL (aligned to existing schema)
# # ============================================================================

# class Screenshot(Base):
#     """Screenshot capture record (matches existing SQLite schema)"""
#     __tablename__ = "screenshots"

#     id = Column(String, primary_key=True, index=True, default=lambda: str(uuid4()))

#     user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

#     url = Column(Text, nullable=False)

#     width = Column(Integer, nullable=False, default=1920)
#     height = Column(Integer, nullable=False, default=1080)

#     full_page = Column(Boolean, nullable=True)
#     format = Column(String(10), nullable=False, default="png")
#     quality = Column(Integer, nullable=True)

#     size_bytes = Column(Integer, nullable=False, default=0)

#     storage_url = Column(Text, nullable=False, default="")
#     storage_key = Column(String, nullable=True)

#     processing_time_ms = Column(Float, nullable=True)

#     status = Column(String, nullable=True, default="completed")
#     error_message = Column(Text, nullable=True)

#     dark_mode = Column(Boolean, nullable=True)
#     delay_seconds = Column(Integer, nullable=True)
#     remove_elements = Column(Text, nullable=True)

#     created_at = Column(DateTime, nullable=True, default=datetime.utcnow)
#     expires_at = Column(DateTime, nullable=True)

#     is_baseline = Column(Boolean, nullable=True)
#     baseline_screenshot_id = Column(String, ForeignKey("screenshots.id"), nullable=True)
#     difference_percentage = Column(Float, nullable=True)
#     has_changes = Column(Boolean, nullable=True)

#     screenshot_path = Column(Text, nullable=True)

#     __table_args__ = (
#         Index("idx_screenshot_user", "user_id"),
#         Index("idx_screenshot_created", "created_at"),
#         Index("idx_screenshot_status", "status"),
#         Index("idx_screenshot_format", "format"),
#     )

# # ============================================================================
# # BATCH JOB MODEL
# # ============================================================================
# # ✅ NEW: This model was missing, which caused the dashboard "Batch Requests"
# #    counter to always read from user.usage_batch_requests (never updated by
# #    batch.py) instead of from this table.
# #
# #    main.py subscription_status endpoint queries:
# #        db.query(BatchJob).filter(
# #            BatchJob.user_id == current_user.id,
# #            BatchJob.created_at >= period_start,
# #        ).count()
# #    That query now works correctly once batch.py writes a record here
# #    on every job submission.
# # ============================================================================

# class BatchJob(Base):
#     """Batch screenshot job record — one row per submitted batch job."""
#     __tablename__ = "batch_jobs"

#     # Use the same hex job_id that batch.py generates (uuid4().hex[:16])
#     id = Column(String(32), primary_key=True, index=True)

#     user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

#     # Job-level metadata
#     status = Column(String(20), nullable=False, default="queued")
#     # 'queued' | 'processing' | 'completed' | 'partial' | 'failed'

#     format = Column(String(10), nullable=False, default="png")
#     width = Column(Integer, nullable=False, default=1920)
#     height = Column(Integer, nullable=False, default=1080)
#     full_page = Column(Boolean, nullable=False, default=False)

#     # URL counts — set at submission time
#     total_urls = Column(Integer, nullable=False, default=0)

#     # Result counts — updated when job finishes
#     completed_count = Column(Integer, nullable=True, default=0)
#     failed_count = Column(Integer, nullable=True, default=0)

#     # Timestamps
#     created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
#     completed_at = Column(DateTime, nullable=True)

#     __table_args__ = (
#         Index("idx_batch_job_user", "user_id"),
#         Index("idx_batch_job_created", "created_at"),
#         Index("idx_batch_job_status", "status"),
#     )

# # ============================================================================
# # SUBSCRIPTION MODEL
# # ============================================================================

# class Subscription(Base):
#     """Stripe subscription details"""
#     __tablename__ = "subscriptions"

#     id = Column(Integer, primary_key=True, index=True)
#     user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

#     stripe_subscription_id = Column(String(100), unique=True, nullable=False)
#     stripe_customer_id = Column(String(100), nullable=False)

#     tier = Column(String(20), nullable=False)
#     status = Column(String(20), nullable=False)

#     current_period_start = Column(DateTime, nullable=True)
#     current_period_end = Column(DateTime, nullable=True)
#     cancel_at_period_end = Column(Boolean, default=False, nullable=False)

#     created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
#     updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

#     __table_args__ = (
#         Index("idx_subscription_user", "user_id"),
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
#             "screenshots": 100,
#             "batch_requests": 0,
#             "api_calls": 1000,
#             "features": ["basic_customization", "community_support"],
#         },
#         "pro": {
#             "screenshots": 5000,
#             "batch_requests": 50,
#             "api_calls": 10000,
#             "features": ["full_customization", "batch_processing", "priority_support"],
#         },
#         "business": {
#             "screenshots": 50000,
#             "batch_requests": 500,
#             "api_calls": 100000,
#             "features": ["webhooks", "change_detection", "dedicated_support", "batch_processing"],
#         },
#         "premium": {
#             "screenshots": "unlimited",
#             "batch_requests": "unlimited",
#             "api_calls": "unlimited",
#             "features": ["white_label", "custom_sla", "account_manager", "webhooks", "change_detection"],
#         },
#     }

#     return limits.get(tier, limits["free"])

# # ============================================================================
# # USAGE RESET HELPER
# # ============================================================================

# def reset_monthly_usage(user: User, db: Session) -> None:
#     """Reset user's monthly usage counters"""
#     user.usage_screenshots = 0
#     user.usage_batch_requests = 0
#     user.usage_api_calls = 0
#     user.usage_reset_at = datetime.utcnow() + timedelta(days=30)
#     db.commit()

# # ============================================================================
# # DATABASE INITIALIZATION
# # ============================================================================

# def initialize_database() -> None:
#     """Create all database tables (including batch_jobs if new)"""
#     Base.metadata.create_all(bind=engine)
#     print("✅ Database tables created successfully")

# # ============================================================================
# # MIGRATION HELPER (for adding new fields to existing DB)
# # ============================================================================

# def add_missing_columns():
#     """
#     Add missing columns to existing tables.
#     Run this ONCE after deploying the updated models.py to a live DB.
#     The batch_jobs table is created automatically by initialize_database()
#     via Base.metadata.create_all() — no manual migration needed for it.
#     """
#     from sqlalchemy import inspect, text

#     inspector = inspect(engine)
#     columns = [col["name"] for col in inspector.get_columns("users")]

#     with engine.begin() as conn:
#         if "stripe_subscription_status" not in columns:
#             conn.execute(text("ALTER TABLE users ADD COLUMN stripe_subscription_status VARCHAR(20)"))
#             print("✅ Added stripe_subscription_status")

#         if "subscription_expires_at" not in columns:
#             conn.execute(text("ALTER TABLE users ADD COLUMN subscription_expires_at TIMESTAMP"))
#             print("✅ Added subscription_expires_at")

#         if "subscription_updated_at" not in columns:
#             conn.execute(text("ALTER TABLE users ADD COLUMN subscription_updated_at TIMESTAMP"))
#             print("✅ Added subscription_updated_at")

#     print("✅ Migration complete!")

# # # ===== END OF models.py ==========


