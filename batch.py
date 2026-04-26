# backend/routers/batch.py — PixelPerfect Screenshot API
# UPDATED: April 2026
#
# ✅ All previous fixes retained (R2 upload, DB records, tier limits, etc.)
#
# ✅ FIX (Apr 2026 — Persistent batch job storage)
# ✅ FIX (Apr 2026 — AttributeError: 'BatchJob' has no attribute 'urls_json')
# ✅ FIX (Apr 2026 — User-friendly error messages for invalid URLs)
#
# ✅ NEW (Apr 2026): dark_mode / delay / remove_elements now supported in batch.
#
#   Background:
#     The single-screenshot endpoint (POST /api/v1/screenshot) gained these
#     three capture options in April 2026. The batch endpoints did not — so
#     a user calling the single endpoint could hide cookie banners and use
#     dark mode, but the same user submitting 50 URLs via batch could not.
#     That inconsistency made the product feel half-finished.
#
#   What's new:
#     - BatchSubmitRequest gains 3 optional fields:
#         dark_mode        (bool, default False)
#         delay            (int 0–10, default None/0)
#         remove_elements  (List[str], ≤20 items, each ≤200 chars)
#     - submit_batch_file accepts the same fields as Form parameters.
#       remove_elements is passed as a comma-separated string in multipart
#       forms (matches how the frontend textbox already works).
#     - _validate_remove_elements() shared helper silently drops bad entries.
#     - Values are stored on the in-memory job dict so retry_failed reuses them.
#     - _process_item() passes all three through to screenshot_service.
#     - Backward compatible: omitting any field = current behavior (no-op).
#
#   Design note on retry_failed:
#     Previously retry called _process_job_async(..., None) for quality.
#     Now it reads the stored job values so a retried batch uses the exact
#     same capture options as the original submission. Without this, a user
#     who hid banners on submit would see the banners reappear on retry.

from __future__ import annotations

import asyncio
import csv
import json
import logging
import re as _re
import time
import uuid
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from auth_deps import get_current_user
from models import BatchJob, Screenshot, SessionLocal, User, get_db
from screenshot_service import screenshot_service, get_screenshot_url
from services.storage_service import storage_service

log = logging.getLogger("batch_screenshots")

router = APIRouter(prefix="/batch", tags=["batch"])

SCREENSHOTS_DIR = Path(__file__).resolve().parents[1] / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

TIER_BATCH_LIMITS = {
    "free":     0,
    "pro":      50,
    "business": 200,
    "premium":  1000,
}

# In-memory job store — fast path for active/recent jobs.
# Jobs not found here are reconstructed from DB (handles restarts).
JOBS: Dict[str, Dict[str, Any]] = {}

VALID_FORMATS = {"png", "jpeg", "jpg", "webp", "pdf"}

_CONTENT_TYPES: Dict[str, str] = {
    "png":  "image/png",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "pdf":  "application/pdf",
}

# ── Hard limits for remove_elements (must match screenshot_service.py) ────────
_MAX_REMOVE_ELEMENTS_COUNT   = 20
_MAX_REMOVE_ELEMENT_SELECTOR = 200


# ── ✅ User-friendly error translation ─────────────────────────────────────────

