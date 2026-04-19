# =====================================================
# SCREENSHOT ENDPOINTS - PixelPerfect Screenshot API
# File: backend/screenshot_endpoints.py
# Author: OneTechly
# Updated: April 2026 - PRODUCTION READY
#
# ✅ FIX (Apr 2026): Single screenshot capture now uploads to R2.
#
#   Root cause of production screenshot loss:
#     batch.py correctly called storage_service.upload_screenshot() after
#     every capture, so batch screenshots survived Render restarts via R2.
#     capture_screenshot_endpoint() here did NOT — it called get_screenshot_url()
#     directly, producing URLs that pointed to Render's ephemeral local disk
#     (/app/screenshots/). Every redeploy wiped those files → all "View
#     Screenshot" links returned 404 in production.
#
#   Fix:
#     Mirror the exact same R2 upload pattern that batch.py already uses:
#       1. Capture screenshot → local temp file
#       2. Read file bytes
#       3. If storage_service.use_r2: upload to R2 → get permanent CDN URL
#       4. Store that CDN URL in storage_url (DB record + API response)
#     Local storage fallback is preserved for dev (R2 not configured).
# =====================================================

from datetime import datetime
from pathlib import Path
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
from services.storage_service import storage_service

logger = logging.getLogger("pixelperfect")

# ── Content-type map (used when uploading to R2) ──────────────────────────────
_CONTENT_TYPES = {
    "png":  "image/png",
    "jpeg": "image/jpeg",
    "jpg":  "image/jpeg",
    "webp": "image/webp",
    "pdf":  "application/pdf",
}


def _raise_not_ready(err: Optional[str] = None):
    detail = (
        "Screenshot service is not ready. Playwright browsers may be missing.\n"
        "Fix:\n"
        "  python -m playwright install --with-deps chromium\n"
        "Then redeploy."
    )
    if err:
        detail = f"{detail}\n\nLast error:\n{err}"
    raise HTTPException(status_code=503, detail=detail)


class ScreenshotRequest(BaseModel):
    url: HttpUrl = Field(..., description="Website URL to screenshot")
    width:     int  = Field(default=1920, ge=320, le=3840)
    height:    int  = Field(default=1080, ge=240, le=2160)
    format:    str  = Field(default="png", description="png, jpeg, webp, pdf")
    full_page: bool = Field(default=False)
    dark_mode: bool = Field(default=False)


class ScreenshotResponse(BaseModel):
    screenshot_id:  str
    screenshot_url: str
    width:          int
    height:         int
    format:         str
    size_bytes:     int
    created_at:     str
    message:        Optional[str] = None


class BatchScreenshotRequest(BaseModel):
    urls:      List[HttpUrl] = Field(..., min_length=1, max_length=50)
    width:     int  = Field(default=1920, ge=320, le=3840)
    height:    int  = Field(default=1080, ge=240, le=2160)
    format:    str  = Field(default="png")
    full_page: bool = Field(default=False)
    dark_mode: bool = Field(default=False)


# ── Single screenshot capture ─────────────────────────────────────────────────

