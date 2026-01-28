# ============================================================================
# DATABASE MODELS - PixelPerfect Screenshot API
# File: backend/models.py
# Author: OneTechly
# Updated: January 2026 - UUID Screenshot IDs + schema alignment
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
)
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

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    pool_pre_ping=True,
)

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

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(100), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)

    # Stripe integration
    stripe_customer_id = Column(String(100), unique=True, nullable=True)

    # Subscription
    subscription_tier = Column(String(20), default="free", nullable=False)
    subscription_status = Column(String(20), default="active", nullable=True)
    subscription_id = Column(String(100), unique=True, nullable=True)
    subscription_ends_at = Column(DateTime, nullable=True)

    # Usage tracking
    usage_screenshots = Column(Integer, default=0)
    usage_batch_requests = Column(Integer, default=0)
    usage_api_calls = Column(Integer, default=0)
    usage_reset_at = Column(DateTime, nullable=True)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    __table_args__ = (
        Index("idx_user_email", "email"),
        Index("idx_user_username", "username"),
        Index("idx_user_stripe", "stripe_customer_id"),
    )

# ============================================================================
# API KEY MODEL
# ============================================================================

class ApiKey(Base):
    """
    API Keys for programmatic access.
    Keys are stored as hashes (never store plaintext).
    """
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    key_hash = Column(String(64), unique=True, nullable=False, index=True)
    key_prefix = Column(String(16), nullable=False)

    name = Column(String(100), default="Default API Key", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_api_key_hash", "key_hash"),
        Index("idx_api_key_user", "user_id"),
        Index("idx_api_key_active", "is_active"),
    )

# ============================================================================
# SCREENSHOT MODEL (✅ aligned to your real DB schema)
# ============================================================================

class Screenshot(Base):
    """Screenshot capture record (matches existing SQLite schema)"""
    __tablename__ = "screenshots"

    # IMPORTANT: your DB shows "id VARCHAR NOT NULL"
    id = Column(String, primary_key=True, index=True, default=lambda: str(uuid4()))

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    url = Column(Text, nullable=False)

    # Your DB has width/height NOT NULL
    width = Column(Integer, nullable=False, default=1920)
    height = Column(Integer, nullable=False, default=1080)

    full_page = Column(Boolean, nullable=True)
    format = Column(String(10), nullable=False, default="png")
    quality = Column(Integer, nullable=True)

    size_bytes = Column(Integer, nullable=False, default=0)

    # Your DB includes storage_url/storage_key even if you also store local path
    storage_url = Column(Text, nullable=False, default="")
    storage_key = Column(String, nullable=True)

    processing_time_ms = Column(Float, nullable=True)

    status = Column(String, nullable=True, default="completed")
    error_message = Column(Text, nullable=True)

    dark_mode = Column(Boolean, nullable=True)
    delay_seconds = Column(Integer, nullable=True)
    remove_elements = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=True, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)

    is_baseline = Column(Boolean, nullable=True)
    baseline_screenshot_id = Column(String, ForeignKey("screenshots.id"), nullable=True)
    difference_percentage = Column(Float, nullable=True)
    has_changes = Column(Boolean, nullable=True)

    # Your DB also has screenshot_path
    screenshot_path = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_screenshot_user", "user_id"),
        Index("idx_screenshot_created", "created_at"),
        Index("idx_screenshot_status", "status"),
        Index("idx_screenshot_format", "format"),
    )

# ============================================================================
# SUBSCRIPTION MODEL
# ============================================================================

class Subscription(Base):
    """Stripe subscription details"""
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    stripe_subscription_id = Column(String(100), unique=True, nullable=False)
    stripe_customer_id = Column(String(100), nullable=False)

    tier = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False)

    current_period_start = Column(DateTime, nullable=True)
    current_period_end = Column(DateTime, nullable=True)
    cancel_at_period_end = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_subscription_user", "user_id"),
        Index("idx_subscription_stripe", "stripe_subscription_id"),
    )

