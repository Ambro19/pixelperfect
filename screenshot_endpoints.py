# ============================================================================
# SCREENSHOT ENDPOINTS - PixelPerfect Screenshot API
# File: backend/screenshot_endpoints.py
# Fixes:
# ✅ Rollback on ANY DB failure (prevents PendingRollbackError)
# ✅ Works with Screenshot.id = UUID string default
# ✅ Graceful 503 if Playwright not ready
# ============================================================================

from datetime import datetime
import logging
from typing import List, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy.orm import Session

from auth_deps import get_current_user
from models import Screenshot, User, get_db, get_tier_limits
from screenshot_service import (
    screenshot_service,
    get_screenshot_url,
    increment_user_usage,
    check_usage_limit,
)

logger = logging.getLogger("pixelperfect")

# ============================================================================
# INTERNAL HELPERS
# ============================================================================

def _screenshot_service_ready() -> bool:
    return bool(getattr(screenshot_service, "_initialized", False))

def _raise_not_ready():
    raise HTTPException(
        status_code=503,
        detail=(
            "Screenshot service is not ready. Playwright browsers may be missing.\n"
            "If deploying on Render, ensure your build runs:\n"
            "  python -m playwright install --with-deps chromium\n"
            "Then redeploy."
        ),
    )

# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class ScreenshotRequest(BaseModel):
    url: HttpUrl = Field(..., description="Website URL to screenshot")
    width: int = Field(default=1920, ge=320, le=3840)
    height: int = Field(default=1080, ge=240, le=2160)
    format: str = Field(default="png", description="png, jpeg, webp, pdf")
    full_page: bool = Field(default=False)
    dark_mode: bool = Field(default=False)

class ScreenshotResponse(BaseModel):
    screenshot_id: str
    screenshot_url: str
    width: int
    height: int
    format: str
    size_bytes: int
    created_at: str
    message: Optional[str] = None

class BatchScreenshotRequest(BaseModel):
    urls: List[HttpUrl] = Field(..., min_length=1, max_length=50)
    width: int = Field(default=1920, ge=320, le=3840)
    height: int = Field(default=1080, ge=240, le=2160)
    format: str = Field(default="png")
    full_page: bool = Field(default=False)
    dark_mode: bool = Field(default=False)

# ============================================================================
# SCREENSHOT ENDPOINT
# ============================================================================

