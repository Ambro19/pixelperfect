# ============================================================================
# SCREENSHOT ENDPOINTS — PixelPerfect Screenshot API
# File: backend/screenshot_endpoints.py
# Author: OneTechly
# Updated: July 2026
# ============================================================================
# PRODUCTION READY
#
# ✅ FIX (July 2026 — PDF Tier Gate: Business-only → Pro+):
#   Root cause: Three files disagreed on which tier PDF requires:
#     - models.py TIER_FEATURES["pro"]["pdf"] = False  ← wrong
#     - screenshot_endpoints.py error said "Business tier" ← wrong
#     - ScreenshotPage.js had no client-side gate ← wrong
#     - Features.jsx Feature Availability table showed PDF as free ← wrong
#   Fix applied here: has_feature(user, "pdf") now returns True for Pro
#   because models.py TIER_FEATURES["pro"]["pdf"] is now True.
#   Error message updated: "Pro tier or higher" (not "Business tier").
#   PDF is now available on: Pro, Business, Premium.
#   PDF is blocked on: Free.
#
# Previous fixes (retained):
# ✅ FIX (May 2026 — Phase 2): element_selector field in ScreenshotResponse
# ✅ FIX (May 2026 — Phase 1): Device emulation + custom_js + wait_for_selector
# ✅ FIX (Apr 2026): Per-user asyncio Semaphore concurrency limiter
# ✅ FIX (Apr 2026): 5-layer tier resolution chain prevents None tier errors
# ✅ FIX (Mar 2026): Batch job URLs stored in DB for crash recovery
# ============================================================================

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session

from auth_deps import get_current_user, get_optional_user
from database import get_db
from models import (
    BatchJob, Screenshot, User,
    get_tier_limits, has_feature, reset_monthly_usage
)
from screenshot_service import ScreenshotService
from storage_service import StorageService

logger = logging.getLogger(__name__)

# ── Router ────────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/v1", tags=["screenshots"])

# ── Thread pool (one thread per Playwright call) ──────────────────────────────
_executor = ThreadPoolExecutor(max_workers=1)

# ── Per-user concurrency semaphores ───────────────────────────────────────────
_user_semaphores: Dict[int, asyncio.Semaphore] = {}
_semaphore_lock  = asyncio.Lock()

CONCURRENCY_ACQUIRE_TIMEOUT = int(os.getenv("CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS", "5"))

TIER_CONCURRENCY: Dict[str, int] = {
    "free":     2,
    "starter":  2,
    "pro":      3,
    "business": 5,
    "premium":  5,
}

