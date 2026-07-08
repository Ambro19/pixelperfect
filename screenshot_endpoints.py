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
        # ✅ FIX (July 2026): PDF is Pro+, not Business-only.
        # models.py TIER_FEATURES was out of sync with the documented tier matrix.
        "pdf":               True,
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

# # =====================================================
# # SCREENSHOT ENDPOINTS - PixelPerfect Screenshot API
# # File: backend/screenshot_endpoints.py
# # Author: OneTechly
# # Updated: April 2026 - PRODUCTION READY
# #
# # ✅ FIX (Apr 2026): Single screenshot capture now uploads to R2.
# #
# #   Root cause of production screenshot loss:
# #     batch.py correctly called storage_service.upload_screenshot() after
# #     every capture, so batch screenshots survived Render restarts via R2.
# #     capture_screenshot_endpoint() here did NOT — it called get_screenshot_url()
# #     directly, producing URLs that pointed to Render's ephemeral local disk
# #     (/app/screenshots/). Every redeploy wiped those files → all "View
# #     Screenshot" links returned 404 in production.
# #
# #   Fix:
# #     Mirror the exact same R2 upload pattern that batch.py already uses:
# #       1. Capture screenshot → local temp file
# #       2. Read file bytes
# #       3. If storage_service.use_r2: upload to R2 → get permanent CDN URL
# #       4. Store that CDN URL in storage_url (DB record + API response)
# #     Local storage fallback is preserved for dev (R2 not configured).
# #
# # ✅ NEW (Apr 2026): `delay` and `remove_elements` parameters wired through.
# #
# #   The frontend (ScreenshotPage.js) was already sending these fields, but
# #   the Pydantic request model here dropped them — they never reached the
# #   screenshot service. Users set "delay 3 seconds" or "hide the cookie
# #   banner" and saw no effect.
# #
# #   Now:
# #     - ScreenshotRequest.delay:            int 0–10 (Field validation)
# #     - ScreenshotRequest.remove_elements:  List[str] ≤20 items, each ≤200 chars
# #     - BatchScreenshotRequest: same additions for consistency
# #     - Both passed through to screenshot_service.capture_screenshot()
# #     - Backward compatible: both default to None, omitted = current behavior
# # =====================================================

# from datetime import datetime
# from pathlib import Path
# import logging
# from typing import List, Optional

# from fastapi import Depends, HTTPException
# from pydantic import BaseModel, Field, HttpUrl, field_validator
# from sqlalchemy.orm import Session

# from auth_deps import get_current_user
# from models import Screenshot, User, get_db, get_tier_limits
# from screenshot_service import (
#     screenshot_service,
#     get_screenshot_url,
#     increment_user_usage,
#     check_usage_limit,
# )
# from services.storage_service import storage_service

# logger = logging.getLogger("pixelperfect")

# # ── Content-type map (used when uploading to R2) ──────────────────────────────
# _CONTENT_TYPES = {
#     "png":  "image/png",
#     "jpeg": "image/jpeg",
#     "jpg":  "image/jpeg",
#     "webp": "image/webp",
#     "pdf":  "application/pdf",
# }

# # ── Hard limits for remove_elements (must match screenshot_service.py) ────────
# _MAX_REMOVE_ELEMENTS_COUNT   = 20
# _MAX_REMOVE_ELEMENT_SELECTOR = 200


# def _raise_not_ready(err: Optional[str] = None):
#     detail = (
#         "Screenshot service is not ready. Playwright browsers may be missing.\n"
#         "Fix:\n"
#         "  python -m playwright install --with-deps chromium\n"
#         "Then redeploy."
#     )
#     if err:
#         detail = f"{detail}\n\nLast error:\n{err}"
#     raise HTTPException(status_code=503, detail=detail)


# def _validate_remove_elements(value: Optional[List[str]]) -> Optional[List[str]]:
#     """
#     Shared validator for remove_elements. Returns cleaned list (or None).

