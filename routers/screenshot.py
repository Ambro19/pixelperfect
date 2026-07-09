# backend/routers/screenshot.py
# PixelPerfect Screenshot API Router — Phase 1 + Phase 2 Advanced Features
# Author: OneTechly
# Updated: July 2026
#
# ✅ FIX (Jul 2026 — PDF Tier Gate messaging):
#   Enforcement was already correct — has_feature(current_user, "pdf") reads
#   TIER_FEATURES in models.py, which now grants PDF to Pro+ (Pro, Business,
#   Premium). But the 403 error message and the docstring tier table still
#   said "Business tier", which would mislead Free users into buying the
#   wrong plan. Message and docs updated to "Pro tier or higher".
#
# ✅ Phase 1 changes vs January 2026 scaffolding:
#   - Added has_feature from models (replaces inline check_feature_access dict)
#   - Added asyncio, hashlib, hmac, json (for webhook retry + HMAC signing)
#   - ScreenshotRequest: added webhook_secret field
#   - ScreenshotResponse: added js_warning: Optional[str]
#   - check_feature_access() replaced by has_feature() from models.py
#   - send_webhook_notification(): exponential-backoff retry, HMAC-SHA256 signing
#   - create_screenshot(): all tier gates use has_feature(), Phase 1 params wired
#
# ✅ Phase 2 changes (May 2026):
#   - ScreenshotResponse: added element_selector: Optional[str] = None
#     Root cause of TC-EL-* failures: the field existed in the service return dict
#     but was missing from the Pydantic response model, so it was silently dropped
#     from every API response. PowerShell read it as empty string → all 14 tests failed.
#   - create_screenshot(): extracts element_selector from result dict and passes it
#     to ScreenshotResponse and to the webhook payload.
#   - Webhook payload: element_selector now included in data block.
#   - get_usage_stats(): element_selection and webhooks now correctly reported
#     for business tier (was already working via has_feature — no change needed).

import asyncio
import hashlib
import hmac
import json
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, HttpUrl, Field

from auth_deps import get_current_user
from models import User, get_db, Screenshot, get_tier_limits, has_feature
from services.screenshot_service import screenshot_service
from services.storage_service import storage_service

logger = logging.getLogger("pixelperfect")

router = APIRouter(prefix="/api/v1/screenshot", tags=["Screenshot"])


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class ScreenshotRequest(BaseModel):
    """Complete screenshot request model with all advertised features."""
    url: HttpUrl
    width: int = Field(default=1920, ge=320, le=3840)
    height: int = Field(default=1080, ge=240, le=2160)
    full_page: bool = False
    format: str = Field(default="png", pattern="^(png|jpeg|webp|pdf)$")
    quality: Optional[int] = Field(default=None, ge=0, le=100)
    delay: int = Field(default=0, ge=0, le=10)
    dark_mode: bool = False
    remove_elements: Optional[List[str]] = None
    return_url: bool = True

    # Phase 1 — Pro+ features
    device: Optional[str] = Field(
        default=None,
        description="Device preset (Pro+). Overrides width/height. "
                    "See GET /api/v1/screenshot/devices for the full list.",
    )
    custom_js: Optional[str] = Field(
        default=None,
        max_length=10000,
        description="JavaScript to execute before capture (Pro+). "
                    "Errors are non-fatal — screenshot still captures and "
                    "js_warning is populated in the response.",
    )
    wait_for_selector: Optional[str] = Field(
        default=None,
        max_length=200,
        description="CSS selector to wait for before capture (Pro+).",
    )

    # Phase 2 — Business+
    target_element: Optional[str] = Field(
        default=None,
        max_length=200,
        description="CSS selector to crop to (Business+). "
                    "The full page is captured first, then Pillow crops to this element.",
    )

    # Phase 3 — Business+ webhook
    webhook_url: Optional[str] = Field(
        default=None,
        description="POST completion payload to this URL (Business+).",
    )
    webhook_secret: Optional[str] = Field(
        default=None,
        max_length=200,
        description="HMAC-SHA256 secret. When set, responses include "
                    "X-PixelPerfect-Signature for verification.",
    )


class ScreenshotResponse(BaseModel):
    """
    Screenshot response model.

    ✅ Phase 2 (May 2026): added element_selector field.
       Without this field the Pydantic model silently dropped element_selector
       from all API responses even though the service returned it correctly,
       causing every TC-EL-* test to fail with element_selector=''.
    """
    url: str
    screenshot_url: Optional[str] = None
    screenshot_id: str
    width: int
    height: int
    format: str
    size_bytes: int
    created_at: str
    usage: dict
    device_used: Optional[str] = None
    js_warning: Optional[str] = None       # Phase 1: non-None when custom_js threw
    element_selector: Optional[str] = None # Phase 2: selector used for crop, or None


class DeviceListResponse(BaseModel):
    devices: List[str]
    descriptions: Dict[str, str]


# ============================================================================
# HELPERS
# ============================================================================

def check_user_screenshot_limit(user: User) -> tuple[bool, int, int]:
    """Return (allowed, current_count, limit)."""
    tier_limits = get_tier_limits(user.subscription_tier or "free")
    current = user.usage_screenshots or 0
    limit = tier_limits["screenshots"]
    return current < limit, current, limit


def increment_user_usage(user: User, db, usage_type: str = "screenshots"):
    """Increment usage counter and commit."""
    if usage_type == "screenshots":
        user.usage_screenshots = (user.usage_screenshots or 0) + 1
    elif usage_type == "batch_requests":
        user.usage_batch_requests = (user.usage_batch_requests or 0) + 1
    user.usage_api_calls = (user.usage_api_calls or 0) + 1
    db.commit()