# ── Batch URL limits (must match models.py get_tier_limits) ──────────────────
TIER_BATCH_LIMITS: Dict[str, int] = {
    "free":     0,
    "starter":  0,
    "pro":      50,
    "business": 200,
    "premium":  1000,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_tier(user: User) -> str:
    """5-layer tier resolution — prevents None or missing tier errors."""
    raw = (
        getattr(user, "subscription_tier", None) or
        getattr(user, "tier",              None) or
        getattr(user, "plan",              None) or
        getattr(user, "account_type",      None) or
        "free"
    )
    return (str(raw) or "free").lower().strip()


async def _get_semaphore(user_id: int, max_concurrent: int) -> asyncio.Semaphore:
    async with _semaphore_lock:
        sem = _user_semaphores.get(user_id)
        if sem is None or sem._value != max_concurrent:
            sem = asyncio.Semaphore(max_concurrent)
            _user_semaphores[user_id] = sem
        return sem


def _build_storage_url(key: str) -> str:
    storage_type = os.getenv("STORAGE_TYPE", "local").lower()
    if storage_type == "r2":
        base = os.getenv("BACKEND_URL", "").rstrip("/")
        return f"{base}/screenshots/{key}"
    base = os.getenv("BACKEND_URL", "").rstrip("/") or os.getenv("CUSTOM_API_DOMAIN", "").rstrip("/")
    return f"{base}/screenshots/{key}"


def _increment_usage(user: User, field: str, db: Session, amount: int = 1) -> None:
    current = getattr(user, field, 0) or 0
    setattr(user, field, current + amount)
    try:
        db.commit()
    except Exception as e:
        logger.error(f"Usage increment failed ({field}): {e}")
        db.rollback()


# ── Request / Response models ─────────────────────────────────────────────────

class ScreenshotRequest(BaseModel):
    url:    str = Field(..., description="Target website URL (must start with http:// or https://)")
    width:  int = Field(1920, ge=320, le=3840)
    height: int = Field(1080, ge=240, le=2160)
    format: str = Field("png",   description="png | jpeg | webp | pdf")
    quality: Optional[int] = Field(None, ge=1, le=100)

    full_page: bool = Field(False)
    dark_mode: bool = Field(False)
    delay:     int  = Field(0, ge=0, le=10)

    remove_elements: Optional[List[str]] = Field(None)

    # Phase 1 — Pro+
    device:            Optional[str] = Field(None)
    custom_js:         Optional[str] = Field(None, max_length=10000)
    wait_for_selector: Optional[str] = Field(None)

    # Phase 2 — Business+
    target_element: Optional[str] = Field(None)

    # Phase 3 — Business+
    webhook_url: Optional[str] = Field(None)

    @validator("url")
    def validate_url(cls, v):
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v.strip()

    @validator("format")
    def validate_format(cls, v):
        allowed = {"png", "jpeg", "jpg", "webp", "pdf"}
        v = v.lower().strip()
        if v not in allowed:
            raise ValueError(f"Format must be one of: {', '.join(sorted(allowed))}")
        return v


class ScreenshotResponse(BaseModel):
    screenshot_url:  str
    screenshot_id:   str
    format:          str
    width:           int
    height:          int
    size_bytes:      int
    processing_time: float
    created_at:      str
    storage_type:    str
    js_warning:      Optional[str] = None
    element_selector: Optional[str] = None


class BatchSubmitRequest(BaseModel):
    urls:     Optional[List[str]] = Field(None)
    csv_text: Optional[str]       = Field(None)
    format:   str                 = Field("png")
    width:    int                 = Field(1920)
    height:   int                 = Field(1080)
    full_page: bool               = Field(False)
    quality:  Optional[int]       = Field(None)

    @validator("format")
    def validate_format(cls, v):
        allowed = {"png", "jpeg", "jpg", "webp", "pdf"}
        v = v.lower().strip()
        if v not in allowed:
            raise ValueError(f"Format must be one of: {', '.join(sorted(allowed))}")
        return v


class BatchJobStatusResponse(BaseModel):
    id:          str
    status:      str
    format:      str
    total:       int
    completed:   int
    failed:      int
    queued:      int
    processing:  int
    created_at:  str
    completed_at: Optional[str]
    items:       List[Dict[str, Any]]


# ── Single screenshot endpoint ────────────────────────────────────────────────

@router.post("/screenshot/", response_model=ScreenshotResponse)
async def capture_screenshot(
    request_data: ScreenshotRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Capture a single website screenshot.
    Authentication: JWT Bearer token or X-API-Key header.
    """
    start_time = time.time()
    tier = _resolve_tier(current_user)
    limits = get_tier_limits(tier)

    # ── Usage limit check ──────────────────────────────────────────────────────
    screenshots_used  = getattr(current_user, "usage_screenshots",    0) or 0
    screenshots_limit = limits.get("screenshots", 100)
    if (
        screenshots_limit != "unlimited"
        and isinstance(screenshots_limit, int)
        and screenshots_used >= screenshots_limit
    ):
        raise HTTPException(
            status_code=429,
            detail=f"Monthly screenshot limit reached ({screenshots_limit}). Please upgrade your plan.",
        )

    # ── PDF tier gate ─────────────────────────────────────────────────────────
    # ✅ FIX (July 2026): Changed from Business-only to Pro+.
    # has_feature(user, "pdf") returns True for Pro, Business, Premium.
    # Returns False for Free tier.
    if request_data.format.lower() == "pdf" and not has_feature(current_user, "pdf"):
        raise HTTPException(
            status_code=403,
            detail="PDF generation requires Pro tier or higher. Please upgrade.",
        )

    # ── Phase 1 feature gates (Pro+) ──────────────────────────────────────────
    if request_data.device and not has_feature(current_user, "device_emulation"):
        raise HTTPException(status_code=403, detail="Device emulation requires Pro tier or higher.")

    if request_data.custom_js and not has_feature(current_user, "custom_js"):
        raise HTTPException(status_code=403, detail="Custom JavaScript requires Pro tier or higher.")

    # ── Phase 2 feature gate (Business+) ──────────────────────────────────────
    if request_data.target_element and not has_feature(current_user, "element_selection"):
        raise HTTPException(status_code=403, detail="Element selection requires Business tier or higher.")

    # ── Phase 3 feature gate (Business+) ──────────────────────────────────────
    if request_data.webhook_url and not has_feature(current_user, "webhooks"):
        raise HTTPException(status_code=403, detail="Webhooks require Business tier or higher.")

    # ── Per-user concurrency limiter ───────────────────────────────────────────
    max_concurrent = TIER_CONCURRENCY.get(tier, 2)
    semaphore = await _get_semaphore(current_user.id, max_concurrent)
    acquired  = False

    try:
        acquired = await asyncio.wait_for(
            semaphore.acquire(),
            timeout=CONCURRENCY_ACQUIRE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=429,
            detail="Too many concurrent requests. Please wait and retry.",
            headers={"Retry-After": "1"},
        )

    try:
        # ── Playwright capture ────────────────────────────────────────────────
        loop    = asyncio.get_event_loop()
        service = ScreenshotService()

        capture_kwargs: Dict[str, Any] = {
            "url":             request_data.url,
            "width":           request_data.width,
            "height":          request_data.height,
            "format":          request_data.format,
            "full_page":       request_data.full_page,
            "dark_mode":       request_data.dark_mode,
            "delay":           request_data.delay,
            "remove_elements": request_data.remove_elements or [],
        }
        if request_data.quality is not None:
            capture_kwargs["quality"] = request_data.quality
        if request_data.device:
            capture_kwargs["device"] = request_data.device
        if request_data.custom_js:
            capture_kwargs["custom_js"] = request_data.custom_js
        if request_data.wait_for_selector:
            capture_kwargs["wait_for_selector"] = request_data.wait_for_selector
        if request_data.target_element:
            capture_kwargs["target_element"] = request_data.target_element

        result = await loop.run_in_executor(
            _executor,
            lambda: service.take_screenshot(**capture_kwargs),
        )

        if not result.get("success"):
            raise HTTPException(
                status_code=500,
                detail=result.get("error", "Screenshot capture failed."),
            )

        # ── Storage ───────────────────────────────────────────────────────────
        image_data   = result["image_data"]
        storage_type = os.getenv("STORAGE_TYPE", "local").lower()
        ext          = request_data.format if request_data.format != "jpg" else "jpeg"
        filename     = f"screenshot_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.{ext}"

        storage_svc = StorageService()
        if storage_type == "r2":
            r2_key = f"screenshots/{filename}"
            storage_svc.upload_to_r2(image_data, r2_key, f"image/{ext}")
            storage_url = _build_storage_url(r2_key)
        else:
            os.makedirs("screenshots", exist_ok=True)
            path = f"screenshots/{filename}"
            with open(path, "wb") as fp:
                fp.write(image_data)
            storage_url = _build_storage_url(filename)

        # ── DB record ─────────────────────────────────────────────────────────
        screenshot_id  = uuid.uuid4().hex
        processing_time = time.time() - start_time

        screenshot = Screenshot(
            id              = screenshot_id,
            user_id         = current_user.id,
            url             = request_data.url,
            width           = result.get("width",  request_data.width),
            height          = result.get("height", request_data.height),
            format          = ext,
            full_page       = request_data.full_page,
            dark_mode       = request_data.dark_mode,
            size_bytes      = len(image_data),
            storage_url     = storage_url,
            status          = "completed",
            processing_time_ms = processing_time * 1000,
            created_at      = datetime.utcnow(),
        )
        db.add(screenshot)

        _increment_usage(current_user, "usage_screenshots", db)
        _increment_usage(current_user, "usage_api_calls",   db)

        db.refresh(screenshot)

        processing_time = time.time() - start_time

        return ScreenshotResponse(
            screenshot_url   = storage_url,
            screenshot_id    = screenshot_id,
            format           = ext,
            width            = result.get("width",  request_data.width),
            height           = result.get("height", request_data.height),
            size_bytes       = len(image_data),
            processing_time  = processing_time,
            created_at       = datetime.utcnow().isoformat(),
            storage_type     = storage_type,
            js_warning       = result.get("js_warning"),
            element_selector = result.get("element_selector"),
        )

    finally:
        if acquired:
            semaphore.release()


# ── Batch submit (JSON) ────────────────────────────────────────────────────────

@router.post("/batch/submit")
async def batch_submit(
    request_data: BatchSubmitRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tier        = _resolve_tier(current_user)
    batch_limit = TIER_BATCH_LIMITS.get(tier, 0)

    if batch_limit == 0:
        raise HTTPException(
            status_code=403,
            detail="Batch processing requires Pro tier or higher. Please upgrade.",
        )

    # ── PDF gate for batch ────────────────────────────────────────────────────
    # ✅ FIX (July 2026): PDF is Pro+, not Business-only.
    if request_data.format.lower() == "pdf" and not has_feature(current_user, "pdf"):
        raise HTTPException(
            status_code=403,
            detail="PDF generation requires Pro tier or higher. Please upgrade.",
        )

    # ── Parse URLs ─────────────────────────────────────────────────────────────
    urls: List[str] = []
    if request_data.urls:
        urls = request_data.urls
    elif request_data.csv_text:
        sep  = "\t" if "\t" in request_data.csv_text else ","
        for line in request_data.csv_text.splitlines():
            for cell in line.split(sep):
                cell = cell.strip().strip('"').strip("'")
                if cell.startswith(("http://", "https://")):
                    urls.append(cell)

    urls = list(dict.fromkeys(urls))  # deduplicate preserving order

    if not urls:
        raise HTTPException(status_code=400, detail="No valid URLs provided.")

    if len(urls) > batch_limit:
        raise HTTPException(
            status_code=400,
            detail=f"URL count ({len(urls)}) exceeds your plan limit ({batch_limit}). Reduce the list or upgrade.",
        )

    # ── Create job ─────────────────────────────────────────────────────────────
    job_id = uuid.uuid4().hex[:16]
    job    = BatchJob(
        id         = job_id,
        user_id    = current_user.id,
        status     = "queued",
        format     = request_data.format,
        width      = request_data.width,
        height     = request_data.height,
        full_page  = request_data.full_page,
        total_urls = len(urls),
        urls_json  = json.dumps(urls),
        created_at = datetime.utcnow(),
    )
    db.add(job)
    db.commit()

    background_tasks.add_task(_process_batch_job, job_id, urls, request_data, current_user.id)

    return {
        "job_id":      job_id,
        "status":      "queued",
        "total_urls":  len(urls),
        "format":      request_data.format,
        "message":     f"Batch job queued. {len(urls)} URL(s) will be processed.",
    }


# ── Batch submit (file upload) ─────────────────────────────────────────────────

@router.post("/batch/submit_file")
async def batch_submit_file(
    background_tasks: BackgroundTasks,
    file:      UploadFile = File(...),
    format:    str        = Form("png"),
    width:     int        = Form(1920),
    height:    int        = Form(1080),
    full_page: bool       = Form(False),
    current_user: User    = Depends(get_current_user),
    db: Session           = Depends(get_db),
):
    tier        = _resolve_tier(current_user)
    batch_limit = TIER_BATCH_LIMITS.get(tier, 0)

    if batch_limit == 0:
        raise HTTPException(
            status_code=403,
            detail="Batch processing requires Pro tier or higher. Please upgrade.",
        )

    if format.lower() == "pdf" and not has_feature(current_user, "pdf"):
        raise HTTPException(
            status_code=403,
            detail="PDF generation requires Pro tier or higher. Please upgrade.",
        )

    content = (await file.read()).decode("utf-8", errors="replace")
    urls: List[str] = []
    sep  = "\t" if "\t" in content else ","
    for line in content.splitlines():
        for cell in line.split(sep):
            cell = cell.strip().strip('"').strip("'")
            if cell.startswith(("http://", "https://")):
                urls.append(cell)

    urls = list(dict.fromkeys(urls))

    if not urls:
        raise HTTPException(status_code=400, detail="No valid URLs found in file.")

    if len(urls) > batch_limit:
        raise HTTPException(
            status_code=400,
            detail=f"URL count ({len(urls)}) exceeds your plan limit ({batch_limit}).",
        )

    job_id = uuid.uuid4().hex[:16]
    req    = BatchSubmitRequest(
        format=format, width=width, height=height, full_page=full_page
    )
    job = BatchJob(
        id         = job_id,
        user_id    = current_user.id,
        status     = "queued",
        format     = format,
        width      = width,
        height     = height,
        full_page  = full_page,
        total_urls = len(urls),
        urls_json  = json.dumps(urls),
        created_at = datetime.utcnow(),
    )
    db.add(job)
    db.commit()

    background_tasks.add_task(_process_batch_job, job_id, urls, req, current_user.id)

    return {
        "job_id":     job_id,
        "status":     "queued",
        "total_urls": len(urls),
        "message":    f"Batch job queued from file upload. {len(urls)} URL(s) to process.",
    }


# ── Batch background processor ────────────────────────────────────────────────

async def _process_batch_job(
    job_id:    str,
    urls:      List[str],
    req:       BatchSubmitRequest,
    user_id:   int,
):
    from database import SessionLocal
    db      = SessionLocal()
    service = ScreenshotService()

    try:
        job = db.query(BatchJob).filter(BatchJob.id == job_id).first()
        if not job:
            return

        job.status = "processing"
        db.commit()

        completed = 0
        failed    = 0
        items: List[Dict[str, Any]] = []

        for idx, url in enumerate(urls):
            item_start = time.time()
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    _executor,
                    lambda u=url: service.take_screenshot(
                        url=u, width=req.width, height=req.height,
                        format=req.format, full_page=req.full_page,
                    ),
                )
                if not result.get("success"):
                    raise RuntimeError(result.get("error", "Unknown error"))

                image_data   = result["image_data"]
                ext          = req.format if req.format != "jpg" else "jpeg"
                filename     = f"batch_{job_id}_{idx}_{uuid.uuid4().hex[:6]}.{ext}"
                storage_type = os.getenv("STORAGE_TYPE", "local").lower()

                storage_svc = StorageService()
                if storage_type == "r2":
                    r2_key = f"screenshots/{filename}"
                    storage_svc.upload_to_r2(image_data, r2_key, f"image/{ext}")
                    screenshot_url = _build_storage_url(r2_key)
                else:
                    os.makedirs("screenshots", exist_ok=True)
                    path = f"screenshots/{filename}"
                    with open(path, "wb") as fp:
                        fp.write(image_data)
                    screenshot_url = _build_storage_url(filename)

                processing_time = time.time() - item_start
                items.append({
                    "idx":             idx,
                    "url":             url,
                    "status":          "completed",
                    "screenshot_url":  screenshot_url,
                    "file_size":       len(image_data),
                    "processing_time": round(processing_time, 2),
                })
                completed += 1

            except Exception as e:
                logger.error(f"Batch item {idx} failed ({url}): {e}")
                items.append({
                    "idx":    idx,
                    "url":    url,
                    "status": "failed",
                    "error":  str(e),
                })
                failed += 1

        if failed == 0:
            final_status = "completed"
        elif completed == 0:
            final_status = "failed"
        else:
            final_status = "partial"

        job.status          = final_status
        job.completed_count = completed
        job.failed_count    = failed
        job.completed_at    = datetime.utcnow()
        db.commit()

        # Store item results in snapshot JSON
        snap_path = ".batch_jobs_snapshot.json"
        try:
            existing: Dict = {}
            if os.path.exists(snap_path):
                with open(snap_path) as f:
                    existing = json.load(f)
            existing[job_id] = {
                "status":    final_status,
                "items":     items,
                "completed": completed,
                "failed":    failed,
            }
            with open(snap_path, "w") as f:
                json.dump(existing, f)
        except Exception as e:
            logger.warning(f"Failed to write snapshot: {e}")

    except Exception as e:
        logger.error(f"Batch job {job_id} crashed: {e}")
        try:
            job = db.query(BatchJob).filter(BatchJob.id == job_id).first()
            if job:
                job.status = "failed"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


# ── Batch job status ──────────────────────────────────────────────────────────

@router.get("/batch/jobs", response_model=List[Dict[str, Any]])
async def list_batch_jobs(
    current_user: User = Depends(get_current_user),
    db: Session        = Depends(get_db),
):
    jobs = (
        db.query(BatchJob)
        .filter(BatchJob.user_id == current_user.id)
        .order_by(BatchJob.created_at.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "id":          j.id,
            "status":      j.status,
            "format":      j.format,
            "total":       j.total_urls,
            "completed":   j.completed_count or 0,
            "failed":      j.failed_count    or 0,
            "created_at":  j.created_at.isoformat() if j.created_at else None,
            "completed_at": j.completed_at.isoformat() if j.completed_at else None,
        }
        for j in jobs
    ]


@router.get("/batch/jobs/{job_id}", response_model=Dict[str, Any])
async def get_batch_job(
    job_id:       str,
    current_user: User    = Depends(get_current_user),
    db: Session           = Depends(get_db),
):
    job = (
        db.query(BatchJob)
        .filter(BatchJob.id == job_id, BatchJob.user_id == current_user.id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Batch job not found.")

    queued     = max(0, (job.total_urls or 0) - (job.completed_count or 0) - (job.failed_count or 0))
    processing = 1 if job.status == "processing" else 0

    # Load items from snapshot
    items: List[Dict] = []
    try:
        snap_path = ".batch_jobs_snapshot.json"
        if os.path.exists(snap_path):
            with open(snap_path) as f:
                snap = json.load(f)
            snap_job = snap.get(job_id, {})
            items    = snap_job.get("items", [])
    except Exception as e:
        logger.warning(f"Failed to load snapshot for job {job_id}: {e}")

    return {
        "id":           job.id,
        "status":       job.status,
        "format":       job.format,
        "total":        job.total_urls or 0,
        "completed":    job.completed_count or 0,
        "failed":       job.failed_count    or 0,
        "queued":       queued,
        "processing":   processing,
        "created_at":   job.created_at.isoformat()   if job.created_at   else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "items":        items,
    }


@router.post("/batch/jobs/{job_id}/retry_failed")
async def retry_failed_batch_items(
    job_id:          str,
    background_tasks: BackgroundTasks,
    current_user:    User    = Depends(get_current_user),
    db: Session              = Depends(get_db),
):
    job = (
        db.query(BatchJob)
        .filter(BatchJob.id == job_id, BatchJob.user_id == current_user.id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Batch job not found.")
    if job.status == "processing":
        raise HTTPException(status_code=409, detail="Job is still processing.")

    # Load failed URLs from snapshot
    failed_urls: List[str] = []
    try:
        snap_path = ".batch_jobs_snapshot.json"
        if os.path.exists(snap_path):
            with open(snap_path) as f:
                snap = json.load(f)
            items       = snap.get(job_id, {}).get("items", [])
            failed_urls = [i["url"] for i in items if i.get("status") == "failed"]
    except Exception as e:
        logger.warning(f"Could not read snapshot for retry: {e}")

    if not failed_urls:
        return {"message": "No failed items to retry.", "retried": 0}

    job.status          = "queued"
    job.completed_count = (job.completed_count or 0)
    job.failed_count    = 0
    db.commit()

    req = BatchSubmitRequest(
        format=job.format, width=job.width, height=job.height, full_page=job.full_page
    )
    background_tasks.add_task(_process_batch_job, job_id, failed_urls, req, current_user.id)

    return {"message": f"Retrying {len(failed_urls)} failed item(s).", "retried": len(failed_urls)}


@router.delete("/batch/jobs/{job_id}")
async def delete_batch_job(
    job_id:       str,
    current_user: User    = Depends(get_current_user),
    db: Session           = Depends(get_db),
):
    job = (
        db.query(BatchJob)
        .filter(BatchJob.id == job_id, BatchJob.user_id == current_user.id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Batch job not found.")
    db.delete(job)
    db.commit()

    # Remove from snapshot
    try:
        snap_path = ".batch_jobs_snapshot.json"
        if os.path.exists(snap_path):
            with open(snap_path) as f:
                snap = json.load(f)
            snap.pop(job_id, None)
            with open(snap_path, "w") as f:
                json.dump(snap, f)
    except Exception:
        pass

    return {"message": "Batch job deleted.", "job_id": job_id}

# ===== END OF screenshot_endpoints.py =====


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