def _friendly_error(msg: str) -> str:
    """
    Translate raw Playwright / network error codes into plain English.
    Same semantics as ScreenshotPage.js friendlyError() on the frontend.
    """
    if not msg:
        return "Screenshot capture failed. Please try again."

    m = msg.lower()

    if any(k in m for k in (
        "err_name_not_resolved",
        "name not resolved",
        "getaddrinfo",
        "nodename nor servname",
    )):
        return (
            "The website address could not be found. "
            "Please check that the URL is spelled correctly and the domain "
            "exists (e.g. https://example.com — not https://exampel.com)."
        )

    if any(k in m for k in ("err_connection_refused", "connection refused")):
        return (
            "The website refused the connection. "
            "The server may be down or blocking automated requests."
        )

    if any(k in m for k in (
        "err_connection_timed_out",
        "err_timed_out",
        "timed out after all retry",
    )):
        return (
            "The website took too long to respond and timed out. "
            "It may be slow or temporarily unavailable. "
            "Try again later or use a simpler URL."
        )

    if any(k in m for k in ("err_cert", "ssl", "certificate")):
        return (
            "The website has an SSL certificate problem "
            "(expired or self-signed certificate). "
            "The site may not be publicly accessible."
        )

    if any(k in m for k in ("err_access_denied", "access denied", "forbidden")):
        return (
            "Access to this website was denied. "
            "The site may be blocking automated access."
        )

    if "page.goto" in m:
        code_match = _re.search(r"net::(ERR_[A-Z_]+)", msg)
        if code_match:
            return (
                f"Failed to load the website ({code_match.group(1)}). "
                "Please check the URL is correct and the site is publicly accessible."
            )
        return (
            "Failed to load the website. "
            "Please check the URL is correct and the site is publicly accessible."
        )

    return msg


# ── ✅ NEW helper: validate remove_elements consistently with single endpoint ──

def _validate_remove_elements(value: Optional[List[str]]) -> Optional[List[str]]:
    """
    Clean remove_elements list — silently drop bad entries rather than
    rejecting the whole request. Matches screenshot_endpoints.py behavior.

    - None or non-list → None
    - Non-string entries → dropped
    - Empty strings → dropped
    - Each selector capped at _MAX_REMOVE_ELEMENT_SELECTOR chars
    - Array capped at _MAX_REMOVE_ELEMENTS_COUNT entries
    """
    if value is None or not isinstance(value, list):
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


def _parse_remove_elements_form(raw: Optional[str]) -> Optional[List[str]]:
    """
    Parse remove_elements from a multipart form field.

    Multipart forms don't natively support arrays. The convention here
    matches how the frontend's text input already works:
      ".cookie-banner, #popup, .ads"  →  [".cookie-banner", "#popup", ".ads"]

    Also accepts a JSON array string for programmatic clients:
      '[".cookie-banner", "#popup"]'  →  [".cookie-banner", "#popup"]
    """
    if not raw:
        return None

    stripped = raw.strip()
    if not stripped:
        return None

    # Try JSON array first (for programmatic clients passing structured data)
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return _validate_remove_elements(parsed)
        except Exception:
            pass  # Fall through to comma-split

    # Comma-separated (matches frontend textbox behavior)
    selectors = [s.strip() for s in stripped.split(",") if s.strip()]
    return _validate_remove_elements(selectors)


# ── Pydantic models ────────────────────────────────────────────────────────────