async def send_webhook_notification(
    webhook_url: str,
    screenshot_data: Dict[str, Any],
    secret: Optional[str] = None,
    max_retries: int = 3,
) -> None:
    """
    POST screenshot completion payload to caller's webhook URL.

    Security:
      - HMAC-SHA256 body signature → X-PixelPerfect-Signature: sha256=<hex>
      - UTC timestamp → X-PixelPerfect-Timestamp (replay-attack mitigation)
        Signature input: f"{timestamp}.".encode() + body_bytes

    Reliability:
      - Exponential backoff: 2s → 4s → 8s between attempts
      - Permanent failure is logged but never raises (background task)
    """
    if not webhook_url:
        return

    payload = {
        "event": "screenshot.completed",
        "data": screenshot_data,
        "timestamp": datetime.utcnow().isoformat(),
    }
    body = json.dumps(payload, sort_keys=True).encode()
    ts = str(int(datetime.now(timezone.utc).timestamp()))

    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": "PixelPerfect-Webhook/1.0",
        "X-PixelPerfect-Timestamp": ts,
    }
    if secret:
        sig_input = f"{ts}.".encode() + body
        sig = hmac.new(secret.encode(), sig_input, hashlib.sha256).hexdigest()
        headers["X-PixelPerfect-Signature"] = f"sha256={sig}"

    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    webhook_url, content=body, headers=headers, timeout=10.0
                )
            if resp.is_success:
                logger.info(
                    "✅ Webhook delivered on attempt %d: %s", attempt, webhook_url
                )
                return
            logger.warning(
                "⚠️ Webhook attempt %d/%d → HTTP %d: %s",
                attempt, max_retries, resp.status_code, webhook_url,
            )
        except Exception as exc:
            logger.warning(
                "⚠️ Webhook attempt %d/%d failed: %s — %s",
                attempt, max_retries, webhook_url, exc,
            )

        if attempt < max_retries:
            await asyncio.sleep(2 ** attempt)  # 2s, 4s, 8s

    logger.error(
        "❌ Webhook permanently failed after %d attempts: %s", max_retries, webhook_url
    )


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/", response_model=ScreenshotResponse)
async def create_screenshot(
    request: ScreenshotRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Capture a screenshot with all advertised features.

    **Tier gates**
    | Feature          | Minimum tier |
    |---|---|
    | `custom_js`      | Pro |
    | `device`         | Pro |
    | PDF format       | Pro |
    | `target_element` | Business |
    | `webhook_url`    | Business |

    **JavaScript errors** are non-fatal (option-c). If `custom_js` throws,
    the screenshot still captures and `js_warning` contains the error.

    **Element selection** crops the screenshot to the element's bounding box.
    Returns HTTP 400 if the selector matches nothing or the element has zero size.

    **Webhook delivery** uses exponential-backoff retry (3 attempts) and
    optional HMAC-SHA256 signing via `webhook_secret`.
    """

    # ── Usage limit ───────────────────────────────────────────────────────────
    can_use, current, limit = check_user_screenshot_limit(current_user)
    if not can_use:
        raise HTTPException(
            status_code=429,
            detail=f"Screenshot limit reached ({current}/{limit}). Please upgrade your plan.",
        )

    # ── Tier gates (all via has_feature — single source of truth) ─────────────
    tier_limits = get_tier_limits(current_user.subscription_tier or "free")

    # ✅ FIX (Jul 2026): PDF is Pro+, not Business-only. Enforcement was already
    # correct via has_feature (models.py TIER_FEATURES["pro"]["pdf"] = True);
    # only the error message needed updating.
    if request.format == "pdf" and not has_feature(current_user, "pdf"):
        raise HTTPException(
            status_code=403,
            detail="PDF generation requires Pro tier or higher. Please upgrade.",
        )

    if request.custom_js and not has_feature(current_user, "custom_js"):
        raise HTTPException(
            status_code=403,
            detail="Custom JavaScript execution requires Pro tier or higher. Please upgrade.",
        )

    if request.device and not has_feature(current_user, "device_emulation"):
        raise HTTPException(
            status_code=403,
            detail="Device emulation requires Pro tier or higher. Please upgrade.",
        )

    if request.target_element and not has_feature(current_user, "element_selection"):
        raise HTTPException(
            status_code=403,
            detail="Element selection requires Business tier. Please upgrade.",
        )

    if request.webhook_url and not has_feature(current_user, "webhooks"):
        raise HTTPException(
            status_code=403,
            detail="Webhook notifications require Business tier. Please upgrade.",
        )

    if request.width > tier_limits.get("max_width", 1920):
        raise HTTPException(
            status_code=400,
            detail=f"Width exceeds tier limit ({tier_limits.get('max_width', 1920)}px). Please upgrade.",
        )

    # ── Capture ───────────────────────────────────────────────────────────────
    try:
        if not screenshot_service.is_ready():
            logger.info("🔧 Initializing Playwright browser…")
            await screenshot_service.initialize()

        start_time = datetime.utcnow()

        # Service returns Dict[str, Any] with keys:
        #   filename, filepath, url, width, height, format, full_page,
        #   dark_mode, file_size, created_at,
        #   js_warning        ← Phase 1
        #   element_selector  ← Phase 2 (None when target_element not used)
        result = await screenshot_service.capture_screenshot(
            url=str(request.url),
            width=request.width,
            height=request.height,
            full_page=request.full_page,
            format=request.format,
            delay=request.delay,
            dark_mode=request.dark_mode,
            remove_elements=request.remove_elements,
            # Phase 1 params
            device=request.device,
            custom_js=request.custom_js,
            wait_for_selector=request.wait_for_selector,
            # Phase 2 param
            target_element=request.target_element,
        )

        # Extract Phase 1 + Phase 2 fields from result dict
        js_warning: Optional[str]       = result.get("js_warning")
        element_selector: Optional[str] = result.get("element_selector")  # ← Phase 2

        # Read captured bytes from disk
        from pathlib import Path
        screenshot_bytes = Path(result["filepath"]).read_bytes()

        processing_time = (datetime.utcnow() - start_time).total_seconds() * 1000

        # ── Storage ───────────────────────────────────────────────────────────
        screenshot_id = str(uuid.uuid4())
        filename = f"screenshots/{current_user.id}/{screenshot_id}.{request.format}"
        content_type = (
            "application/pdf" if request.format == "pdf" else f"image/{request.format}"
        )

        try:
            screenshot_url = await storage_service.upload_screenshot(
                file_data=screenshot_bytes,
                filename=filename,
                content_type=content_type,
            )
            storage_key = filename
        except Exception as upload_err:
            logger.warning("R2 upload failed, falling back to local storage: %s", upload_err)
            local_dir = Path("screenshots") / str(current_user.id)
            local_dir.mkdir(parents=True, exist_ok=True)
            local_path = local_dir / f"{screenshot_id}.{request.format}"
            local_path.write_bytes(screenshot_bytes)
            screenshot_url = f"/screenshots/{current_user.id}/{screenshot_id}.{request.format}"
            storage_key = str(local_path)

        # ── Database ──────────────────────────────────────────────────────────
        retention_days = tier_limits.get("screenshot_retention_days", 7)
        expires_at = datetime.utcnow() + timedelta(days=retention_days)

        screenshot_record = Screenshot(
            id=screenshot_id,
            user_id=current_user.id,
            url=str(request.url),
            width=request.width if not request.device else None,
            height=request.height if not request.device else None,
            full_page=request.full_page,
            format=request.format,
            quality=request.quality,
            delay_seconds=request.delay,
            dark_mode=request.dark_mode,
            size_bytes=len(screenshot_bytes),
            storage_url=screenshot_url,
            storage_key=storage_key,
            processing_time_ms=processing_time,
            status="completed",
            expires_at=expires_at,
            created_at=datetime.utcnow(),
        )
        db.add(screenshot_record)
        increment_user_usage(current_user, db, "screenshots")
        db.refresh(screenshot_record)
        db.refresh(current_user)

        logger.info(
            "✅ Screenshot created: %s for user %s (element=%s)",
            screenshot_id, current_user.id,
            repr(element_selector) if element_selector else "none",
        )

        # ── Webhook (background, Business+) ───────────────────────────────────
        if request.webhook_url:
            background_tasks.add_task(
                send_webhook_notification,
                webhook_url=request.webhook_url,
                screenshot_data={
                    "screenshot_id":   screenshot_id,
                    "url":             str(request.url),
                    "screenshot_url":  screenshot_url,
                    "format":          request.format,
                    "size_bytes":      len(screenshot_bytes),
                    "processing_time_ms": processing_time,
                    "js_warning":      js_warning,
                    "element_selector": element_selector,  # ← Phase 2 in webhook payload
                },
                secret=request.webhook_secret,
            )

        return ScreenshotResponse(
            url=str(request.url),
            screenshot_url=screenshot_url if request.return_url else None,
            screenshot_id=screenshot_id,
            width=request.width,
            height=request.height,
            format=request.format,
            size_bytes=len(screenshot_bytes),
            created_at=screenshot_record.created_at.isoformat(),
            device_used=request.device,
            js_warning=js_warning,
            element_selector=element_selector,   # ← Phase 2: the key that was missing
            usage={
                "current":   current_user.usage_screenshots,
                "limit":     limit,
                "remaining": limit - current_user.usage_screenshots,
            },
        )

    except httpx.HTTPError as exc:
        logger.error("HTTP error loading URL %s: %s", request.url, exc)
        raise HTTPException(status_code=400, detail=f"Failed to load URL: {exc}")
    except ValueError as exc:
        logger.error("Capture validation error: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Screenshot failed: %s", exc, exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Screenshot failed: {exc}")


@router.get("/devices", response_model=DeviceListResponse)
async def list_devices(current_user: User = Depends(get_current_user)):
    """List available device presets. Requires Pro tier or higher."""
    if not has_feature(current_user, "device_emulation"):
        raise HTTPException(
            status_code=403,
            detail="Device emulation requires Pro tier or higher. Please upgrade.",
        )
    devices = screenshot_service.get_available_devices()
    descriptions = {
        "iphone_13":         "iPhone 13 (390×844, 3× DPR, Safari UA)",
        "iphone_13_pro_max": "iPhone 13 Pro Max (428×926, 3× DPR, Safari UA)",
        "iphone_se":         "iPhone SE (375×667, 2× DPR, Safari UA)",
        "pixel_5":           "Google Pixel 5 (393×851, 2.75× DPR, Chrome UA)",
        "pixel_7":           "Google Pixel 7 (412×915, 2.625× DPR, Chrome UA)",
        "ipad_pro":          "iPad Pro 11\" (1024×1366, 2× DPR, Safari UA)",
        "ipad_mini":         "iPad Mini (768×1024, 2× DPR, Safari UA)",
        "galaxy_s9":         "Samsung Galaxy S9+ (320×658, 4.5× DPR, Chrome UA)",
        "galaxy_tab_s4":     "Samsung Galaxy Tab S4 (712×1138, 2.25× DPR, Chrome UA)",
    }
    return DeviceListResponse(devices=devices, descriptions=descriptions)


@router.get("/stats/usage")
async def get_usage_stats(
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Detailed usage statistics including feature access flags."""
    tier_limits = get_tier_limits(current_user.subscription_tier or "free")
    screenshots_used  = current_user.usage_screenshots or 0
    screenshots_limit = tier_limits["screenshots"]
    pct = (
        round((screenshots_used / screenshots_limit) * 100, 1)
        if screenshots_limit and screenshots_limit != "unlimited"
        else 0
    )

    return {
        "tier": current_user.subscription_tier or "free",
        "usage": {
            "screenshots": {
                "used":      screenshots_used,
                "limit":     screenshots_limit,
                "remaining": max(0, screenshots_limit - screenshots_used)
                if screenshots_limit != "unlimited" else "unlimited",
                "percentage": pct,
            },
            "batch_requests": {
                "used":  current_user.usage_batch_requests or 0,
                "limit": tier_limits["batch_requests"],
                "remaining": max(
                    0,
                    (tier_limits["batch_requests"] or 0) - (current_user.usage_batch_requests or 0),
                ) if tier_limits["batch_requests"] != "unlimited" else "unlimited",
            },
            "api_calls": {"used": current_user.usage_api_calls or 0},
        },
        "limits": tier_limits,
        "reset_date": (
            current_user.usage_reset_at.isoformat()
            if current_user.usage_reset_at else None
        ),
        "features": {
            "custom_js":         has_feature(current_user, "custom_js"),
            "device_emulation":  has_feature(current_user, "device_emulation"),
            "element_selection": has_feature(current_user, "element_selection"),
            "pdf":               has_feature(current_user, "pdf"),
            "webhooks":          has_feature(current_user, "webhooks"),
            "white_label":       has_feature(current_user, "white_label"),
        },
    }


@router.get("/{screenshot_id}")
async def get_screenshot(
    screenshot_id: str,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Retrieve screenshot metadata by ID."""
    screenshot = (
        db.query(Screenshot)
        .filter(Screenshot.id == screenshot_id, Screenshot.user_id == current_user.id)
        .first()
    )
    if not screenshot:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    return {
        "id":                screenshot.id,
        "url":               screenshot.url,
        "screenshot_url":    screenshot.storage_url,
        "width":             screenshot.width,
        "height":            screenshot.height,
        "format":            screenshot.format,
        "size_bytes":        screenshot.size_bytes,
        "status":            screenshot.status,
        "processing_time_ms": screenshot.processing_time_ms,
        "created_at":        screenshot.created_at.isoformat(),
        "expires_at":        screenshot.expires_at.isoformat() if screenshot.expires_at else None,
    }


@router.get("/")
async def list_screenshots(
    limit: int = 20,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """List screenshots for the authenticated user."""
    q = db.query(Screenshot).filter(Screenshot.user_id == current_user.id)
    total = q.count()
    screenshots = q.order_by(Screenshot.created_at.desc()).limit(limit).offset(offset).all()

    return {
        "screenshots": [
            {
                "id":             s.id,
                "url":            s.url,
                "screenshot_url": s.storage_url,
                "width":          s.width,
                "height":         s.height,
                "format":         s.format,
                "size_bytes":     s.size_bytes,
                "status":         s.status,
                "created_at":     s.created_at.isoformat(),
            }
            for s in screenshots
        ],
        "total":  total,
        "limit":  limit,
        "offset": offset,
    }


@router.delete("/{screenshot_id}")
async def delete_screenshot(
    screenshot_id: str,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Delete a screenshot and its storage file."""
    screenshot = (
        db.query(Screenshot)
        .filter(Screenshot.id == screenshot_id, Screenshot.user_id == current_user.id)
        .first()
    )
    if not screenshot:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    try:
        if screenshot.storage_key:
            await storage_service.delete_screenshot(screenshot.storage_key)
    except Exception as exc:
        logger.warning("Failed to delete from storage %s: %s", screenshot_id, exc)

    db.delete(screenshot)
    db.commit()
    logger.info("🗑️ Screenshot deleted: %s", screenshot_id)
    return {"status": "deleted", "screenshot_id": screenshot_id}

# ===== END OF routers/screenshot.py ==========================================

# # backend/routers/screenshot.py
# # PixelPerfect Screenshot API Router — Phase 1 + Phase 2 Advanced Features
# # Author: OneTechly
# # Updated: May 2026
# #
# # ✅ Phase 1 changes vs January 2026 scaffolding:
# #   - Added has_feature from models (replaces inline check_feature_access dict)
# #   - Added asyncio, hashlib, hmac, json (for webhook retry + HMAC signing)
# #   - ScreenshotRequest: added webhook_secret field
# #   - ScreenshotResponse: added js_warning: Optional[str]
# #   - check_feature_access() replaced by has_feature() from models.py
# #   - send_webhook_notification(): exponential-backoff retry, HMAC-SHA256 signing
# #   - create_screenshot(): all tier gates use has_feature(), Phase 1 params wired
# #
# # ✅ Phase 2 changes (May 2026):
# #   - ScreenshotResponse: added element_selector: Optional[str] = None
# #     Root cause of TC-EL-* failures: the field existed in the service return dict
# #     but was missing from the Pydantic response model, so it was silently dropped
# #     from every API response. PowerShell read it as empty string → all 14 tests failed.
# #   - create_screenshot(): extracts element_selector from result dict and passes it
# #     to ScreenshotResponse and to the webhook payload.
# #   - Webhook payload: element_selector now included in data block.
# #   - get_usage_stats(): element_selection and webhooks now correctly reported
# #     for business tier (was already working via has_feature — no change needed).

# import asyncio
# import hashlib
# import hmac
# import json
# import uuid
# import logging
# from datetime import datetime, timezone, timedelta
# from typing import Optional, List, Dict, Any

# import httpx
# from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
# from pydantic import BaseModel, HttpUrl, Field

# from auth_deps import get_current_user
# from models import User, get_db, Screenshot, get_tier_limits, has_feature
# from services.screenshot_service import screenshot_service
# from services.storage_service import storage_service

# logger = logging.getLogger("pixelperfect")

# router = APIRouter(prefix="/api/v1/screenshot", tags=["Screenshot"])


# # ============================================================================
# # PYDANTIC MODELS
# # ============================================================================

# class ScreenshotRequest(BaseModel):
#     """Complete screenshot request model with all advertised features."""
#     url: HttpUrl
#     width: int = Field(default=1920, ge=320, le=3840)
#     height: int = Field(default=1080, ge=240, le=2160)
#     full_page: bool = False
#     format: str = Field(default="png", pattern="^(png|jpeg|webp|pdf)$")
#     quality: Optional[int] = Field(default=None, ge=0, le=100)
#     delay: int = Field(default=0, ge=0, le=10)
#     dark_mode: bool = False
#     remove_elements: Optional[List[str]] = None
#     return_url: bool = True

#     # Phase 1 — Pro+ features
#     device: Optional[str] = Field(
#         default=None,
#         description="Device preset (Pro+). Overrides width/height. "
#                     "See GET /api/v1/screenshot/devices for the full list.",
#     )
#     custom_js: Optional[str] = Field(
#         default=None,
#         max_length=10000,
#         description="JavaScript to execute before capture (Pro+). "
#                     "Errors are non-fatal — screenshot still captures and "
#                     "js_warning is populated in the response.",
#     )
#     wait_for_selector: Optional[str] = Field(
#         default=None,
#         max_length=200,
#         description="CSS selector to wait for before capture (Pro+).",
#     )

#     # Phase 2 — Business+
#     target_element: Optional[str] = Field(
#         default=None,
#         max_length=200,
#         description="CSS selector to crop to (Business+). "
#                     "The full page is captured first, then Pillow crops to this element.",
#     )

#     # Phase 3 — Business+ webhook
#     webhook_url: Optional[str] = Field(
#         default=None,
#         description="POST completion payload to this URL (Business+).",
#     )
#     webhook_secret: Optional[str] = Field(
#         default=None,
#         max_length=200,
#         description="HMAC-SHA256 secret. When set, responses include "
#                     "X-PixelPerfect-Signature for verification.",
#     )


# class ScreenshotResponse(BaseModel):
#     """
#     Screenshot response model.

#     ✅ Phase 2 (May 2026): added element_selector field.
#        Without this field the Pydantic model silently dropped element_selector
#        from all API responses even though the service returned it correctly,
#        causing every TC-EL-* test to fail with element_selector=''.
#     """
#     url: str
#     screenshot_url: Optional[str] = None
#     screenshot_id: str
#     width: int
#     height: int
#     format: str
#     size_bytes: int
#     created_at: str
#     usage: dict
#     device_used: Optional[str] = None
#     js_warning: Optional[str] = None       # Phase 1: non-None when custom_js threw
#     element_selector: Optional[str] = None # Phase 2: selector used for crop, or None


# class DeviceListResponse(BaseModel):
#     devices: List[str]
#     descriptions: Dict[str, str]


# # ============================================================================
# # HELPERS
# # ============================================================================

# def check_user_screenshot_limit(user: User) -> tuple[bool, int, int]:
#     """Return (allowed, current_count, limit)."""
#     tier_limits = get_tier_limits(user.subscription_tier or "free")
#     current = user.usage_screenshots or 0
#     limit = tier_limits["screenshots"]
#     return current < limit, current, limit


# def increment_user_usage(user: User, db, usage_type: str = "screenshots"):
#     """Increment usage counter and commit."""
#     if usage_type == "screenshots":
#         user.usage_screenshots = (user.usage_screenshots or 0) + 1
#     elif usage_type == "batch_requests":
#         user.usage_batch_requests = (user.usage_batch_requests or 0) + 1
#     user.usage_api_calls = (user.usage_api_calls or 0) + 1
#     db.commit()


# async def send_webhook_notification(
#     webhook_url: str,
#     screenshot_data: Dict[str, Any],
#     secret: Optional[str] = None,
#     max_retries: int = 3,
# ) -> None:
#     """
#     POST screenshot completion payload to caller's webhook URL.

#     Security:
#       - HMAC-SHA256 body signature → X-PixelPerfect-Signature: sha256=<hex>
#       - UTC timestamp → X-PixelPerfect-Timestamp (replay-attack mitigation)
#         Signature input: f"{timestamp}.".encode() + body_bytes

#     Reliability:
#       - Exponential backoff: 2s → 4s → 8s between attempts
#       - Permanent failure is logged but never raises (background task)
#     """
#     if not webhook_url:
#         return

#     payload = {
#         "event": "screenshot.completed",
#         "data": screenshot_data,
#         "timestamp": datetime.utcnow().isoformat(),
#     }
#     body = json.dumps(payload, sort_keys=True).encode()
#     ts = str(int(datetime.now(timezone.utc).timestamp()))

#     headers: Dict[str, str] = {
#         "Content-Type": "application/json",
#         "User-Agent": "PixelPerfect-Webhook/1.0",
#         "X-PixelPerfect-Timestamp": ts,
#     }
#     if secret:
#         sig_input = f"{ts}.".encode() + body
#         sig = hmac.new(secret.encode(), sig_input, hashlib.sha256).hexdigest()
#         headers["X-PixelPerfect-Signature"] = f"sha256={sig}"

#     for attempt in range(1, max_retries + 1):
#         try:
#             async with httpx.AsyncClient() as client:
#                 resp = await client.post(
#                     webhook_url, content=body, headers=headers, timeout=10.0
#                 )
#             if resp.is_success:
#                 logger.info(
#                     "✅ Webhook delivered on attempt %d: %s", attempt, webhook_url
#                 )
#                 return
#             logger.warning(
#                 "⚠️ Webhook attempt %d/%d → HTTP %d: %s",
#                 attempt, max_retries, resp.status_code, webhook_url,
#             )
#         except Exception as exc:
#             logger.warning(
#                 "⚠️ Webhook attempt %d/%d failed: %s — %s",
#                 attempt, max_retries, webhook_url, exc,
#             )

#         if attempt < max_retries:
#             await asyncio.sleep(2 ** attempt)  # 2s, 4s, 8s

#     logger.error(
#         "❌ Webhook permanently failed after %d attempts: %s", max_retries, webhook_url
#     )


# # ============================================================================
# # ENDPOINTS
# # ============================================================================

# @router.post("/", response_model=ScreenshotResponse)
# async def create_screenshot(
#     request: ScreenshotRequest,
#     background_tasks: BackgroundTasks,
#     current_user: User = Depends(get_current_user),
#     db=Depends(get_db),
# ):
#     """
#     Capture a screenshot with all advertised features.

#     **Tier gates**
#     | Feature          | Minimum tier |
#     |---|---|
#     | `custom_js`      | Pro |
#     | `device`         | Pro |
#     | `target_element` | Business |
#     | `webhook_url`    | Business |
#     | PDF format       | Business |

#     **JavaScript errors** are non-fatal (option-c). If `custom_js` throws,
#     the screenshot still captures and `js_warning` contains the error.

#     **Element selection** crops the screenshot to the element's bounding box.
#     Returns HTTP 400 if the selector matches nothing or the element has zero size.

#     **Webhook delivery** uses exponential-backoff retry (3 attempts) and
#     optional HMAC-SHA256 signing via `webhook_secret`.
#     """

#     # ── Usage limit ───────────────────────────────────────────────────────────
#     can_use, current, limit = check_user_screenshot_limit(current_user)
#     if not can_use:
#         raise HTTPException(
#             status_code=429,
#             detail=f"Screenshot limit reached ({current}/{limit}). Please upgrade your plan.",
#         )

#     # ── Tier gates (all via has_feature — single source of truth) ─────────────
#     tier_limits = get_tier_limits(current_user.subscription_tier or "free")

#     if request.format == "pdf" and not has_feature(current_user, "pdf"):
#         raise HTTPException(
#             status_code=403,
#             detail="PDF generation requires Business tier. Please upgrade.",
#         )

#     if request.custom_js and not has_feature(current_user, "custom_js"):
#         raise HTTPException(
#             status_code=403,
#             detail="Custom JavaScript execution requires Pro tier or higher. Please upgrade.",
#         )

#     if request.device and not has_feature(current_user, "device_emulation"):
#         raise HTTPException(
#             status_code=403,
#             detail="Device emulation requires Pro tier or higher. Please upgrade.",
#         )

#     if request.target_element and not has_feature(current_user, "element_selection"):
#         raise HTTPException(
#             status_code=403,
#             detail="Element selection requires Business tier. Please upgrade.",
#         )

#     if request.webhook_url and not has_feature(current_user, "webhooks"):
#         raise HTTPException(
#             status_code=403,
#             detail="Webhook notifications require Business tier. Please upgrade.",
#         )

#     if request.width > tier_limits.get("max_width", 1920):
#         raise HTTPException(
#             status_code=400,
#             detail=f"Width exceeds tier limit ({tier_limits.get('max_width', 1920)}px). Please upgrade.",
#         )

#     # ── Capture ───────────────────────────────────────────────────────────────
#     try:
#         if not screenshot_service.is_ready():
#             logger.info("🔧 Initializing Playwright browser…")
#             await screenshot_service.initialize()

#         start_time = datetime.utcnow()

#         # Service returns Dict[str, Any] with keys:
#         #   filename, filepath, url, width, height, format, full_page,
#         #   dark_mode, file_size, created_at,
#         #   js_warning        ← Phase 1
#         #   element_selector  ← Phase 2 (None when target_element not used)
#         result = await screenshot_service.capture_screenshot(
#             url=str(request.url),
#             width=request.width,
#             height=request.height,
#             full_page=request.full_page,
#             format=request.format,
#             delay=request.delay,
#             dark_mode=request.dark_mode,
#             remove_elements=request.remove_elements,
#             # Phase 1 params
#             device=request.device,
#             custom_js=request.custom_js,
#             wait_for_selector=request.wait_for_selector,
#             # Phase 2 param
#             target_element=request.target_element,
#         )

#         # Extract Phase 1 + Phase 2 fields from result dict
#         js_warning: Optional[str]       = result.get("js_warning")
#         element_selector: Optional[str] = result.get("element_selector")  # ← Phase 2

#         # Read captured bytes from disk
#         from pathlib import Path
#         screenshot_bytes = Path(result["filepath"]).read_bytes()

#         processing_time = (datetime.utcnow() - start_time).total_seconds() * 1000

#         # ── Storage ───────────────────────────────────────────────────────────
#         screenshot_id = str(uuid.uuid4())
#         filename = f"screenshots/{current_user.id}/{screenshot_id}.{request.format}"
#         content_type = (
#             "application/pdf" if request.format == "pdf" else f"image/{request.format}"
#         )

#         try:
#             screenshot_url = await storage_service.upload_screenshot(
#                 file_data=screenshot_bytes,
#                 filename=filename,
#                 content_type=content_type,
#             )
#             storage_key = filename
#         except Exception as upload_err:
#             logger.warning("R2 upload failed, falling back to local storage: %s", upload_err)
#             local_dir = Path("screenshots") / str(current_user.id)
#             local_dir.mkdir(parents=True, exist_ok=True)
#             local_path = local_dir / f"{screenshot_id}.{request.format}"
#             local_path.write_bytes(screenshot_bytes)
#             screenshot_url = f"/screenshots/{current_user.id}/{screenshot_id}.{request.format}"
#             storage_key = str(local_path)

#         # ── Database ──────────────────────────────────────────────────────────
#         retention_days = tier_limits.get("screenshot_retention_days", 7)
#         expires_at = datetime.utcnow() + timedelta(days=retention_days)

#         screenshot_record = Screenshot(
#             id=screenshot_id,
#             user_id=current_user.id,
#             url=str(request.url),
#             width=request.width if not request.device else None,
#             height=request.height if not request.device else None,
#             full_page=request.full_page,
#             format=request.format,
#             quality=request.quality,
#             delay_seconds=request.delay,
#             dark_mode=request.dark_mode,
#             size_bytes=len(screenshot_bytes),
#             storage_url=screenshot_url,
#             storage_key=storage_key,
#             processing_time_ms=processing_time,
#             status="completed",
#             expires_at=expires_at,
#             created_at=datetime.utcnow(),
#         )
#         db.add(screenshot_record)
#         increment_user_usage(current_user, db, "screenshots")
#         db.refresh(screenshot_record)
#         db.refresh(current_user)

#         logger.info(
#             "✅ Screenshot created: %s for user %s (element=%s)",
#             screenshot_id, current_user.id,
#             repr(element_selector) if element_selector else "none",
#         )

#         # ── Webhook (background, Business+) ───────────────────────────────────
#         if request.webhook_url:
#             background_tasks.add_task(
#                 send_webhook_notification,
#                 webhook_url=request.webhook_url,
#                 screenshot_data={
#                     "screenshot_id":   screenshot_id,
#                     "url":             str(request.url),
#                     "screenshot_url":  screenshot_url,
#                     "format":          request.format,
#                     "size_bytes":      len(screenshot_bytes),
#                     "processing_time_ms": processing_time,
#                     "js_warning":      js_warning,
#                     "element_selector": element_selector,  # ← Phase 2 in webhook payload
#                 },
#                 secret=request.webhook_secret,
#             )

#         return ScreenshotResponse(
#             url=str(request.url),
#             screenshot_url=screenshot_url if request.return_url else None,
#             screenshot_id=screenshot_id,
#             width=request.width,
#             height=request.height,
#             format=request.format,
#             size_bytes=len(screenshot_bytes),
#             created_at=screenshot_record.created_at.isoformat(),
#             device_used=request.device,
#             js_warning=js_warning,
#             element_selector=element_selector,   # ← Phase 2: the key that was missing
#             usage={
#                 "current":   current_user.usage_screenshots,
#                 "limit":     limit,
#                 "remaining": limit - current_user.usage_screenshots,
#             },
#         )

#     except httpx.HTTPError as exc:
#         logger.error("HTTP error loading URL %s: %s", request.url, exc)
#         raise HTTPException(status_code=400, detail=f"Failed to load URL: {exc}")
#     except ValueError as exc:
#         logger.error("Capture validation error: %s", exc)
#         raise HTTPException(status_code=400, detail=str(exc))
#     except Exception as exc:
#         logger.error("Screenshot failed: %s", exc, exc_info=True)
#         db.rollback()
#         raise HTTPException(status_code=500, detail=f"Screenshot failed: {exc}")


# @router.get("/devices", response_model=DeviceListResponse)
# async def list_devices(current_user: User = Depends(get_current_user)):
#     """List available device presets. Requires Pro tier or higher."""
#     if not has_feature(current_user, "device_emulation"):
#         raise HTTPException(
#             status_code=403,
#             detail="Device emulation requires Pro tier or higher. Please upgrade.",
#         )
#     devices = screenshot_service.get_available_devices()
#     descriptions = {
#         "iphone_13":         "iPhone 13 (390×844, 3× DPR, Safari UA)",
#         "iphone_13_pro_max": "iPhone 13 Pro Max (428×926, 3× DPR, Safari UA)",
#         "iphone_se":         "iPhone SE (375×667, 2× DPR, Safari UA)",
#         "pixel_5":           "Google Pixel 5 (393×851, 2.75× DPR, Chrome UA)",
#         "pixel_7":           "Google Pixel 7 (412×915, 2.625× DPR, Chrome UA)",
#         "ipad_pro":          "iPad Pro 11\" (1024×1366, 2× DPR, Safari UA)",
#         "ipad_mini":         "iPad Mini (768×1024, 2× DPR, Safari UA)",
#         "galaxy_s9":         "Samsung Galaxy S9+ (320×658, 4.5× DPR, Chrome UA)",
#         "galaxy_tab_s4":     "Samsung Galaxy Tab S4 (712×1138, 2.25× DPR, Chrome UA)",
#     }
#     return DeviceListResponse(devices=devices, descriptions=descriptions)


# @router.get("/stats/usage")
# async def get_usage_stats(
#     current_user: User = Depends(get_current_user),
#     db=Depends(get_db),
# ):
#     """Detailed usage statistics including feature access flags."""
#     tier_limits = get_tier_limits(current_user.subscription_tier or "free")
#     screenshots_used  = current_user.usage_screenshots or 0
#     screenshots_limit = tier_limits["screenshots"]
#     pct = (
#         round((screenshots_used / screenshots_limit) * 100, 1)
#         if screenshots_limit and screenshots_limit != "unlimited"
#         else 0
#     )

#     return {
#         "tier": current_user.subscription_tier or "free",
#         "usage": {
#             "screenshots": {
#                 "used":      screenshots_used,
#                 "limit":     screenshots_limit,
#                 "remaining": max(0, screenshots_limit - screenshots_used)
#                 if screenshots_limit != "unlimited" else "unlimited",
#                 "percentage": pct,
#             },
#             "batch_requests": {
#                 "used":  current_user.usage_batch_requests or 0,
#                 "limit": tier_limits["batch_requests"],
#                 "remaining": max(
#                     0,
#                     (tier_limits["batch_requests"] or 0) - (current_user.usage_batch_requests or 0),
#                 ) if tier_limits["batch_requests"] != "unlimited" else "unlimited",
#             },
#             "api_calls": {"used": current_user.usage_api_calls or 0},
#         },
#         "limits": tier_limits,
#         "reset_date": (
#             current_user.usage_reset_at.isoformat()
#             if current_user.usage_reset_at else None
#         ),
#         "features": {
#             "custom_js":         has_feature(current_user, "custom_js"),
#             "device_emulation":  has_feature(current_user, "device_emulation"),
#             "element_selection": has_feature(current_user, "element_selection"),
#             "pdf":               has_feature(current_user, "pdf"),
#             "webhooks":          has_feature(current_user, "webhooks"),
#             "white_label":       has_feature(current_user, "white_label"),
#         },
#     }


# @router.get("/{screenshot_id}")
# async def get_screenshot(
#     screenshot_id: str,
#     current_user: User = Depends(get_current_user),
#     db=Depends(get_db),
# ):
#     """Retrieve screenshot metadata by ID."""
#     screenshot = (
#         db.query(Screenshot)
#         .filter(Screenshot.id == screenshot_id, Screenshot.user_id == current_user.id)
#         .first()
#     )
#     if not screenshot:
#         raise HTTPException(status_code=404, detail="Screenshot not found")

#     return {
#         "id":                screenshot.id,
#         "url":               screenshot.url,
#         "screenshot_url":    screenshot.storage_url,
#         "width":             screenshot.width,
#         "height":            screenshot.height,
#         "format":            screenshot.format,
#         "size_bytes":        screenshot.size_bytes,
#         "status":            screenshot.status,
#         "processing_time_ms": screenshot.processing_time_ms,
#         "created_at":        screenshot.created_at.isoformat(),
#         "expires_at":        screenshot.expires_at.isoformat() if screenshot.expires_at else None,
#     }


# @router.get("/")
# async def list_screenshots(
#     limit: int = 20,
#     offset: int = 0,
#     current_user: User = Depends(get_current_user),
#     db=Depends(get_db),
# ):
#     """List screenshots for the authenticated user."""
#     q = db.query(Screenshot).filter(Screenshot.user_id == current_user.id)
#     total = q.count()
#     screenshots = q.order_by(Screenshot.created_at.desc()).limit(limit).offset(offset).all()

#     return {
#         "screenshots": [
#             {
#                 "id":             s.id,
#                 "url":            s.url,
#                 "screenshot_url": s.storage_url,
#                 "width":          s.width,
#                 "height":         s.height,
#                 "format":         s.format,
#                 "size_bytes":     s.size_bytes,
#                 "status":         s.status,
#                 "created_at":     s.created_at.isoformat(),
#             }
#             for s in screenshots
#         ],
#         "total":  total,
#         "limit":  limit,
#         "offset": offset,
#     }


# @router.delete("/{screenshot_id}")
# async def delete_screenshot(
#     screenshot_id: str,
#     current_user: User = Depends(get_current_user),
#     db=Depends(get_db),
# ):
#     """Delete a screenshot and its storage file."""
#     screenshot = (
#         db.query(Screenshot)
#         .filter(Screenshot.id == screenshot_id, Screenshot.user_id == current_user.id)
#         .first()
#     )
#     if not screenshot:
#         raise HTTPException(status_code=404, detail="Screenshot not found")

#     try:
#         if screenshot.storage_key:
#             await storage_service.delete_screenshot(screenshot.storage_key)
#     except Exception as exc:
#         logger.warning("Failed to delete from storage %s: %s", screenshot_id, exc)

#     db.delete(screenshot)
#     db.commit()
#     logger.info("🗑️ Screenshot deleted: %s", screenshot_id)
#     return {"status": "deleted", "screenshot_id": screenshot_id}

# # ===== END OF routers/screenshot.py ==========================================

