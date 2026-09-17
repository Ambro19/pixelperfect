# =====================================================
# SCREENSHOT ENDPOINTS - PixelPerfect Screenshot API
# File: backend/screenshot_endpoints.py
# Author: OneTechly
# Updated: September 2026
#
# ============================================================================
# ⚠️ ROUTING REALITY CHECK (Sep 2026) — read before trusting this file
# ============================================================================
#   main.py registers routes in this order:
#
#       app.include_router(batch_router, prefix="/api/v1")   # batch.py
#       ...
#       @app.post("/api/v1/batch/submit")                    # → this file
#
#   Starlette matches in registration order, so batch.py's POST /batch/submit
#   wins and batch_screenshot_endpoint() below NEVER RUNS for real traffic.
#   The July 2026 header used to claim the opposite ("main.py routes
#   POST /api/v1/batch/submit through this function"), which meant its PDF
#   tier gate was believed to be protecting a path it never saw.
#
#   That gate now also exists in batch.py, where it actually applies. The
#   endpoint below is kept for backward compatibility and for anything that
#   calls it directly, but treat batch.py as the live batch implementation.
#
#   Likewise, single captures go through routers/screenshot.py via the main.py
#   wrapper — capture_screenshot_endpoint() below is the legacy path.
#
# ============================================================================
# ✅ NEW (Sep 2026 — capture-engine failures return 503, not 400/500)
# ============================================================================
#   A dead browser ("Target page, context or browser has been closed") or a
#   thread-affinity fault ("Cannot switch to a different thread") used to come
#   back as 400 (because screenshot_service wraps most Playwright errors in
#   ValueError) or as a generic 500. Both are retryable server-side faults and
#   neither is the caller's mistake. They now return 503 with Retry-After.
#
#   Root causes are fixed in screenshot_service.py: one Playwright worker
#   thread, automatic browser relaunch, and a full-page capture budget.
#
# ✅ NEW (Sep 2026 — trimmed captures are reported)
#   Pages taller than the capture budget are trimmed instead of killing
#   Chromium; the response message says so.
#
# ✅ CLEANUP (Sep 2026) — removed the commented-out check_usage_limit attempts
#   left over from the Aug 2026 fix. The live call passes db, which is what
#   makes the limit period-scoped and consistent with the dashboard.
#
# ----------------------------------------------------------------------------
# Earlier fixes (all retained)
# ----------------------------------------------------------------------------
# ✅ FIX (Jul 2026 — PDF Tier Gate): PDF requires Pro+ via has_feature();
#    TIER_FEATURES in models.py is the authoritative mapping.
# ✅ FIX (Apr 2026): single capture uploads to R2, so URLs survive redeploys.
#    Previously they pointed at Render's ephemeral disk and 404'd after every
#    deploy.
# ✅ NEW (Apr 2026): `delay` and `remove_elements` wired through on both
#    request models and passed to the service.
# =====================================================

from datetime import datetime
from pathlib import Path
import logging
from typing import List, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy.orm import Session

from auth_deps import get_current_user
from models import Screenshot, User, get_db, get_tier_limits, has_feature
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

# ── Hard limits for remove_elements (must match screenshot_service.py) ────────
_MAX_REMOVE_ELEMENTS_COUNT   = 20
_MAX_REMOVE_ELEMENT_SELECTOR = 200

# ── Capture-engine failure detection (Sep 2026) ───────────────────────────────
# greenlet.error is raised when Playwright's sync API is touched from a foreign
# thread and is not a PlaywrightError subclass, so detection is by message.
_ENGINE_FAILURE_MARKERS = (
    "cannot switch to a different thread",
    "target page, context or browser has been closed",
    "browser has been closed",
    "target closed",
    "browser has disconnected",
    "playwright browser is not initialized",
)


def _is_engine_failure(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _ENGINE_FAILURE_MARKERS)


def _engine_failure_http() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=(
            "The capture engine was unavailable for this request and the page "
            "was not captured. This was not caused by the website. Please try "
            "again in a moment."
        ),
        headers={"Retry-After": "5"},
    )


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


