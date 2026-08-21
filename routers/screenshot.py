# backend/routers/screenshot.py
# PixelPerfect Screenshot API Router — Phase 1 + Phase 2 Advanced Features
# Author: OneTechly
# Updated: August 2026
#
# ============================================================================
# ✅ FIX (Aug 2026 — Round 4): FOUR BUGS. Read this before editing.
# ============================================================================
#
# ── BUG 1 (CRITICAL): Premium tier returned HTTP 500 on every capture ───────
#   get_tier_limits("premium")["screenshots"] returns the STRING "unlimited"
#   (models.py _env_limit converts any value >= 999_999_999 to that sentinel).
#   check_user_screenshot_limit then evaluated:
#
#       current < limit          # int < "unlimited"
#
#   In Python 3 that raises TypeError, and the call sits OUTSIDE the try
#   block, so it surfaced as an unhandled 500. The same fault existed in the
#   response payload: `limit - current_user.usage_screenshots`.
#   Every Premium screenshot request failed. Fix: "unlimited" is now checked
#   explicitly before any arithmetic or comparison, everywhere.
#
# ── BUG 2: Usage enforcement disagreed with the dashboard ───────────────────
#   Enforcement read user.usage_screenshots (a lifetime counter), while
#   /subscription_status counted rows in the current calendar month. Two
#   different systems, two different answers.
#
#   The counter is reset by reset_monthly_usage(), which per its own docstring
#   fires on Stripe invoice.paid events. Free-tier users never generate an
#   invoice, so their counter NEVER reset — it accumulated from signup
#   forever. A Free user who took 100 screenshots across three months got
#   permanently locked out with "Screenshot limit reached (100/100)" while
#   their dashboard showed 0/100, because the calendar month had rolled over.
#   No error, no explanation, no way for them to self-diagnose.
#
#   Fix: enforcement now calls screenshots_used_this_period() from
#   usage_accounting.py — the same function /subscription_status uses. One
#   source of truth. Resets automatically at the period boundary with no
#   reset job required. user.usage_screenshots is still incremented for
#   backward compatibility but is no longer authoritative for any decision.
#
# ── BUG 3: DELETE here bypassed the usage tombstone ─────────────────────────
#   There are TWO delete endpoints:
#       DELETE /api/v1/screenshots/{id}   (main.py — patched Aug 2026)
#       DELETE /api/v1/screenshot/{id}    (this file — was NOT patched)
#   The second one hard-deleted the row with no tombstone, so deleting
#   through it still refunded quota. Fix: record_screenshot_deletion() is
#   called here too, before the delete, in the same transaction.
#
# ── BUG 4: max_width tier gate rejected every width above 1920 ──────────────
#   The check read tier_limits.get("max_width", 1920), but get_tier_limits()
#   in models.py returns only: screenshots, batch_requests, api_calls,
#   features. There is no max_width key, so .get() ALWAYS returned the 1920
#   default — for every tier including Premium.
#
#   ScreenshotRequest allows width up to 3840 and ScreenshotPage.js ships an
#   "Ultrawide (3440x1440)" preset, so that preset returned
#   "Width exceeds tier limit (1920px). Please upgrade." for everyone —
#   and upgrading did not help, because no tier defines max_width.
#
#   Fix: TIER_MAX_WIDTH is defined here explicitly, with Free capped at 1920
#   and paid tiers allowed the full 3840 the request model accepts. If you
#   would rather gate this differently, change TIER_MAX_WIDTH — it is now a
#   real, visible policy instead of an accidental default.
#
# ============================================================================
# Previous fixes (all retained)
# ============================================================================
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
from typing import Optional, List, Dict, Any, Tuple, Union

import httpx
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, HttpUrl, Field

from auth_deps import get_current_user
from models import User, get_db, Screenshot, get_tier_limits, has_feature

# ✅ NEW (Aug 2026): single source of truth for usage. Enforcement and the
# dashboard now read the same number, and deleting a screenshot no longer
# refunds quota. See usage_accounting.py.
from usage_accounting import (
    screenshots_used_this_period,
    batch_used_this_period,
    record_screenshot_deletion,
    current_period_start,
    first_of_next_month,
)

from services.screenshot_service import screenshot_service
from services.storage_service import storage_service

logger = logging.getLogger("pixelperfect")

router = APIRouter(prefix="/api/v1/screenshot", tags=["Screenshot"])


# ============================================================================
# TIER POLICY — explicit, not accidental
# ============================================================================
# ✅ NEW (Aug 2026 — BUG 4): max viewport width per tier.
#
# This used to be read as tier_limits.get("max_width", 1920), but
# get_tier_limits() never returns a max_width key, so the 1920 default
# applied to every tier and silently broke the Ultrawide preset for
# everyone. Making the policy explicit here means it can be changed
# deliberately and reviewed.
#
# 3840 is the ceiling ScreenshotRequest already validates against
# (width: le=3840), so paid tiers get the full documented range.
TIER_MAX_WIDTH: Dict[str, int] = {
    "free":     1920,
    "pro":      3840,
    "business": 3840,
    "premium":  3840,
}
DEFAULT_MAX_WIDTH = 1920