#     Why a custom validator instead of relying on Pydantic alone:
#       - We want to silently drop bad entries (non-strings, empties) rather
#         than reject the whole request, because the frontend may send slightly
#         malformed input and we'd rather succeed than 422.
#       - The screenshot service ALSO sanitizes, so this is defense-in-depth.
#     """
#     if value is None:
#         return None
#     if not isinstance(value, list):
#         return None

#     cleaned: List[str] = []
#     for item in value:
#         if not isinstance(item, str):
#             continue
#         stripped = item.strip()
#         if not stripped:
#             continue
#         if len(stripped) > _MAX_REMOVE_ELEMENT_SELECTOR:
#             stripped = stripped[:_MAX_REMOVE_ELEMENT_SELECTOR]
#         cleaned.append(stripped)
#         if len(cleaned) >= _MAX_REMOVE_ELEMENTS_COUNT:
#             break

#     return cleaned or None


# class ScreenshotRequest(BaseModel):
#     url: HttpUrl = Field(..., description="Website URL to screenshot")
#     width:     int  = Field(default=1920, ge=320, le=3840)
#     height:    int  = Field(default=1080, ge=240, le=2160)
#     format:    str  = Field(default="png", description="png, jpeg, webp, pdf")
#     full_page: bool = Field(default=False)
#     dark_mode: bool = Field(default=False)

#     # ✅ NEW (Apr 2026)
#     delay: Optional[int] = Field(
#         default=None,
#         ge=0,
#         le=10,
#         description="Seconds to wait after page load before capture (0–10).",
#     )
#     remove_elements: Optional[List[str]] = Field(
#         default=None,
#         description=(
#             "CSS selectors for elements to hide before capture "
#             "(e.g. cookie banners, popups). Max 20 selectors, each ≤200 chars."
#         ),
#     )

#     @field_validator("remove_elements")
#     @classmethod
#     def _clean_remove_elements(cls, v):
#         return _validate_remove_elements(v)


# class ScreenshotResponse(BaseModel):
#     screenshot_id:  str
#     screenshot_url: str
#     width:          int
#     height:         int
#     format:         str
#     size_bytes:     int
#     created_at:     str
#     message:        Optional[str] = None


# class BatchScreenshotRequest(BaseModel):
#     urls:      List[HttpUrl] = Field(..., min_length=1, max_length=50)
#     width:     int  = Field(default=1920, ge=320, le=3840)
#     height:    int  = Field(default=1080, ge=240, le=2160)
#     format:    str  = Field(default="png")
#     full_page: bool = Field(default=False)
#     dark_mode: bool = Field(default=False)

#     # ✅ NEW (Apr 2026): Applied to every URL in the batch
#     delay: Optional[int] = Field(
#         default=None,
#         ge=0,
#         le=10,
#         description="Seconds to wait after page load before each capture (0–10).",
#     )
#     remove_elements: Optional[List[str]] = Field(
#         default=None,
#         description=(
#             "CSS selectors for elements to hide before capture. "
#             "Applied to every URL in the batch. Max 20 selectors, each ≤200 chars."
#         ),
#     )

#     @field_validator("remove_elements")
#     @classmethod
#     def _clean_remove_elements(cls, v):
#         return _validate_remove_elements(v)


# # ── Single screenshot capture ─────────────────────────────────────────────────

# async def capture_screenshot_endpoint(
#     request: ScreenshotRequest,
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     tier        = (current_user.subscription_tier or "free").lower()
#     tier_limits = get_tier_limits(tier)

#     if not check_usage_limit(current_user, tier_limits):
#         limit = tier_limits.get("screenshots")
#         raise HTTPException(
#             status_code=429,
#             detail=f"Screenshot limit exceeded ({limit}/month). Upgrade your plan to continue.",
#         )

#     if not screenshot_service.is_ready():
#         _raise_not_ready(screenshot_service.last_error())

#     try:
#         # ── 1. Capture screenshot → local temp file ───────────────────────
#         result = await screenshot_service.capture_screenshot(
#             url=str(request.url),
#             width=request.width,
#             height=request.height,
#             format=request.format.lower(),
#             full_page=request.full_page,
#             dark_mode=request.dark_mode,
#             delay=request.delay,                       # ✅ NEW
#             remove_elements=request.remove_elements,   # ✅ NEW
#         )