def _validate_remove_elements(value: Optional[List[str]]) -> Optional[List[str]]:
    """
    Shared validator for remove_elements. Returns a cleaned list (or None).

    Bad entries are dropped silently rather than rejecting the whole request —
    the frontend may send slightly malformed input and succeeding beats a 422.
    The screenshot service also sanitizes, so this is defense in depth.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        return None

    cleaned: List[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        stripped = item.strip()
        if not stripped:
            continue
        if len(stripped) > _MAX_REMOVE_ELEMENT_SELECTOR:
            stripped = stripped[:_MAX_REMOVE_ELEMENT_SELECTOR]
        cleaned.append(stripped)
        if len(cleaned) >= _MAX_REMOVE_ELEMENTS_COUNT:
            break

    return cleaned or None


def _capture_message(result: dict, default: str) -> str:
    """✅ NEW (Sep 2026): say when a capture was trimmed to the capture budget."""
    if result.get("truncated"):
        doc_h = result.get("document_height") or 0
        return (
            f"Screenshot captured, but the page is {doc_h}px tall and was "
            f"trimmed to fit the capture limit at this width. Reduce the width "
            f"or capture a specific section to get the whole page."
        )
    return default


class ScreenshotRequest(BaseModel):
    url: HttpUrl = Field(..., description="Website URL to screenshot")
    width:     int  = Field(default=1920, ge=320, le=3840)
    height:    int  = Field(default=1080, ge=240, le=2160)
    format:    str  = Field(default="png", description="png, jpeg, webp, pdf")
    full_page: bool = Field(default=False)
    dark_mode: bool = Field(default=False)

    delay: Optional[int] = Field(
        default=None,
        ge=0,
        le=10,
        description="Seconds to wait after page load before capture (0–10).",
    )
    remove_elements: Optional[List[str]] = Field(
        default=None,
        description=(
            "CSS selectors for elements to hide before capture "
            "(e.g. cookie banners, popups). Max 20 selectors, each ≤200 chars."
        ),
    )

    @field_validator("remove_elements")
    @classmethod
    def _clean_remove_elements(cls, v):
        return _validate_remove_elements(v)


class ScreenshotResponse(BaseModel):
    screenshot_id:  str
    screenshot_url: str
    width:          int
    height:         int
    format:         str
    size_bytes:     int
    created_at:     str
    message:        Optional[str] = None
    # ✅ NEW (Sep 2026)
    truncated:       bool          = False
    document_height: Optional[int] = None


class BatchScreenshotRequest(BaseModel):
    urls:      List[HttpUrl] = Field(..., min_length=1, max_length=50)
    width:     int  = Field(default=1920, ge=320, le=3840)
    height:    int  = Field(default=1080, ge=240, le=2160)
    format:    str  = Field(default="png")
    full_page: bool = Field(default=False)
    dark_mode: bool = Field(default=False)

    delay: Optional[int] = Field(
        default=None,
        ge=0,
        le=10,
        description="Seconds to wait after page load before each capture (0–10).",
    )
    remove_elements: Optional[List[str]] = Field(
        default=None,
        description=(
            "CSS selectors for elements to hide before capture. "
            "Applied to every URL in the batch. Max 20 selectors, each ≤200 chars."
        ),
    )

    @field_validator("remove_elements")
    @classmethod
    def _clean_remove_elements(cls, v):
        return _validate_remove_elements(v)


# ── Single screenshot capture (LEGACY — production uses routers/screenshot.py) ─

async def capture_screenshot_endpoint(
    request: ScreenshotRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tier        = (current_user.subscription_tier or "free").lower()
    tier_limits = get_tier_limits(tier)

    # db is required here: without it check_usage_limit falls back to the
    # lifetime counter, which never resets for Free users.
    if not check_usage_limit(current_user, tier_limits, db=db):
        limit = tier_limits.get("screenshots")
        raise HTTPException(
            status_code=429,
            detail=f"Screenshot limit exceeded ({limit}/month). Upgrade your plan to continue.",
        )

    # PDF tier gate: PDF requires Pro+ (Pro, Business, Premium).
    if request.format.lower() == "pdf" and not has_feature(current_user, "pdf"):
        raise HTTPException(
            status_code=403,
            detail="PDF generation requires Pro tier or higher. Please upgrade.",
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
            delay=request.delay,
            remove_elements=request.remove_elements,
        )

        filename        = result["filename"]
        screenshot_path = result.get("filepath")
        fmt             = str(result.get("format") or request.format).lower()

        # ── 2. Upload to R2 if configured; fall back to local URL ─────────
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
                logger.warning(
                    "⚠️ R2 upload failed for single capture, using local URL: %s",
                    r2_err,
                )
                screenshot_url = get_screenshot_url(filename)
        else:
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
            storage_url=screenshot_url,
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
            message=_capture_message(result, "Screenshot captured successfully"),
            truncated=bool(result.get("truncated")),
            document_height=result.get("document_height") or None,
        )

    except ValueError as e:
        db.rollback()
        # ✅ NEW (Sep 2026): the service wraps most Playwright failures in
        # ValueError, so an engine fault would otherwise be reported as a 400
        # — the caller's fault, which it is not.
        if _is_engine_failure(e):
            logger.error("💥 Capture engine failure for %s: %s", request.url, e)
            raise _engine_failure_http()
        raise HTTPException(status_code=400, detail=str(e))

    except HTTPException:
        raise

    except Exception as e:
        db.rollback()
        if _is_engine_failure(e):
            logger.error(
                "💥 Capture engine failure for %s: %s", request.url, e, exc_info=True
            )
            raise _engine_failure_http()
        logger.exception(
            "❌ Unexpected error capturing screenshot for user %s",
            current_user.id,
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to capture screenshot. Please try again.",
        )


# ── Batch screenshot capture ──────────────────────────────────────────────────
# ⚠️ LEGACY / UNREACHABLE for POST /api/v1/batch/submit — batch.py's router is
# registered first and serves that path. See the routing note in the header.
# Kept for backward compatibility and direct callers.

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

    if request.format.lower() == "pdf" and not has_feature(current_user, "pdf"):
        raise HTTPException(
            status_code=403,
            detail="PDF generation requires Pro tier or higher. Please upgrade.",
        )

    if not screenshot_service.is_ready():
        _raise_not_ready(screenshot_service.last_error())

    tier_limits  = get_tier_limits(tier)
    batch_limit  = tier_limits.get("batch_requests", 0)
    # Period-scoped and unlimited-safe: comparing an int with the string
    # "unlimited" raised TypeError for Premium before the try block, which
    # surfaced as an unhandled 500.
    if batch_limit not in ("unlimited", None):
        from usage_accounting import batch_used_this_period
        current_batch_usage = batch_used_this_period(db, current_user)
        if current_batch_usage >= int(batch_limit):
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
                    delay=request.delay,
                    remove_elements=request.remove_elements,
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
                    "truncated":      bool(result.get("truncated")),      # ✅ Sep 2026
                    "message":        _capture_message(result, "Screenshot captured successfully"),
                })

            except Exception as e:
                logger.error("❌ Failed to capture %s: %s", url, e)
                failed.append({"url": str(url), "status": "failed", "error": str(e)})

        # Legacy counters — kept in sync for backward compatibility only. Real
        # usage is derived from the Screenshot rows written above; see
        # usage_accounting.screenshots_used_this_period().
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

    except HTTPException:
        raise

    except Exception as e:
        db.rollback()
        if _is_engine_failure(e):
            logger.error("💥 Capture engine failure during batch: %s", e, exc_info=True)
            raise _engine_failure_http()
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

# ===== END OF screenshot_endpoints.py ======


# # =====================================================
# # SCREENSHOT ENDPOINTS - PixelPerfect Screenshot API
# # File: backend/screenshot_endpoints.py
# # Author: OneTechly
# # Updated: July 2026 - PRODUCTION READY
# #
# # ✅ FIX (Jul 2026 — PDF Tier Gate: PDF requires Pro+):
# #   Added has_feature import from models and PDF format gates to BOTH
# #   capture_screenshot_endpoint and batch_screenshot_endpoint.
# #   PDF is available on: Pro, Business, Premium. Blocked on: Free.
# #   The authoritative feature mapping lives in models.py TIER_FEATURES
# #   (pro.pdf = True as of Jul 2026) — this file only calls has_feature().
# #   NOTE: single-capture requests in production route through
# #   routers/screenshot.py (main.py v9 wrapper); the gate here covers the
# #   legacy path and, critically, the batch path which still uses this file.
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
# from models import Screenshot, User, get_db, get_tier_limits, has_feature
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

#     #if not check_usage_limit(current_user, tier_limits):
#     # ✅ FIX (Aug 2026): pass db so the limit is period-scoped and agrees
#     # with the dashboard and routers/screenshot.py. Without db this silently
#     # falls back to the lifetime counter that never resets for Free users.
#    # if not check_usage_limit(current_user, tier_limits, db):
#     if not check_usage_limit(current_user, tier_limits, db=db):    
#         limit = tier_limits.get("screenshots")
#         raise HTTPException(
#             status_code=429,
#             detail=f"Screenshot limit exceeded ({limit}/month). Upgrade your plan to continue.",
#         )

#     # ✅ PDF tier gate (Jul 2026): PDF requires Pro+ (Pro, Business, Premium).
#     # has_feature() reads TIER_FEATURES in models.py — the single source of truth.
#     if request.format.lower() == "pdf" and not has_feature(current_user, "pdf"):
#         raise HTTPException(
#             status_code=403,
#             detail="PDF generation requires Pro tier or higher. Please upgrade.",
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

# # ✅ FIX (Aug 2026):Legacy counters — kept in sync for backward compatibility only.
# # Real usage is derived from the Screenshot rows written above; see
# # usage_accounting.screenshots_used_this_period().

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

#     # ✅ PDF tier gate (Jul 2026): PDF requires Pro+ (Pro, Business, Premium).
#     # This is the ONLY PDF gate on the batch path — main.py routes
#     # POST /api/v1/batch/submit through this function.
#     if request.format.lower() == "pdf" and not has_feature(current_user, "pdf"):
#         raise HTTPException(
#             status_code=403,
#             detail="PDF generation requires Pro tier or higher. Please upgrade.",
#         )

#     if not screenshot_service.is_ready():
#         _raise_not_ready(screenshot_service.last_error())

#     tier_limits  = get_tier_limits(tier)
#     batch_limit  = tier_limits.get("batch_requests", 0)
#     # ✅ FIX (Aug 2026): was reading the lifetime usage_batch_requests counter
#     # and comparing it with >= against a limit that can be the STRING
#     # "unlimited" — a TypeError for Premium, raised before the try block, so
#     # it surfaced as an unhandled 500. Now period-scoped and unlimited-safe,
#     # matching the single-capture path.
#     if batch_limit not in ("unlimited", None):
#         from usage_accounting import batch_used_this_period
#         current_batch_usage = batch_used_this_period(db, current_user)
#         if current_batch_usage >= int(batch_limit):
#     # if batch_limit != "unlimited":
#     #     current_batch_usage = current_user.usage_batch_requests or 0
#     #     if current_batch_usage >= batch_limit:
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

