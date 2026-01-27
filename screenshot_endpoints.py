# backend/screenshot_endpoints.py
# ============================================================================
# SCREENSHOT ENDPOINTS
# ============================================================================
# Author: OneTechly
# Updated: January 2026 - Production Ready
#
# Fixes:
# ✅ db.rollback() on failures (prevents PendingRollbackError)
# ✅ Avoid accessing current_user ORM fields after DB failure
# ✅ Writes required DB fields: size_bytes, storage_url, storage_key
# ✅ Uses Screenshot.id UUID default (from models.py)
# ============================================================================

from fastapi import HTTPException, Depends
from pydantic import BaseModel, HttpUrl, Field
from typing import Optional, List
from datetime import datetime

from sqlalchemy.orm import Session

from models import User, Screenshot, get_db, get_tier_limits
from auth_deps import get_current_user
from screenshot_service import (
    screenshot_service,
    get_screenshot_url,
    increment_user_usage,
    check_usage_limit,
)

import logging
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
    width: int = Field(default=1920, ge=320, le=3840, description="Viewport width (320-3840)")
    height: int = Field(default=1080, ge=240, le=2160, description="Viewport height (240-2160)")
    format: str = Field(default="png", description="Output format: png, jpeg, webp, pdf")
    full_page: bool = Field(default=False, description="Capture full page height")
    dark_mode: bool = Field(default=False, description="Emulate dark mode")

    class Config:
        json_schema_extra = {
            "example": {
                "url": "https://example.com",
                "width": 1920,
                "height": 1080,
                "format": "png",
                "full_page": False,
                "dark_mode": False,
            }
        }

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
    urls: List[HttpUrl] = Field(..., min_length=1, max_length=50, description="List of URLs (max 50)")
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
    # Capture user_id early (avoid touching ORM object after failures)
    user_id = getattr(current_user, "id", None)

    tier = (current_user.subscription_tier or "free").lower()
    tier_limits = get_tier_limits(tier)

    if not check_usage_limit(current_user, tier_limits):
        limit = tier_limits.get("screenshots")
        raise HTTPException(
            status_code=429,
            detail=f"Screenshot limit exceeded ({limit}/month). Upgrade your plan to continue."
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

        filename = result["filename"]
        filepath = result["filepath"]
        created_at = result.get("created_at") or datetime.utcnow()
        file_size = int(result.get("file_size") or 0)

        # This is what your API returns publicly
        public_url = get_screenshot_url(filename)

        screenshot_record = Screenshot(
            user_id=user_id,
            url=str(request.url),
            screenshot_path=filepath,

            width=int(result["width"]),
            height=int(result["height"]),
            format=str(result["format"]),
            full_page=bool(result["full_page"]),
            dark_mode=bool(result["dark_mode"]),

            status="completed",
            created_at=created_at,

            # IMPORTANT: your SQLite schema has NOT NULL constraints for these
            size_bytes=file_size,
            storage_url=public_url,
            storage_key=result.get("storage_key") or filename,
            processing_time_ms=result.get("processing_time_ms"),
        )

        db.add(screenshot_record)

        # Update usage in the same transaction
        increment_user_usage(current_user, db)

        db.commit()
        db.refresh(screenshot_record)

        logger.info("✅ Screenshot created for user %s: %s", user_id, filename)

        return ScreenshotResponse(
            screenshot_id=str(screenshot_record.id),
            screenshot_url=public_url,
            width=int(result["width"]),
            height=int(result["height"]),
            format=str(result["format"]),
            size_bytes=file_size,
            created_at=created_at.isoformat(),
            message="Screenshot captured successfully",
        )

    except ValueError as e:
        db.rollback()
        logger.warning("❌ Screenshot validation error for user %s: %s", user_id, e)
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        db.rollback()
        logger.exception("❌ Unexpected error capturing screenshot for user %s", user_id)
        raise HTTPException(status_code=500, detail="Failed to capture screenshot. Please try again.")


# ============================================================================
# BATCH SCREENSHOT ENDPOINT
# ============================================================================

async def batch_screenshot_endpoint(
    request: BatchScreenshotRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = getattr(current_user, "id", None)

    tier = (current_user.subscription_tier or "free").lower()
    if tier == "free":
        raise HTTPException(
            status_code=403,
            detail="Batch processing requires Pro plan or higher. Upgrade at /pricing"
        )

    if not _screenshot_service_ready():
        _raise_not_ready()

    tier_limits = get_tier_limits(tier)
    batch_limit = tier_limits.get("batch_requests", 0)

    if batch_limit != "unlimited":
        current_batch_usage = current_user.usage_batch_requests or 0
        if current_batch_usage >= batch_limit:
            raise HTTPException(
                status_code=429,
                detail=f"Batch request limit exceeded ({batch_limit}/month). Upgrade to continue."
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

                filename = result["filename"]
                filepath = result["filepath"]
                created_at = result.get("created_at") or datetime.utcnow()
                file_size = int(result.get("file_size") or 0)
                public_url = get_screenshot_url(filename)

                screenshot_record = Screenshot(
                    user_id=user_id,
                    url=str(url),
                    screenshot_path=filepath,
                    width=int(result["width"]),
                    height=int(result["height"]),
                    format=str(result["format"]),
                    full_page=bool(result["full_page"]),
                    dark_mode=bool(result["dark_mode"]),
                    status="completed",
                    created_at=created_at,

                    size_bytes=file_size,
                    storage_url=public_url,
                    storage_key=result.get("storage_key") or filename,
                    processing_time_ms=result.get("processing_time_ms"),
                )
                db.add(screenshot_record)

                results.append({
                    "url": str(url),
                    "screenshot_url": public_url,
                    "status": "success",
                })

            except Exception as e:
                logger.error("❌ Failed to capture %s: %s", url, e)
                failed.append({
                    "url": str(url),
                    "status": "failed",
                    "error": str(e),
                })

        # Update usage counters once
        current_user.usage_batch_requests = (current_user.usage_batch_requests or 0) + 1
        current_user.usage_screenshots = (current_user.usage_screenshots or 0) + len(results)
        current_user.usage_api_calls = (current_user.usage_api_calls or 0) + 1

        db.commit()

    except Exception:
        db.rollback()
        logger.exception("❌ Batch transaction failed for user %s", user_id)
        raise HTTPException(status_code=500, detail="Batch processing failed. Please try again.")

    return {
        "batch_id": f"batch_{int(datetime.utcnow().timestamp())}",
        "total": len(request.urls),
        "successful": len(results),
        "failed": len(failed),
        "results": results,
        "failures": failed,
        "message": f"Batch processing completed: {len(results)}/{len(request.urls)} successful"
    }


# ============================================================================
# REGENERATE API KEY ENDPOINT
# ============================================================================

async def regenerate_api_key_endpoint(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
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
        logger.error("❌ Failed to regenerate API key for user %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Failed to regenerate API key")


# # ================================================================================
# # backend/screenshot_endpoints.py
# # ============================================================================
# # SCREENSHOT ENDPOINTS
# # ============================================================================
# # Author: OneTechly
# # Updated: January 2026 - Production Ready
# #
# # Fixes:
# # ✅ Graceful 503 if Playwright not ready (Render-safe)
# # ✅ Missing datetime import fixed
# # ✅ Clear operator message when browsers are missing
# # ============================================================================

# from fastapi import HTTPException, Depends
# from pydantic import BaseModel, HttpUrl, Field
# from typing import Optional, List
# from datetime import datetime

# from sqlalchemy.orm import Session

# from models import User, Screenshot, get_db, get_tier_limits
# from auth_deps import get_current_user
# from screenshot_service import (
#     screenshot_service,
#     get_screenshot_url,
#     increment_user_usage,
#     check_usage_limit,
# )

# import logging
# logger = logging.getLogger("pixelperfect")

# # ============================================================================
# # INTERNAL HELPERS
# # ============================================================================

# def _screenshot_service_ready() -> bool:
#     """
#     Returns True if screenshot service appears initialized.
#     Works with your ScreenshotService which sets self._initialized.
#     """
#     return bool(getattr(screenshot_service, "_initialized", False))

# def _raise_not_ready():
#     # Very clear message for production ops (Render)
#     raise HTTPException(
#         status_code=503,
#         detail=(
#             "Screenshot service is not ready. Playwright browsers may be missing.\n"
#             "If deploying on Render, ensure your build runs:\n"
#             "  python -m playwright install --with-deps chromium\n"
#             "Then redeploy."
#         ),
#     )

# # ============================================================================
# # PYDANTIC MODELS
# # ============================================================================

# class ScreenshotRequest(BaseModel):
#     url: HttpUrl = Field(..., description="Website URL to screenshot")
#     width: int = Field(default=1920, ge=320, le=3840, description="Viewport width (320-3840)")
#     height: int = Field(default=1080, ge=240, le=2160, description="Viewport height (240-2160)")
#     format: str = Field(default="png", description="Output format: png, jpeg, webp, pdf")
#     full_page: bool = Field(default=False, description="Capture full page height")
#     dark_mode: bool = Field(default=False, description="Emulate dark mode")

#     class Config:
#         json_schema_extra = {
#             "example": {
#                 "url": "https://example.com",
#                 "width": 1920,
#                 "height": 1080,
#                 "format": "png",
#                 "full_page": False,
#                 "dark_mode": False,
#             }
#         }

# class ScreenshotResponse(BaseModel):
#     screenshot_id: str
#     screenshot_url: str
#     width: int
#     height: int
#     format: str
#     size_bytes: int
#     created_at: str
#     message: Optional[str] = None

# class BatchScreenshotRequest(BaseModel):
#     urls: List[HttpUrl] = Field(..., min_length=1, max_length=50, description="List of URLs (max 50)")
#     width: int = Field(default=1920, ge=320, le=3840)
#     height: int = Field(default=1080, ge=240, le=2160)
#     format: str = Field(default="png")
#     full_page: bool = Field(default=False)
#     dark_mode: bool = Field(default=False)

# # ============================================================================
# # SCREENSHOT ENDPOINT
# # ============================================================================

# async def capture_screenshot_endpoint(
#     request: ScreenshotRequest,
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     tier = (current_user.subscription_tier or "free").lower()
#     tier_limits = get_tier_limits(tier)

#     if not check_usage_limit(current_user, tier_limits):
#         limit = tier_limits.get("screenshots")
#         raise HTTPException(
#             status_code=429,
#             detail=f"Screenshot limit exceeded ({limit}/month). Upgrade your plan to continue."
#         )

#     # ✅ If service isn't ready, return 503 instead of crashing.
#     if not _screenshot_service_ready():
#         _raise_not_ready()

#     try:
#         result = await screenshot_service.capture_screenshot(
#             url=str(request.url),
#             width=request.width,
#             height=request.height,
#             format=request.format.lower(),
#             full_page=request.full_page,
#             dark_mode=request.dark_mode,
#         )

#         screenshot_record = Screenshot(
#             user_id=current_user.id,
#             url=str(request.url),
#             screenshot_path=result["filepath"],
#             width=result["width"],
#             height=result["height"],
#             format=result["format"],
#             full_page=result["full_page"],
#             dark_mode=result["dark_mode"],
#             status="completed",
#             created_at=result["created_at"],
#         )
#         db.add(screenshot_record)

#         increment_user_usage(current_user, db)

#         db.commit()
#         db.refresh(screenshot_record)

#         screenshot_url = get_screenshot_url(result["filename"])

#         logger.info("✅ Screenshot created for user %s: %s", current_user.id, result["filename"])

#         return ScreenshotResponse(
#             screenshot_id=str(screenshot_record.id),
#             screenshot_url=screenshot_url,
#             width=result["width"],
#             height=result["height"],
#             format=result["format"],
#             size_bytes=result["file_size"],
#             created_at=result["created_at"].isoformat(),
#             message="Screenshot captured successfully",
#         )

#     except ValueError as e:
#         logger.error("❌ Screenshot error for user %s: %s", current_user.id, e)
#         raise HTTPException(status_code=400, detail=str(e))

#     except Exception:
#         logger.exception("❌ Unexpected error capturing screenshot for user %s", current_user.id)
#         raise HTTPException(status_code=500, detail="Failed to capture screenshot. Please try again.")

# # ============================================================================
# # BATCH SCREENSHOT ENDPOINT
# # ============================================================================

# async def batch_screenshot_endpoint(
#     request: BatchScreenshotRequest,
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     tier = (current_user.subscription_tier or "free").lower()
#     if tier == "free":
#         raise HTTPException(
#             status_code=403,
#             detail="Batch processing requires Pro plan or higher. Upgrade at /pricing"
#         )

#     # ✅ If service isn't ready, return 503 instead of crashing.
#     if not _screenshot_service_ready():
#         _raise_not_ready()

#     tier_limits = get_tier_limits(tier)
#     batch_limit = tier_limits.get("batch_requests", 0)

#     if batch_limit != "unlimited":
#         current_batch_usage = current_user.usage_batch_requests or 0
#         if current_batch_usage >= batch_limit:
#             raise HTTPException(
#                 status_code=429,
#                 detail=f"Batch request limit exceeded ({batch_limit}/month). Upgrade to continue."
#             )

#     results = []
#     failed = []

#     for url in request.urls:
#         try:
#             result = await screenshot_service.capture_screenshot(
#                 url=str(url),
#                 width=request.width,
#                 height=request.height,
#                 format=request.format.lower(),
#                 full_page=request.full_page,
#                 dark_mode=request.dark_mode,
#             )

#             screenshot_record = Screenshot(
#                 user_id=current_user.id,
#                 url=str(url),
#                 screenshot_path=result["filepath"],
#                 width=result["width"],
#                 height=result["height"],
#                 format=result["format"],
#                 full_page=result["full_page"],
#                 dark_mode=result["dark_mode"],
#                 status="completed",
#                 created_at=result["created_at"],
#             )
#             db.add(screenshot_record)

#             results.append({
#                 "url": str(url),
#                 "screenshot_url": get_screenshot_url(result["filename"]),
#                 "status": "success",
#             })

#         except Exception as e:
#             logger.error("❌ Failed to capture %s: %s", url, e)
#             failed.append({
#                 "url": str(url),
#                 "status": "failed",
#                 "error": str(e),
#             })

#     current_user.usage_batch_requests = (current_user.usage_batch_requests or 0) + 1
#     current_user.usage_screenshots = (current_user.usage_screenshots or 0) + len(results)
#     current_user.usage_api_calls = (current_user.usage_api_calls or 0) + 1
#     db.commit()

#     return {
#         "batch_id": f"batch_{int(datetime.utcnow().timestamp())}",
#         "total": len(request.urls),
#         "successful": len(results),
#         "failed": len(failed),
#         "results": results,
#         "failures": failed,
#         "message": f"Batch processing completed: {len(results)}/{len(request.urls)} successful"
#     }

# # ============================================================================
# # REGENERATE API KEY ENDPOINT
# # ============================================================================

# async def regenerate_api_key_endpoint(
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     from api_key_system import regenerate_api_key

#     try:
#         new_key, new_record = regenerate_api_key(db, current_user.id)
#         logger.info("🔄 API key regenerated for user %s", current_user.id)
#         return {
#             "api_key": new_key,
#             "key_prefix": new_record.key_prefix,
#             "created_at": new_record.created_at.isoformat(),
#             "message": "⚠️ Save this key securely. Your old key has been deactivated and will no longer work.",
#         }
#     except Exception as e:
#         logger.error("❌ Failed to regenerate API key for user %s: %s", current_user.id, e)
#         raise HTTPException(status_code=500, detail="Failed to regenerate API key")