#         filename        = result["filename"]
#         screenshot_path = result.get("filepath")
#         fmt             = str(result.get("format") or request.format).lower()

#         # ── 2. Upload to R2 if configured; fall back to local URL ─────────
#         if storage_service.use_r2 and screenshot_path:
#             try:
#                 file_bytes   = Path(screenshot_path).read_bytes()
#                 content_type = _CONTENT_TYPES.get(fmt, "image/png")
#                 screenshot_url = await storage_service.upload_screenshot(
#                     file_data=file_bytes,
#                     filename=filename,
#                     content_type=content_type,
#                 )
#                 logger.info(
#                     "☁️  Single screenshot uploaded to R2: %s", screenshot_url
#                 )
#             except Exception as r2_err:
#                 logger.warning(
#                     "⚠️ R2 upload failed for single capture, using local URL: %s",
#                     r2_err,
#                 )
#                 screenshot_url = get_screenshot_url(filename)
#         else:
#             screenshot_url = get_screenshot_url(filename)
#             logger.info("💾 Single screenshot saved locally: %s", screenshot_url)

#         # ── 3. Persist DB record ──────────────────────────────────────────
#         screenshot_record = Screenshot(
#             user_id=current_user.id,
#             url=str(request.url),
#             screenshot_path=screenshot_path,
#             width=int(result.get("width")  or request.width),
#             height=int(result.get("height") or request.height),
#             format=fmt,
#             full_page=bool(result.get("full_page")),
#             dark_mode=bool(result.get("dark_mode")),
#             status="completed",
#             created_at=result.get("created_at") or datetime.utcnow(),
#             size_bytes=int(result.get("file_size") or 0),
#             storage_url=screenshot_url,
#         )

#         db.add(screenshot_record)
#         increment_user_usage(current_user)
#         db.commit()
#         db.refresh(screenshot_record)

#         return ScreenshotResponse(
#             screenshot_id=str(screenshot_record.id),
#             screenshot_url=screenshot_url,
#             width=int(result.get("width")  or request.width),
#             height=int(result.get("height") or request.height),
#             format=fmt,
#             size_bytes=int(result.get("file_size") or 0),
#             created_at=(result.get("created_at") or datetime.utcnow()).isoformat(),
#             message="Screenshot captured successfully",
#         )

#     except ValueError as e:
#         db.rollback()
#         raise HTTPException(status_code=400, detail=str(e))

#     except Exception:
#         db.rollback()
#         logger.exception(
#             "❌ Unexpected error capturing screenshot for user %s",
#             current_user.id,
#         )
#         raise HTTPException(
#             status_code=500,
#             detail="Failed to capture screenshot. Please try again.",
#         )


# # ── Batch screenshot capture ──────────────────────────────────────────────────
# # NOTE: batch.py (the background-task batch router) already handles R2 uploads
# # correctly. This endpoint is the older synchronous batch path and is preserved
# # for backward compatibility. R2 upload is added here too for consistency.

# async def batch_screenshot_endpoint(
#     request: BatchScreenshotRequest,
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     tier = (current_user.subscription_tier or "free").lower()
#     if tier == "free":
#         raise HTTPException(
#             status_code=403,
#             detail="Batch processing requires Pro plan or higher.",
#         )

#     if not screenshot_service.is_ready():
#         _raise_not_ready(screenshot_service.last_error())

#     tier_limits  = get_tier_limits(tier)
#     batch_limit  = tier_limits.get("batch_requests", 0)

#     if batch_limit != "unlimited":
#         current_batch_usage = current_user.usage_batch_requests or 0
#         if current_batch_usage >= batch_limit:
#             raise HTTPException(
#                 status_code=429,
#                 detail=f"Batch request limit exceeded ({batch_limit}/month). Upgrade to continue.",
#             )

#     results = []
#     failed  = []

#     try:
#         for url in request.urls:
#             try:
#                 result = await screenshot_service.capture_screenshot(
#                     url=str(url),
#                     width=request.width,
#                     height=request.height,
#                     format=request.format.lower(),
#                     full_page=request.full_page,
#                     dark_mode=request.dark_mode,
#                     delay=request.delay,                       # ✅ NEW
#                     remove_elements=request.remove_elements,   # ✅ NEW
#                 )