async def capture_screenshot_endpoint(
    request: ScreenshotRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tier        = (current_user.subscription_tier or "free").lower()
    tier_limits = get_tier_limits(tier)

    if not check_usage_limit(current_user, tier_limits):
        limit = tier_limits.get("screenshots")
        raise HTTPException(
            status_code=429,
            detail=f"Screenshot limit exceeded ({limit}/month). Upgrade your plan to continue.",
        )

    if not screenshot_service.is_ready():
        _raise_not_ready(screenshot_service.last_error())

    try:
        # ── 1. Capture screenshot → local temp file ───────────────────────
        result = await screenshot_service.capture_screenshot(
            url=str(request.url),
            width=request.width,
            height=request.height,
            format=request.format.lower(),
            full_page=request.full_page,
            dark_mode=request.dark_mode,
        )

        filename        = result["filename"]
        screenshot_path = result.get("filepath")
        fmt             = str(result.get("format") or request.format).lower()

        # ── 2. Upload to R2 if configured; fall back to local URL ─────────
        # ✅ FIX: this is what was missing — single captures now go to R2
        # exactly the same way batch.py has always done it.
        if storage_service.use_r2 and screenshot_path:
            try:
                file_bytes   = Path(screenshot_path).read_bytes()
                content_type = _CONTENT_TYPES.get(fmt, "image/png")
                screenshot_url = await storage_service.upload_screenshot(
                    file_data=file_bytes,
                    filename=filename,
                    content_type=content_type,
                )
                logger.info(
                    "☁️  Single screenshot uploaded to R2: %s", screenshot_url
                )
            except Exception as r2_err:
                # Non-fatal: fall back to local URL so the request doesn't fail
                logger.warning(
                    "⚠️ R2 upload failed for single capture, using local URL: %s",
                    r2_err,
                )
                screenshot_url = get_screenshot_url(filename)
        else:
            # Local dev or R2 not configured
            screenshot_url = get_screenshot_url(filename)
            logger.info("💾 Single screenshot saved locally: %s", screenshot_url)

        # ── 3. Persist DB record ──────────────────────────────────────────
        screenshot_record = Screenshot(
            user_id=current_user.id,
            url=str(request.url),
            screenshot_path=screenshot_path,
            width=int(result.get("width")  or request.width),
            height=int(result.get("height") or request.height),
            format=fmt,
            full_page=bool(result.get("full_page")),
            dark_mode=bool(result.get("dark_mode")),
            status="completed",
            created_at=result.get("created_at") or datetime.utcnow(),
            size_bytes=int(result.get("file_size") or 0),
            storage_url=screenshot_url,   # ← R2 CDN URL in prod, local in dev
        )

        db.add(screenshot_record)
        increment_user_usage(current_user)
        db.commit()
        db.refresh(screenshot_record)

        return ScreenshotResponse(
            screenshot_id=str(screenshot_record.id),
            screenshot_url=screenshot_url,
            width=int(result.get("width")  or request.width),
            height=int(result.get("height") or request.height),
            format=fmt,
            size_bytes=int(result.get("file_size") or 0),
            created_at=(result.get("created_at") or datetime.utcnow()).isoformat(),
            message="Screenshot captured successfully",
        )

    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    except Exception:
        db.rollback()
        logger.exception(
            "❌ Unexpected error capturing screenshot for user %s",
            current_user.id,
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to capture screenshot. Please try again.",
        )


# ── Batch screenshot capture ──────────────────────────────────────────────────
# NOTE: batch.py (the background-task batch router) already handles R2 uploads
# correctly. This endpoint is the older synchronous batch path and is preserved
# for backward compatibility. R2 upload is added here too for consistency.

async def batch_screenshot_endpoint(
    request: BatchScreenshotRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tier = (current_user.subscription_tier or "free").lower()
    if tier == "free":
        raise HTTPException(
            status_code=403,
            detail="Batch processing requires Pro plan or higher.",
        )

    if not screenshot_service.is_ready():
        _raise_not_ready(screenshot_service.last_error())

    tier_limits  = get_tier_limits(tier)
    batch_limit  = tier_limits.get("batch_requests", 0)

    if batch_limit != "unlimited":
        current_batch_usage = current_user.usage_batch_requests or 0
        if current_batch_usage >= batch_limit:
            raise HTTPException(
                status_code=429,
                detail=f"Batch request limit exceeded ({batch_limit}/month). Upgrade to continue.",
            )

    results = []
    failed  = []

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

                filename        = result["filename"]
                screenshot_path = result.get("filepath")
                fmt             = str(result.get("format") or request.format).lower()

                # ── R2 upload (same pattern as single capture above) ──────
                if storage_service.use_r2 and screenshot_path:
                    try:
                        file_bytes   = Path(screenshot_path).read_bytes()
                        content_type = _CONTENT_TYPES.get(fmt, "image/png")
                        screenshot_url = await storage_service.upload_screenshot(
                            file_data=file_bytes,
                            filename=filename,
                            content_type=content_type,
                        )
                        logger.info(
                            "☁️  Batch item uploaded to R2: %s", screenshot_url
                        )
                    except Exception as r2_err:
                        logger.warning(
                            "⚠️ R2 upload failed for batch item, using local URL: %s",
                            r2_err,
                        )
                        screenshot_url = get_screenshot_url(filename)
                else:
                    screenshot_url = get_screenshot_url(filename)

                rec = Screenshot(
                    user_id=current_user.id,
                    url=str(url),
                    screenshot_path=screenshot_path,
                    width=int(result.get("width")  or request.width),
                    height=int(result.get("height") or request.height),
                    format=fmt,
                    full_page=bool(result.get("full_page")),
                    dark_mode=bool(result.get("dark_mode")),
                    status="completed",
                    created_at=result.get("created_at") or datetime.utcnow(),
                    size_bytes=int(result.get("file_size") or 0),
                    storage_url=screenshot_url,
                )
                db.add(rec)
                db.flush()   # ensures rec.id exists before we return it

                results.append({
                    "id":             str(rec.id),
                    "url":            str(url),
                    "screenshot_url": screenshot_url,
                    "status":         "success",
                    "format":         rec.format,
                    "width":          rec.width,
                    "height":         rec.height,
                    "created_at":     rec.created_at.isoformat() if rec.created_at else None,
                })

            except Exception as e:
                logger.error("❌ Failed to capture %s: %s", url, e)
                failed.append({"url": str(url), "status": "failed", "error": str(e)})

        current_user.usage_batch_requests = (current_user.usage_batch_requests or 0) + 1
        current_user.usage_screenshots    = (current_user.usage_screenshots    or 0) + len(results)
        current_user.usage_api_calls      = (current_user.usage_api_calls      or 0) + 1

        db.commit()

        return {
            "batch_id":   f"batch_{int(datetime.utcnow().timestamp())}",
            "total":      len(request.urls),
            "successful": len(results),
            "failed":     len(failed),
            "results":    results,
            "failures":   failed,
        }

    except Exception:
        db.rollback()
        logger.exception(
            "❌ Batch screenshot failed for user %s", current_user.id
        )
        raise HTTPException(
            status_code=500,
            detail="Batch processing failed. Please try again.",
        )


# ── API key regeneration ──────────────────────────────────────────────────────

async def regenerate_api_key_endpoint(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from api_key_system import regenerate_api_key

    user_id = getattr(current_user, "id", None)

    try:
        new_key, new_record = regenerate_api_key(db, user_id)
        db.commit()
        return {
            "api_key":    new_key,
            "key_prefix": new_record.key_prefix,
            "created_at": new_record.created_at.isoformat(),
            "message":    (
                "⚠️ Save this key securely. "
                "Your old key has been deactivated and will no longer work."
            ),
        }
    except Exception as e:
        db.rollback()
        logger.exception(
            "❌ Failed to regenerate API key for user %s: %s", user_id, e
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to regenerate API key. Please try again.",
        )

# ===== END OF screenshot_endpoints.py ========================================