def _max_width_for(user: User) -> int:
    tier = (getattr(user, "subscription_tier", None) or "free").lower()
    return TIER_MAX_WIDTH.get(tier, DEFAULT_MAX_WIDTH)


def is_unlimited(limit: Any) -> bool:
    """
    True when a tier limit is the 'unlimited' sentinel.

    models.py _env_limit() returns the STRING "unlimited" for Premium.
    Every comparison and every subtraction involving a limit must go
    through this check first — see BUG 1 in the header. Comparing an int
    against "unlimited" raises TypeError, not a falsy result.
    """
    return limit == "unlimited" or limit is None or limit == float("inf")


def remaining_for(limit: Any, used: int) -> Union[int, str]:
    """Safe 'remaining' value that never subtracts from a string."""
    if is_unlimited(limit):
        return "unlimited"
    try:
        return max(0, int(limit) - int(used))
    except (TypeError, ValueError):
        return "unlimited"


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

def check_user_screenshot_limit(
    user: User,
    db,
) -> Tuple[bool, int, Any]:
    """
    Return (allowed, current_count, limit).

    ✅ REWRITTEN (Aug 2026 — BUGS 1 and 2).

    Was:
        current = user.usage_screenshots or 0
        return current < limit, current, limit

    Two faults in those two lines:

    1. `current < limit` raised TypeError for Premium, whose limit is the
       string "unlimited". Unhandled → HTTP 500 on every Premium capture.

    2. user.usage_screenshots is a LIFETIME counter reset only by
       reset_monthly_usage(), which fires on Stripe invoice.paid. Free users
       never generate an invoice, so their counter never reset and they were
       permanently locked out once they hit 100 — while the dashboard, which
       counts per calendar month, cheerfully showed 0/100.

    Now: usage comes from screenshots_used_this_period(), the same function
    /subscription_status uses. Enforcement and display can no longer diverge,
    and the period rolls over on its own with no reset job.

    NOTE the signature changed — this now needs `db`. Any other caller must
    be updated.
    """
    tier_limits = get_tier_limits(user.subscription_tier or "free")
    limit = tier_limits["screenshots"]

    current = screenshots_used_this_period(db, user.id)

    # ✅ Unlimited is checked BEFORE any comparison. Never compare int < str.
    if is_unlimited(limit):
        return True, current, "unlimited"

    try:
        return current < int(limit), current, int(limit)
    except (TypeError, ValueError):
        # A malformed .env value should not hard-fail a paying customer's
        # capture. Log it and allow through — the dashboard will still show
        # the real usage.
        logger.error(
            "Malformed screenshot limit %r for tier %s — allowing capture.",
            limit, user.subscription_tier,
        )
        return True, current, limit


def increment_user_usage(user: User, db, usage_type: str = "screenshots"):
    """
    Increment the legacy usage counters and commit.

    ⚠️ NO LONGER AUTHORITATIVE (Aug 2026). These columns are kept updated for
    backward compatibility with anything still reading them, but no limit
    decision is made from them any more — see check_user_screenshot_limit().
    The authoritative figure is screenshots_used_this_period(), derived from
    the screenshots table plus deletion tombstones.

    Do not reintroduce enforcement based on these columns. They do not reset
    for Free-tier users.
    """
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