#                 filename        = result["filename"]
#                 screenshot_path = result.get("filepath")
#                 fmt             = str(result.get("format") or request.format).lower()

#                 # ── R2 upload (same pattern as single capture above) ──────
#                 if storage_service.use_r2 and screenshot_path:
#                     try:
#                         file_bytes   = Path(screenshot_path).read_bytes()
#                         content_type = _CONTENT_TYPES.get(fmt, "image/png")
#                         screenshot_url = await storage_service.upload_screenshot(
#                             file_data=file_bytes,
#                             filename=filename,
#                             content_type=content_type,
#                         )
#                         logger.info(
#                             "☁️  Batch item uploaded to R2: %s", screenshot_url
#                         )
#                     except Exception as r2_err:
#                         logger.warning(
#                             "⚠️ R2 upload failed for batch item, using local URL: %s",
#                             r2_err,
#                         )
#                         screenshot_url = get_screenshot_url(filename)
#                 else:
#                     screenshot_url = get_screenshot_url(filename)

#                 rec = Screenshot(
#                     user_id=current_user.id,
#                     url=str(url),
#                     screenshot_path=screenshot_path,
#                     width=int(result.get("width")  or request.width),
#                     height=int(result.get("height") or request.height),
#                     format=fmt,
#                     full_page=bool(result.get("full_page")),
#                     dark_mode=bool(result.get("dark_mode")),
#                     status="completed",
#                     created_at=result.get("created_at") or datetime.utcnow(),
#                     size_bytes=int(result.get("file_size") or 0),
#                     storage_url=screenshot_url,
#                 )
#                 db.add(rec)
#                 db.flush()   # ensures rec.id exists before we return it

#                 results.append({
#                     "id":             str(rec.id),
#                     "url":            str(url),
#                     "screenshot_url": screenshot_url,
#                     "status":         "success",
#                     "format":         rec.format,
#                     "width":          rec.width,
#                     "height":         rec.height,
#                     "created_at":     rec.created_at.isoformat() if rec.created_at else None,
#                 })

#             except Exception as e:
#                 logger.error("❌ Failed to capture %s: %s", url, e)
#                 failed.append({"url": str(url), "status": "failed", "error": str(e)})

#         current_user.usage_batch_requests = (current_user.usage_batch_requests or 0) + 1
#         current_user.usage_screenshots    = (current_user.usage_screenshots    or 0) + len(results)
#         current_user.usage_api_calls      = (current_user.usage_api_calls      or 0) + 1

#         db.commit()

#         return {
#             "batch_id":   f"batch_{int(datetime.utcnow().timestamp())}",
#             "total":      len(request.urls),
#             "successful": len(results),
#             "failed":     len(failed),
#             "results":    results,
#             "failures":   failed,
#         }

#     except Exception:
#         db.rollback()
#         logger.exception(
#             "❌ Batch screenshot failed for user %s", current_user.id
#         )
#         raise HTTPException(
#             status_code=500,
#             detail="Batch processing failed. Please try again.",
#         )


# # ── API key regeneration ──────────────────────────────────────────────────────

# async def regenerate_api_key_endpoint(
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     from api_key_system import regenerate_api_key

#     user_id = getattr(current_user, "id", None)

#     try:
#         new_key, new_record = regenerate_api_key(db, user_id)
#         db.commit()
#         return {
#             "api_key":    new_key,
#             "key_prefix": new_record.key_prefix,
#             "created_at": new_record.created_at.isoformat(),
#             "message":    (
#                 "⚠️ Save this key securely. "
#                 "Your old key has been deactivated and will no longer work."
#             ),
#         }
#     except Exception as e:
#         db.rollback()
#         logger.exception(
#             "❌ Failed to regenerate API key for user %s: %s", user_id, e
#         )
#         raise HTTPException(
#             status_code=500,
#             detail="Failed to regenerate API key. Please try again.",
#         )

# # ===== END OF screenshot_endpoints.py ======