class BatchSubmitRequest(BaseModel):
    urls:     Optional[List[str]] = Field(default=None)
    csv_text: Optional[str]       = Field(default=None)
    format:   str                 = Field(default="png")
    width:    int                 = Field(default=1920, ge=320, le=7680)
    height:   int                 = Field(default=1080, ge=240, le=4320)
    full_page: bool               = Field(default=False)
    quality:  Optional[int]       = Field(default=None, ge=1, le=100)

    # ✅ NEW (Apr 2026): match single-screenshot endpoint capabilities
    dark_mode: bool = Field(
        default=False,
        description="Render with dark color scheme (prefers-color-scheme: dark)",
    )
    delay: Optional[int] = Field(
        default=None,
        ge=0,
        le=10,
        description="Seconds to wait after page load before each capture (0–10).",
    )
    remove_elements: Optional[List[str]] = Field(
        default=None,
        description=(
            "CSS selectors for elements to hide before capture. Applied to every URL "
            "in the batch. Max 20 selectors, each ≤200 chars."
        ),
    )

    @field_validator("format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        value = (v or "").strip().lower()
        if value not in VALID_FORMATS:
            raise ValueError(f"format must be one of {sorted(VALID_FORMATS)}")
        return value

    @field_validator("remove_elements")
    @classmethod
    def _clean_remove_elements(cls, v):
        return _validate_remove_elements(v)

    def collect_urls(self) -> List[str]:
        raw_urls: List[str] = []
        if self.urls:
            raw_urls.extend(self.urls)
        if self.csv_text:
            text = self.csv_text.strip()
            if "," in text:
                reader = csv.reader(StringIO(text))
                for row in reader:
                    raw_urls.extend([cell.strip() for cell in row if cell.strip()])
            elif "\t" in text:
                reader = csv.reader(StringIO(text), delimiter="\t")
                for row in reader:
                    raw_urls.extend([cell.strip() for cell in row if cell.strip()])
            else:
                raw_urls.extend([line.strip() for line in text.splitlines() if line.strip()])
        seen = set()
        urls: List[str] = []
        for url in raw_urls:
            u = (url or "").strip()
            if u and (u.startswith("http://") or u.startswith("https://")) and u not in seen:
                seen.add(u)
                urls.append(u)
        return urls


class BatchItemOut(BaseModel):
    idx:             int
    url:             str
    status:          str
    message:         Optional[str]   = None
    screenshot_url:  Optional[str]   = None
    file_size:       Optional[int]   = None
    processing_time: Optional[float] = None


class BatchJobOut(BaseModel):
    id:         str
    created_at: str
    status:     str
    format:     str
    total:      int
    completed:  int
    failed:     int
    queued:     int
    processing: int
    items:      List[BatchItemOut]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_user_tier(user: User) -> str:
    return (getattr(user, "subscription_tier", "free") or "free").lower()


def _get_batch_limit(tier: str) -> int:
    return TIER_BATCH_LIMITS.get((tier or "").lower(), 0)


def _create_initial_item(idx: int, url: str) -> Dict[str, Any]:
    return {
        "idx":             idx,
        "url":             url,
        "status":          "queued",
        "message":         "Waiting to process...",
        "screenshot_url":  None,
        "file_size":       None,
        "processing_time": None,
        "created_at":      datetime.utcnow().isoformat(),
    }


def _calc_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    completed  = sum(1 for it in items if it["status"] == "completed")
    failed     = sum(1 for it in items if it["status"] in ("failed", "cancelled"))
    queued     = sum(1 for it in items if it["status"] == "queued")
    processing = sum(1 for it in items if it["status"] == "processing")
    return {
        "completed":  completed,
        "failed":     failed,
        "queued":     queued,
        "processing": processing,
        "total":      len(items),
    }


def _update_job_counts(job: Dict[str, Any]) -> None:
    job.update(_calc_counts(job["items"]))


# ── DB-based job reconstruction after restart ──────────────────────────────────

def _reconstruct_job_from_db(db_job: BatchJob, db: Session) -> Dict[str, Any]:
    urls: List[str] = []
    urls_json_raw = getattr(db_job, "urls_json", None)
    if urls_json_raw:
        try:
            urls = json.loads(urls_json_raw)
        except Exception:
            log.warning("⚠️ Failed to parse urls_json for job %s", db_job.id)

    completed_screenshots: List[Screenshot] = []
    try:
        completed_screenshots = (
            db.query(Screenshot)
            .filter(Screenshot.batch_job_id == db_job.id)
            .all()
        )
    except Exception as e:
        log.warning(
            "⚠️ Could not query screenshots by batch_job_id for job %s: %s",
            db_job.id, e,
        )

    url_to_screenshot: Dict[str, Screenshot] = {s.url: s for s in completed_screenshots}
    is_terminal = db_job.status in ("completed", "partial", "failed", "cancelled")
    items: List[Dict[str, Any]] = []

    for i, url in enumerate(urls):
        if url in url_to_screenshot:
            s = url_to_screenshot[url]
            items.append({
                "idx":             i,
                "url":             url,
                "status":          "completed",
                "message":         "Screenshot captured successfully",
                "screenshot_url":  s.storage_url,
                "file_size":       s.size_bytes,
                "processing_time": round(s.processing_time_ms / 1000, 2) if s.processing_time_ms else None,
                "created_at":      s.created_at.isoformat() if s.created_at else db_job.created_at.isoformat(),
                "completed_at":    s.created_at.isoformat() if s.created_at else None,
            })
        else:
            status  = "failed"  if is_terminal else "queued"
            message = (
                "Lost to server restart — retry to recapture"
                if is_terminal else "Waiting to process..."
            )
            items.append({
                "idx":             i,
                "url":             url,
                "status":          status,
                "message":         message,
                "screenshot_url":  None,
                "file_size":       None,
                "processing_time": None,
                "created_at":      db_job.created_at.isoformat(),
            })

    counts = _calc_counts(items)
    job: Dict[str, Any] = {
        "id":         db_job.id,
        "user_id":    db_job.user_id,
        "created_at": db_job.created_at.isoformat(),
        "status":     db_job.status,
        "format":     db_job.format,
        "width":      db_job.width,
        "height":     db_job.height,
        "full_page":  db_job.full_page,
        # ✅ NEW: reconstructed jobs default these to safe values.
        # Retry_failed will use whatever is stored here.
        "dark_mode":       False,
        "delay":           None,
        "remove_elements": None,
        **counts,
        "items":      items,
    }
    JOBS[db_job.id] = job
    log.info(
        "♻️  Reconstructed job %s from DB: %d/%d completed, status=%s",
        db_job.id, counts["completed"], counts["total"], db_job.status,
    )
    return job


def _own_job_or_404(
    job_id: str,
    user_id: int,
    db: Optional[Session] = None,
) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if job and job["user_id"] == user_id:
        return job
    if db is not None:
        db_job = db.query(BatchJob).filter(
            BatchJob.id == job_id,
            BatchJob.user_id == user_id,
        ).first()
        if db_job:
            return _reconstruct_job_from_db(db_job, db)
    raise HTTPException(status_code=404, detail="Job not found")


def _job_to_out(job: Dict[str, Any]) -> BatchJobOut:
    # Strip internal fields that aren't part of the public BatchJobOut schema.
    # We also strip the new options (dark_mode/delay/remove_elements) here
    # because they're stored for internal retry_failed re-use but aren't
    # exposed on the job response model. They will be added to the response
    # once the frontend starts displaying them per-job.
    _INTERNAL_KEYS = {
        "user_id", "_from_db",
        "width", "height", "full_page",
        "dark_mode", "delay", "remove_elements",
    }
    return BatchJobOut(**{k: v for k, v in job.items() if k not in _INTERNAL_KEYS})


def _enforce_tier_limits(urls: List[str], user: User) -> None:
    tier  = _get_user_tier(user)
    limit = _get_batch_limit(tier)
    if limit == 0:
        raise HTTPException(
            status_code=403,
            detail="Batch processing is not available on the free tier. "
                   "Please upgrade to Pro or higher.",
        )
    if len(urls) > limit:
        raise HTTPException(
            status_code=403,
            detail=f"Batch size ({len(urls)}) exceeds your tier limit ({limit}). "
                   "Please upgrade your plan or reduce the number of URLs.",
        )


def _build_and_store_job(
    job_id:          str,
    user_id:         int,
    urls:            List[str],
    fmt:             str,
    width:           int,
    height:          int,
    full_page:       bool,
    db:              Session,
    *,
    dark_mode:       bool               = False,                # ✅ NEW
    delay:           Optional[int]      = None,                 # ✅ NEW
    remove_elements: Optional[List[str]] = None,                # ✅ NEW
) -> Dict[str, Any]:
    now    = datetime.utcnow().isoformat()
    items  = [_create_initial_item(i, url) for i, url in enumerate(urls)]
    counts = _calc_counts(items)

    job: Dict[str, Any] = {
        "id":              job_id,
        "user_id":         user_id,
        "created_at":      now,
        "status":          "queued",
        "format":          fmt,
        "width":           width,
        "height":          height,
        "full_page":       full_page,
        # ✅ NEW: stored on the job dict so retry_failed reuses the same options.
        "dark_mode":       bool(dark_mode),
        "delay":           delay,
        "remove_elements": remove_elements,
        **counts,
        "items":           items,
    }
    JOBS[job_id] = job

    try:
        db_job = BatchJob(
            id=job_id,
            user_id=user_id,
            status="queued",
            format=fmt,
            width=width,
            height=height,
            full_page=full_page,
            total_urls=len(urls),
            completed_count=0,
            failed_count=0,
            created_at=datetime.utcnow(),
        )
        try:
            db_job.urls_json = json.dumps(urls)
        except AttributeError:
            log.warning(
                "⚠️ BatchJob.urls_json not declared on model — "
                "add urls_json = Column(Text, nullable=True) to BatchJob in models.py."
            )

        db.add(db_job)
        db.commit()
        log.info("💾 Saved BatchJob record: id=%s user=%s urls=%d", job_id, user_id, len(urls))
    except Exception as db_err:
        db.rollback()
        log.warning("⚠️ Failed to save BatchJob record (non-fatal): %s", db_err)

    return job


# ── Screenshot capture + R2 upload ────────────────────────────────────────────

async def _process_item(
    item:            Dict[str, Any],
    job_id:          str,
    fmt:             str,
    width:           int,
    height:          int,
    full_page:       bool,
    quality:         Optional[int],
    user:            User,
    db:              Session,
    *,
    dark_mode:       bool                  = False,             # ✅ NEW
    delay:           Optional[int]         = None,              # ✅ NEW
    remove_elements: Optional[List[str]]   = None,              # ✅ NEW
) -> Dict[str, Any]:
    url     = item["url"]
    started = time.time()

    try:
        item["status"]  = "processing"
        item["message"] = "Capturing screenshot..."

        result = await screenshot_service.capture_screenshot(
            url=url,
            width=width,
            height=height,
            format=fmt,
            full_page=full_page,
            dark_mode=dark_mode,                 # ✅ NEW
            delay=delay,                         # ✅ NEW
            remove_elements=remove_elements,     # ✅ NEW
        )

        if not result:
            raise Exception("Screenshot capture failed — no result returned")

        filename:        str           = result["filename"]
        screenshot_path                = result.get("filepath")
        file_size: Optional[int]       = result.get("file_size")

        if not file_size and screenshot_path:
            path_obj = Path(screenshot_path)
            if path_obj.exists():
                file_size = path_obj.stat().st_size

        screenshot_url: str
        if storage_service.use_r2 and screenshot_path:
            try:
                file_bytes   = Path(screenshot_path).read_bytes()
                content_type = _CONTENT_TYPES.get(fmt.lower(), "application/octet-stream")
                screenshot_url = await storage_service.upload_screenshot(
                    file_data=file_bytes,
                    filename=filename,
                    content_type=content_type,
                )
                log.info("☁️  Batch item %s → R2: %s", item["idx"], screenshot_url)
            except Exception as r2_err:
                log.warning(
                    "⚠️ R2 upload failed for item %s, using local URL: %s",
                    item["idx"], r2_err,
                )
                screenshot_url = get_screenshot_url(filename)
        else:
            screenshot_url = get_screenshot_url(filename)
            log.info("💾 Batch item %s → local: %s", item["idx"], screenshot_url)

        processing_time = round(time.time() - started, 2)

        item["status"]          = "completed"
        item["message"]         = "Screenshot captured successfully"
        item["screenshot_url"]  = screenshot_url
        item["file_size"]       = file_size
        item["processing_time"] = processing_time
        item["completed_at"]    = datetime.utcnow().isoformat()

        try:
            db_record = Screenshot(
                user_id=user.id,
                url=url,
                screenshot_path=str(screenshot_path or ""),
                storage_url=screenshot_url,
                format=fmt,
                width=width,
                height=height,
                full_page=full_page,
                size_bytes=file_size,
                processing_time_ms=processing_time * 1000,
                status="completed",
                created_at=datetime.utcnow(),
            )
            try:
                db_record.batch_job_id = job_id
            except AttributeError:
                log.warning(
                    "⚠️ Screenshot.batch_job_id not declared on model — "
                    "add batch_job_id = Column(String(32), nullable=True) "
                    "to Screenshot in models.py."
                )

            db.add(db_record)
            db.commit()
            log.info("💾 Saved batch screenshot record: id=%s url=%s", db_record.id, screenshot_url)
        except Exception as save_err:
            db.rollback()
            log.warning("⚠️ Failed to save batch screenshot record: %s", save_err)

        log.info("✅ Batch item %s completed: %s → %s", item["idx"], url, screenshot_url)

    except Exception as exc:
        processing_time         = round(time.time() - started, 2)
        item["status"]          = "failed"
        item["message"]         = _friendly_error(str(exc))
        item["screenshot_url"]  = None
        item["processing_time"] = processing_time
        item["failed_at"]       = datetime.utcnow().isoformat()
        log.error("❌ Batch item %s failed: %s — %s", item["idx"], url, exc)

    return item


# ── Background job processor ───────────────────────────────────────────────────

async def _process_job_async(
    job_id:          str,
    user_id:         int,
    fmt:             str,
    width:           int,
    height:          int,
    full_page:       bool,
    quality:         Optional[int],
    *,
    dark_mode:       bool                  = False,             # ✅ NEW
    delay:           Optional[int]         = None,              # ✅ NEW
    remove_elements: Optional[List[str]]   = None,              # ✅ NEW
) -> None:
    job = JOBS.get(job_id)
    if not job:
        log.warning("Job %s not found for processing", job_id)
        return

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            log.error("User %s not found for job %s", user_id, job_id)
            return

        job["status"] = "processing"
        log.info(
            "🔵 Starting batch job %s with %s URLs (dark=%s delay=%s remove=%d)",
            job_id, job["total"],
            dark_mode,
            delay or 0,
            len(remove_elements or []),
        )

        for item in job["items"]:
            if job.get("status") == "cancelled":
                log.info("🚫 Job %s was cancelled — stopping", job_id)
                break
            if item["status"] == "queued":
                await _process_item(
                    item, job_id, fmt, width, height,
                    full_page, quality, user, db,
                    dark_mode=dark_mode,
                    delay=delay,
                    remove_elements=remove_elements,
                )
                _update_job_counts(job)
                await asyncio.sleep(0.2)

        counts = _calc_counts(job["items"])
        job.update(counts)

        if job.get("status") != "cancelled":
            if counts["failed"] == 0:
                job["status"] = "completed"
            elif counts["completed"] > 0:
                job["status"] = "partial"
            else:
                job["status"] = "failed"

        job["completed_at"] = datetime.utcnow().isoformat()
        log.info(
            "✅ Batch job %s finished: %s/%s successful",
            job_id, counts["completed"], counts["total"],
        )

        try:
            db_job = db.query(BatchJob).filter(BatchJob.id == job_id).first()
            if db_job:
                db_job.status          = job["status"]
                db_job.completed_count = counts["completed"]
                db_job.failed_count    = counts["failed"]
                db_job.completed_at    = datetime.utcnow()
                db.commit()
                log.info("💾 Updated BatchJob record: id=%s status=%s", job_id, job["status"])
        except Exception as db_err:
            db.rollback()
            log.warning("⚠️ Failed to update BatchJob record: %s", db_err)

    finally:
        db.close()


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post("/submit", response_model=BatchJobOut)
async def submit_batch(
    request:      BatchSubmitRequest = Body(...),
    current_user: User               = Depends(get_current_user),
    db:           Session            = Depends(get_db),
    bg:           BackgroundTasks    = None,
):
    urls = request.collect_urls()
    if not urls:
        raise HTTPException(status_code=400, detail="No valid URLs found in request")
    _enforce_tier_limits(urls, current_user)
    job_id = uuid.uuid4().hex[:16]
    job    = _build_and_store_job(
        job_id, current_user.id, urls,
        request.format, request.width, request.height, request.full_page, db,
        dark_mode=request.dark_mode,              # ✅ NEW
        delay=request.delay,                      # ✅ NEW
        remove_elements=request.remove_elements,  # ✅ NEW
    )
    log.info(
        "📸 Created batch job %s with %s URLs for user %s (dark=%s delay=%s remove=%d)",
        job_id, len(urls), current_user.username,
        request.dark_mode,
        request.delay or 0,
        len(request.remove_elements or []),
    )
    bg.add_task(
        _process_job_async,
        job_id, current_user.id,
        request.format, request.width, request.height,
        request.full_page, request.quality,
        dark_mode=request.dark_mode,
        delay=request.delay,
        remove_elements=request.remove_elements,
    )
    return _job_to_out(job)


@router.post("/submit_file", response_model=BatchJobOut)
async def submit_batch_file(
    file:            UploadFile       = File(...),
    format:          str              = Form(default="png"),
    width:           int              = Form(default=1920),
    height:          int              = Form(default=1080),
    full_page:       bool             = Form(default=False),
    quality:         Optional[int]    = Form(default=None),
    # ✅ NEW (Apr 2026): match single-screenshot endpoint capabilities
    dark_mode:       bool             = Form(default=False),
    delay:           Optional[int]    = Form(default=None),
    remove_elements: Optional[str]    = Form(
        default=None,
        description=(
            "Comma-separated CSS selectors (e.g. '.cookie-banner, #popup') "
            "or a JSON array string."
        ),
    ),
    current_user:    User             = Depends(get_current_user),
    db:              Session          = Depends(get_db),
    bg:              BackgroundTasks  = None,
):
    fname = (file.filename or "").lower()
    if not (fname.endswith(".csv") or fname.endswith(".txt") or fname.endswith(".tsv")):
        raise HTTPException(
            status_code=400,
            detail="Invalid file format. Please upload .csv, .txt, or .tsv",
        )
    try:
        text = (await file.read()).decode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read file: {exc}")

    # ✅ Parse remove_elements from form string → list
    parsed_remove = _parse_remove_elements_form(remove_elements)

    # Validate delay range manually since Form() doesn't support ge/le
    if delay is not None:
        if delay < 0 or delay > 10:
            raise HTTPException(
                status_code=422,
                detail="delay must be between 0 and 10 seconds.",
            )

    req  = BatchSubmitRequest(
        csv_text=text, format=format, width=width,
        height=height, full_page=full_page, quality=quality,
        dark_mode=dark_mode,
        delay=delay,
        remove_elements=parsed_remove,
    )
    urls = req.collect_urls()
    if not urls:
        raise HTTPException(
            status_code=400, detail="No valid URLs found in uploaded file",
        )
    _enforce_tier_limits(urls, current_user)
    job_id = uuid.uuid4().hex[:16]
    job    = _build_and_store_job(
        job_id, current_user.id, urls,
        format, width, height, full_page, db,
        dark_mode=dark_mode,
        delay=delay,
        remove_elements=parsed_remove,
    )
    log.info(
        "📸 Created batch job %s from file with %s URLs for user %s (dark=%s delay=%s remove=%d)",
        job_id, len(urls), current_user.username,
        dark_mode, delay or 0, len(parsed_remove or []),
    )
    bg.add_task(
        _process_job_async,
        job_id, current_user.id,
        format, width, height, full_page, quality,
        dark_mode=dark_mode,
        delay=delay,
        remove_elements=parsed_remove,
    )
    return _job_to_out(job)


@router.get("/jobs", response_model=List[BatchJobOut])
async def list_jobs(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    memory_job_ids = {
        jid for jid, j in JOBS.items() if j["user_id"] == current_user.id
    }
    db_jobs = (
        db.query(BatchJob)
        .filter(BatchJob.user_id == current_user.id)
        .order_by(BatchJob.created_at.desc())
        .all()
    )
    for db_job in db_jobs:
        if db_job.id not in memory_job_ids:
            try:
                _reconstruct_job_from_db(db_job, db)
            except Exception as e:
                log.warning("⚠️ Failed to reconstruct job %s from DB: %s", db_job.id, e)

    user_jobs = [j for j in JOBS.values() if j["user_id"] == current_user.id]
    user_jobs.sort(key=lambda j: j["created_at"], reverse=True)
    return [_job_to_out(j) for j in user_jobs]


@router.get("/jobs/{job_id}", response_model=BatchJobOut)
async def get_job(
    job_id:       str,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    job = _own_job_or_404(job_id, current_user.id, db)
    return _job_to_out(job)


@router.post("/jobs/{job_id}/retry_failed", response_model=BatchJobOut)
async def retry_failed(
    job_id:       str,
    current_user: User            = Depends(get_current_user),
    db:           Session         = Depends(get_db),
    bg:           BackgroundTasks = None,
):
    job     = _own_job_or_404(job_id, current_user.id, db)
    changed = False
    for item in job["items"]:
        if item["status"] == "failed":
            item["status"]         = "queued"
            item["message"]        = "Retrying..."
            item["screenshot_url"] = None
            changed                = True
    if changed:
        job.update(_calc_counts(job["items"]))
        job["status"] = "queued"
        # ✅ FIX (Apr 2026): pass stored capture options so retried items
        # use the same settings as the original submission. Previously
        # dark_mode / delay / remove_elements would have been lost on retry.
        bg.add_task(
            _process_job_async,
            job_id, current_user.id,
            job["format"], job["width"], job["height"], job["full_page"], None,
            dark_mode=bool(job.get("dark_mode", False)),
            delay=job.get("delay"),
            remove_elements=job.get("remove_elements"),
        )
    return _job_to_out(job)


@router.post("/jobs/{job_id}/cancel", response_model=BatchJobOut)
async def cancel_job(
    job_id:       str,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    job = _own_job_or_404(job_id, current_user.id, db)
    if job["status"] not in ("queued", "processing"):
        raise HTTPException(
            status_code=400,
            detail=f"Job cannot be cancelled — current status is '{job['status']}'",
        )
    for item in job["items"]:
        if item["status"] in ("queued", "processing"):
            item["status"]  = "cancelled"
            item["message"] = "Cancelled by user"
    job.update(_calc_counts(job["items"]))
    job["status"]       = "cancelled"
    job["completed_at"] = datetime.utcnow().isoformat()

    try:
        db_job = db.query(BatchJob).filter(BatchJob.id == job_id).first()
        if db_job:
            db_job.status       = "cancelled"
            db_job.completed_at = datetime.utcnow()
            db.commit()
    except Exception as db_err:
        db.rollback()
        log.warning("⚠️ Failed to update cancel status in DB: %s", db_err)

    log.info("🚫 Batch job %s cancelled by user %s", job_id, current_user.id)
    return _job_to_out(job)


@router.delete("/jobs/{job_id}")
async def delete_job(
    job_id:       str,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    _own_job_or_404(job_id, current_user.id, db)
    JOBS.pop(job_id, None)
    try:
        db_job = db.query(BatchJob).filter(BatchJob.id == job_id).first()
        if db_job:
            db.delete(db_job)
            db.commit()
    except Exception as db_err:
        db.rollback()
        log.warning("⚠️ Failed to delete BatchJob from DB: %s", db_err)
    return {"ok": True, "deleted": job_id}

# ====== END OF batch.py ========