# ✅ FIX (Aug 2026): NOT a route. main.py imports this function and calls it
# from @app.post("/api/v1/screenshot"), which carries enforce_tier_concurrency.
# Registering it here too created a second, unguarded POST at the
# trailing-slash path that skipped the per-user concurrency semaphore.

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
    | width > 1920px   | Pro |
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
    # ✅ FIX (Aug 2026): now passes db and uses period-scoped usage.
    # Premium ("unlimited") no longer raises TypeError here.
    can_use, current, limit = check_user_screenshot_limit(current_user, db)
    if not can_use:
        # %-d is not portable (fails on Windows); %d is fine and dev runs on
        # Windows while production runs Linux.
        resets_on = first_of_next_month().strftime("%B %d, %Y")
        raise HTTPException(
            status_code=429,
            detail=(
                f"Screenshot limit reached ({current}/{limit}) for this billing "
                f"period. Your usage resets on {resets_on}. "
                "Upgrade your plan for a higher limit."
            ),
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

    # ✅ FIX (Aug 2026 — BUG 4): width gate now reads a real per-tier policy.
    # Previously this was tier_limits.get("max_width", 1920), but
    # get_tier_limits() has no max_width key, so the 1920 default applied to
    # EVERY tier — including Premium — and the Ultrawide (3440x1440) preset
    # in ScreenshotPage.js failed for everyone with an "upgrade" message that
    # would not have helped. A device preset overrides width entirely, so the
    # gate is skipped in that case.
    if not request.device:
        max_width = _max_width_for(current_user)
        if request.width > max_width:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Width {request.width}px exceeds the {max_width}px limit for "
                    f"your plan. "
                    + (
                        "Upgrade to Pro or higher for widths up to 3840px."
                        if max_width < 3840
                        else "The maximum supported width is 3840px."
                    )
                ),
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

        # ✅ FIX (Aug 2026): width/height are NOT NULL in models.py
        # (Column(Integer, nullable=False, default=1920)). The previous code
        # wrote None whenever a device preset was used, which SQLAlchemy sends
        # as an explicit NULL — the column default does not apply to an
        # explicitly-set None — producing an IntegrityError on Postgres for
        # every device-emulation capture. We now persist the dimensions the
        # service actually produced, falling back to the requested values.
        actual_width  = int(result.get("width")  or request.width)
        actual_height = int(result.get("height") or request.height)

        screenshot_record = Screenshot(
            id=screenshot_id,
            user_id=current_user.id,
            url=str(request.url),
            width=actual_width,
            height=actual_height,
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

        # ✅ FIX (Aug 2026 — BUG 1): usage block is computed with the
        # unlimited-safe helpers. `limit - used` on a Premium account used to
        # raise TypeError here even when the capture itself had succeeded.
        used_now = current + 1
        return ScreenshotResponse(
            url=str(request.url),
            screenshot_url=screenshot_url if request.return_url else None,
            screenshot_id=screenshot_id,
            width=actual_width,
            height=actual_height,
            format=request.format,
            size_bytes=len(screenshot_bytes),
            created_at=screenshot_record.created_at.isoformat(),
            device_used=request.device,
            js_warning=js_warning,
            element_selector=element_selector,   # ← Phase 2: the key that was missing
            usage={
                "current":   used_now,
                "limit":     limit,
                "remaining": remaining_for(limit, used_now),
                "period_start": current_period_start().isoformat(),
                "resets_at":    first_of_next_month().isoformat(),
            },
        )

    except HTTPException:
        # Tier gates and validation errors must pass through untouched —
        # without this they would be swallowed by the generic handler below
        # and returned as a 500.
        raise
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
    """
    Detailed usage statistics including feature access flags.

    ✅ FIX (Aug 2026): reports the same period-scoped numbers as
    /subscription_status and as the enforcement path. Previously this read
    the lifetime user.usage_* counters, so this endpoint, the dashboard, and
    the limit check could all disagree with each other simultaneously.
    """
    tier_limits = get_tier_limits(current_user.subscription_tier or "free")

    screenshots_used  = screenshots_used_this_period(db, current_user.id)
    screenshots_limit = tier_limits["screenshots"]
    batch_used        = batch_used_this_period(db, current_user)
    batch_limit       = tier_limits["batch_requests"]

    # ✅ Unlimited-safe percentage — no division by a string.
    if is_unlimited(screenshots_limit):
        pct = 0
    else:
        try:
            pct = round((screenshots_used / int(screenshots_limit)) * 100, 1) if int(screenshots_limit) else 0
        except (TypeError, ValueError, ZeroDivisionError):
            pct = 0

    return {
        "tier": current_user.subscription_tier or "free",
        "usage": {
            "screenshots": {
                "used":       screenshots_used,
                "limit":      screenshots_limit,
                "remaining":  remaining_for(screenshots_limit, screenshots_used),
                "percentage": pct,
            },
            "batch_requests": {
                "used":      batch_used,
                "limit":     batch_limit,
                "remaining": remaining_for(batch_limit, batch_used),
            },
            "api_calls": {"used": screenshots_used + batch_used},
        },
        "limits": tier_limits,
        "max_width": _max_width_for(current_user),
        "period_start": current_period_start().isoformat(),
        # ✅ FIX (Aug 2026): reset_date now always present and always correct.
        # It used to echo user.usage_reset_at, which is null for every Free
        # user (that field is only written by the Stripe invoice.paid path),
        # so this endpoint returned null for the majority of accounts.
        "reset_date": first_of_next_month().isoformat(),
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
        "created_at":        screenshot.created_at.isoformat() if screenshot.created_at else None,
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
                "created_at":     s.created_at.isoformat() if s.created_at else None,
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
    """
    Delete a screenshot and its storage file.

    ⚠️ NOTE: this is the SECOND delete endpoint in the codebase.
        DELETE /api/v1/screenshots/{id}   → main.py    (used by History.js)
        DELETE /api/v1/screenshot/{id}    → this file  (API clients)

    ✅ FIX (Aug 2026 — BUG 3): main.py was patched to write a usage tombstone
    before deleting; this path was not, so deleting through the API still
    refunded the caller's quota. Both paths now record the tombstone.

    Deleting a screenshot removes the artifact. It does NOT refund usage —
    the capture cost was already spent when Chromium rendered the page.
    """
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

    # ✅ Must run BEFORE db.delete() — it reads created_at off the row.
    # Shares the caller's transaction, so the tombstone and the delete
    # commit together or not at all.
    record_screenshot_deletion(db, screenshot)

    db.delete(screenshot)
    db.commit()
    logger.info("🗑️ Screenshot deleted: %s (usage tombstone recorded)", screenshot_id)
    return {
        "status": "deleted",
        "screenshot_id": screenshot_id,
        "usage_refunded": False,
    }


# ===== END OF routers/screenshot.py ===============