# ============================================================================
# TIER LIMITS CONFIGURATION
# ============================================================================

def get_tier_limits(tier: str) -> Dict[str, Any]:
    tier = (tier or "free").lower()
    limits = {
        "free": {"screenshots": 100, "batch_requests": 0, "api_calls": 1000},
        "pro": {"screenshots": 1000, "batch_requests": 50, "api_calls": 10000},
        "business": {"screenshots": 5000, "batch_requests": 200, "api_calls": 50000},
        "premium": {"screenshots": "unlimited", "batch_requests": "unlimited", "api_calls": "unlimited"},
    }
    return limits.get(tier, limits["free"])

# ============================================================================
# USAGE RESET HELPER
# ============================================================================

def reset_monthly_usage(user: User, db: Session) -> None:
    user.usage_screenshots = 0
    user.usage_batch_requests = 0
    user.usage_api_calls = 0
    user.usage_reset_at = datetime.utcnow() + timedelta(days=30)
    db.commit()

# ============================================================================
# DATABASE INITIALIZATION
# ============================================================================

def initialize_database() -> None:
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created successfully")


# # ===============================================================
# # ============================================================================
# # DATABASE MODELS - PixelPerfect Screenshot API
# # ============================================================================
# # File: backend/models.py
# # Author: OneTechly
# # Updated: January 2026 - FIX: Screenshot.id UUID (matches SQLite schema)
# # Notes:
# # - SQLite schema shows screenshots.id is VARCHAR NOT NULL (PRIMARY KEY)
# # - We use UUID strings for Screenshot.id to avoid NOT NULL failures
# # - We also include the columns that exist in your screenshots table
# # ============================================================================

# from __future__ import annotations

# import os
# import uuid
# from datetime import datetime, timedelta
# from typing import Any, Dict, Generator, Optional

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
# )
# from sqlalchemy.orm import Session, declarative_base, sessionmaker

# # ============================================================================
# # DATABASE CONFIGURATION
# # ============================================================================

# DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./pixelperfect.db")

# # PostgreSQL URL normalization
# if DATABASE_URL.startswith("postgres://"):
#     DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
# elif DATABASE_URL.startswith("postgresql://") and "+psycopg2" not in DATABASE_URL:
#     DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

# engine = create_engine(
#     DATABASE_URL,
#     connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
#     pool_pre_ping=True,
# )

# SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
# Base = declarative_base()


# # ============================================================================
# # HELPERS
# # ============================================================================

# def uuid_str() -> str:
#     """Generate UUID string for VARCHAR PKs."""
#     return str(uuid.uuid4())


# # ============================================================================
# # DATABASE DEPENDENCY
# # ============================================================================

# def get_db() -> Generator[Session, None, None]:
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

#     # Subscription
#     subscription_tier = Column(String(20), default="free", nullable=False)
#     subscription_status = Column(String(20), default="active", nullable=True)
#     subscription_id = Column(String(100), unique=True, nullable=True)
#     subscription_ends_at = Column(DateTime, nullable=True)

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
#     )


# # ============================================================================
# # API KEY MODEL
# # ============================================================================

# class ApiKey(Base):
#     """
#     API Keys for programmatic access
#     Keys are stored as hashes for security.
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
# # SCREENSHOT MODEL (matches your SQLite schema)
# # ============================================================================

# class Screenshot(Base):
#     """
#     Screenshot capture record

#     IMPORTANT:
#     Your SQLite schema shows:
#       id VARCHAR NOT NULL PRIMARY KEY
#     So we use UUID strings.
#     """
#     __tablename__ = "screenshots"

#     # Matches: id VARCHAR NOT NULL PRIMARY KEY
#     id = Column(String, primary_key=True, default=uuid_str)

#     user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

#     # Matches: url TEXT NOT NULL
#     url = Column(Text, nullable=False)