async def capture_screenshot_endpoint(
    request: ScreenshotRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tier = (current_user.subscription_tier or "free").lower()
    tier_limits = get_tier_limits(tier)

    if not check_usage_limit(current_user, tier_limits):
        limit = tier_limits.get("screenshots")
        raise HTTPException(
            status_code=429,
            detail=f"Screenshot limit exceeded ({limit}/month). Upgrade your plan to continue.",
        )

    if not _screenshot_service_ready():
        _raise_not_ready()

    try:
        result = await screenshot_service.capture_screenshot(
            url=str(request.url),
            width=request.width,
            height=request.height,
            format=request.format.lower(),
            full_page=request.full_page,
            dark_mode=request.dark_mode,
        )

        # NOTE: Screenshot.id is UUID string default in models.py now.
        screenshot_record = Screenshot(
            user_id=current_user.id,
            url=str(request.url),
            screenshot_path=result.get("filepath"),
            width=int(result.get("width") or request.width),
            height=int(result.get("height") or request.height),
            format=str(result.get("format") or request.format).lower(),
            full_page=bool(result.get("full_page")),
            dark_mode=bool(result.get("dark_mode")),
            status="completed",
            created_at=result.get("created_at") or datetime.utcnow(),
            size_bytes=int(result.get("file_size") or 0),
            storage_url=get_screenshot_url(result["filename"]),
        )

        db.add(screenshot_record)

        # usage counters update + commit
        increment_user_usage(current_user, db)

        try:
            db.commit()
        except Exception:
            db.rollback()
            raise

        db.refresh(screenshot_record)

        logger.info("✅ Screenshot created for user %s: %s", current_user.id, result["filename"])

        return ScreenshotResponse(
            screenshot_id=str(screenshot_record.id),
            screenshot_url=get_screenshot_url(result["filename"]),
            width=int(result.get("width") or request.width),
            height=int(result.get("height") or request.height),
            format=str(result.get("format") or request.format).lower(),
            size_bytes=int(result.get("file_size") or 0),
            created_at=(result.get("created_at") or datetime.utcnow()).isoformat(),
            message="Screenshot captured successfully",
        )

    except ValueError as e:
        db.rollback()
        logger.error("❌ Screenshot error for user %s: %s", current_user.id, e)
        raise HTTPException(status_code=400, detail=str(e))

    except Exception:
        db.rollback()
        logger.exception("❌ Unexpected error capturing screenshot for user %s", current_user.id)
        raise HTTPException(status_code=500, detail="Failed to capture screenshot. Please try again.")

# ============================================================================
# BATCH SCREENSHOT ENDPOINT
# ============================================================================

async def batch_screenshot_endpoint(
    request: BatchScreenshotRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tier = (current_user.subscription_tier or "free").lower()
    if tier == "free":
        raise HTTPException(status_code=403, detail="Batch processing requires Pro plan or higher.")

    if not _screenshot_service_ready():
        _raise_not_ready()

    tier_limits = get_tier_limits(tier)
    batch_limit = tier_limits.get("batch_requests", 0)

    if batch_limit != "unlimited":
        current_batch_usage = current_user.usage_batch_requests or 0
        if current_batch_usage >= batch_limit:
            raise HTTPException(
                status_code=429,
                detail=f"Batch request limit exceeded ({batch_limit}/month). Upgrade to continue.",
            )

    results = []
    failed = []

    try:
        for url in request.urls:
            try:
                result = await screenshot_service.capture_screenshot(
                    url=str(url),
                    width=request.width,
                    height=request.height,
                    format=request.format.lower(),
                    full_page=request.full_page,
                    dark_mode=request.dark_mode,
                )

                rec = Screenshot(
                    user_id=current_user.id,
                    url=str(url),
                    screenshot_path=result.get("filepath"),
                    width=int(result.get("width") or request.width),
                    height=int(result.get("height") or request.height),
                    format=str(result.get("format") or request.format).lower(),
                    full_page=bool(result.get("full_page")),
                    dark_mode=bool(result.get("dark_mode")),
                    status="completed",
                    created_at=result.get("created_at") or datetime.utcnow(),
                    size_bytes=int(result.get("file_size") or 0),
                    storage_url=get_screenshot_url(result["filename"]),
                )
                db.add(rec)

                results.append(
                    {
                        "id": str(rec.id) if rec.id else None,
                        "url": str(url),
                        "screenshot_url": get_screenshot_url(result["filename"]),
                        "status": "success",
                        "format": rec.format,
                        "width": rec.width,
                        "height": rec.height,
                        "created_at": rec.created_at.isoformat() if rec.created_at else None,
                    }
                )

            except Exception as e:
                logger.error("❌ Failed to capture %s: %s", url, e)
                failed.append({"url": str(url), "status": "failed", "error": str(e)})

        current_user.usage_batch_requests = (current_user.usage_batch_requests or 0) + 1
        current_user.usage_screenshots = (current_user.usage_screenshots or 0) + len(results)
        current_user.usage_api_calls = (current_user.usage_api_calls or 0) + 1

        try:
            db.commit()
        except Exception:
            db.rollback()
            raise

        return {
            "batch_id": f"batch_{int(datetime.utcnow().timestamp())}",
            "total": len(request.urls),
            "successful": len(results),
            "failed": len(failed),
            "results": results,
            "failures": failed,
        }

    except Exception:
        db.rollback()
        logger.exception("❌ Batch screenshot failed for user %s", current_user.id)
        raise HTTPException(status_code=500, detail="Batch processing failed. Please try again.")

# ============================================================================
# REGENERATE API KEY ENDPOINT
# ============================================================================

async def regenerate_api_key_endpoint(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Regenerate a user's API key (deactivates old key, returns new plain key once).
    """
    from api_key_system import regenerate_api_key

    user_id = getattr(current_user, "id", None)

    try:
        new_key, new_record = regenerate_api_key(db, user_id)
        logger.info("🔄 API key regenerated for user %s", user_id)
        return {
            "api_key": new_key,
            "key_prefix": new_record.key_prefix,
            "created_at": new_record.created_at.isoformat(),
            "message": "⚠️ Save this key securely. Your old key has been deactivated and will no longer work.",
        }
    except Exception as e:
        db.rollback()
        logger.exception("❌ Failed to regenerate API key for user %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Failed to regenerate API key")

print("LOADED screenshot_endpoints FROM:", __file__)