#     # Added by you: screenshot_path TEXT
#     screenshot_path = Column(Text, nullable=True)

#     # Matches: width INTEGER NOT NULL / height INTEGER NOT NULL
#     width = Column(Integer, nullable=False, default=1920)
#     height = Column(Integer, nullable=False, default=1080)

#     # Matches: full_page BOOLEAN
#     full_page = Column(Boolean, default=False)

#     # Matches: format VARCHAR(10) NOT NULL
#     format = Column(String(10), nullable=False, default="png")

#     # Matches: quality INTEGER (nullable)
#     quality = Column(Integer, nullable=True)

#     # Matches: size_bytes INTEGER NOT NULL
#     size_bytes = Column(Integer, nullable=False, default=0)

#     # Matches: storage_url TEXT NOT NULL
#     storage_url = Column(Text, nullable=False, default="")

#     # Matches: storage_key VARCHAR (nullable)
#     storage_key = Column(String, nullable=True)

#     # Matches: processing_time_ms FLOAT (nullable)
#     processing_time_ms = Column(Float, nullable=True)

#     # Matches: status VARCHAR (nullable in schema)
#     status = Column(String, nullable=True, default="completed")

#     # Matches: error_message TEXT (nullable)
#     error_message = Column(Text, nullable=True)

#     # Matches: dark_mode BOOLEAN (nullable in schema)
#     dark_mode = Column(Boolean, default=False)

#     # Matches: delay_seconds INTEGER (nullable)
#     delay_seconds = Column(Integer, nullable=True)

#     # Matches: remove_elements TEXT (nullable)
#     remove_elements = Column(Text, nullable=True)

#     # Matches: created_at DATETIME (nullable in schema; you want it set)
#     created_at = Column(DateTime, default=datetime.utcnow)

#     # Matches: expires_at DATETIME (nullable)
#     expires_at = Column(DateTime, nullable=True)

#     # Matches: is_baseline BOOLEAN (nullable)
#     is_baseline = Column(Boolean, nullable=True, default=False)

#     # Matches: baseline_screenshot_id VARCHAR (nullable, FK to screenshots.id)
#     baseline_screenshot_id = Column(String, ForeignKey("screenshots.id"), nullable=True)

#     # Matches: difference_percentage FLOAT (nullable)
#     difference_percentage = Column(Float, nullable=True)

#     # Matches: has_changes BOOLEAN (nullable)
#     has_changes = Column(Boolean, nullable=True, default=False)

#     __table_args__ = (
#         Index("idx_screenshot_user", "user_id"),
#         Index("idx_screenshot_created", "created_at"),
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

#     tier = Column(String(20), nullable=False)  # free, pro, business, premium
#     status = Column(String(20), nullable=False)  # active, canceled, past_due, etc.

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
#     tier = (tier or "free").lower()
#     limits = {
#         "free": {"screenshots": 100, "batch_requests": 0, "api_calls": 1000},
#         "pro": {"screenshots": 1000, "batch_requests": 50, "api_calls": 10000},
#         "business": {"screenshots": 5000, "batch_requests": 200, "api_calls": 50000},
#         "premium": {"screenshots": "unlimited", "batch_requests": "unlimited", "api_calls": "unlimited"},
#     }
#     return limits.get(tier, limits["free"])


# # ============================================================================
# # USAGE RESET HELPER
# # ============================================================================

# def reset_monthly_usage(user: User, db: Session) -> None:
#     user.usage_screenshots = 0
#     user.usage_batch_requests = 0
#     user.usage_api_calls = 0
#     user.usage_reset_at = datetime.utcnow() + timedelta(days=30)
#     db.commit()


# # ============================================================================
# # DATABASE INITIALIZATION
# # ============================================================================

# def initialize_database() -> None:
#     Base.metadata.create_all(bind=engine)
#     print("✅ Database tables created successfully")

