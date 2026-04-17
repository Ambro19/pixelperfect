# backend/routers/batch.py — PixelPerfect Screenshot API
# UPDATED: April 2026
#
# ✅ All previous fixes retained (R2 upload, DB records, tier limits, etc.)
#
# ✅ FIX (Apr 2026 — Persistent batch job storage):
#   Root cause / fix documented in full above the original version.
#
# ✅ FIX (Apr 2026 — AttributeError: 'BatchJob' has no attribute 'urls_json'):
#   Defensive getattr() / setattr() pattern — works before models.py updated.
#
# ✅ FIX (Apr 2026 — User-friendly error messages for invalid URLs):
#
#   Root cause:
#     When a user submits a syntactically-valid-but-unresolvable URL such as
#     "https://www.hollywoodre", Playwright returns a raw Chromium error string:
#       "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://www.hollywoodre/"
#     That raw string was stored directly as item["message"] in the job dict
#     and shown in the Batch Jobs UI. It is meaningless to a non-technical user.
#
#   Fix:
#     Added _friendly_error() — a translation function that maps the most
#     common Chromium/network error codes to plain-English messages.
#     Applied in _process_item()'s except block:
#       item["message"] = _friendly_error(str(exc))   ← was: str(exc)
#     The same translation is applied in ScreenshotPage.js on the frontend.

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
from pydantic import BaseModel, Field, validator
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


# ── ✅ FIX (Apr 2026): User-friendly error translation ─────────────────────────
# Playwright surfaces raw Chromium error codes (ERR_NAME_NOT_RESOLVED, etc.)
# that are meaningless to end users. This function maps the most common ones
# to plain-English messages that are actionable.
#
# Applied in _process_item()'s except block so all batch item failure messages
# are human-readable. The same mapping exists in ScreenshotPage.js (frontend).

def _friendly_error(msg: str) -> str:
    """
    Translate raw Playwright / network error codes into plain English.

    Called in _process_item()'s except block so batch item failure messages
    shown in the UI are actionable rather than showing raw Chromium errors.

    Examples of translated errors:
      "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://www.hollywoodre/"
        → "The website address could not be found. Please check that the URL
           is spelled correctly and the domain exists."
      "Page.goto: net::ERR_CONNECTION_REFUSED at https://localhost/"
        → "The website refused the connection. The server may be down..."
    """
    if not msg:
        return "Screenshot capture failed. Please try again."

    m = msg.lower()

    # ── Domain does not exist / DNS lookup failed ──────────────────────────────
    # Triggered when user submits a URL with a non-existent or misspelled domain,
    # e.g. "https://www.hollywoodre" (syntactically valid, DNS unresolvable).
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

    # ── Connection refused ─────────────────────────────────────────────────────
    if any(k in m for k in ("err_connection_refused", "connection refused")):
        return (
            "The website refused the connection. "
            "The server may be down or blocking automated requests."
        )

    # ── Timeout — caught by 3-tier fallback in screenshot_service.py ──────────
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

    # ── SSL / certificate problems ─────────────────────────────────────────────
    if any(k in m for k in ("err_cert", "ssl", "certificate")):
        return (
            "The website has an SSL certificate problem "
            "(expired or self-signed certificate). "
            "The site may not be publicly accessible."
        )

    # ── Access denied / blocked ────────────────────────────────────────────────
    if any(k in m for k in ("err_access_denied", "access denied", "forbidden")):
        return (
            "Access to this website was denied. "
            "The site may be blocking automated access."
        )

    # ── Generic Playwright navigation error — strip the technical prefix ───────
    # e.g. "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://..."
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

    # ── Unknown — return original so no diagnostic information is hidden ───────
    return msg


# ── Pydantic models ────────────────────────────────────────────────────────────

class BatchSubmitRequest(BaseModel):
    urls:     Optional[List[str]] = Field(default=None)
    csv_text: Optional[str]       = Field(default=None)
    format:   str                 = Field(default="png")
    width:    int                 = Field(default=1920, ge=320, le=7680)
    height:   int                 = Field(default=1080, ge=240, le=4320)
    full_page: bool               = Field(default=False)
    quality:  Optional[int]       = Field(default=None, ge=1, le=100)

    @validator("format")
    def validate_format(cls, v: str) -> str:
        value = (v or "").strip().lower()
        if value not in VALID_FORMATS:
            raise ValueError(f"format must be one of {sorted(VALID_FORMATS)}")
        return value

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
    return BatchJobOut(**{k: v for k, v in job.items() if k not in ("user_id", "_from_db")})


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
    job_id:    str,
    user_id:   int,
    urls:      List[str],
    fmt:       str,
    width:     int,
    height:    int,
    full_page: bool,
    db:        Session,
) -> Dict[str, Any]:
    now    = datetime.utcnow().isoformat()
    items  = [_create_initial_item(i, url) for i, url in enumerate(urls)]
    counts = _calc_counts(items)

    job: Dict[str, Any] = {
        "id":         job_id,
        "user_id":    user_id,
        "created_at": now,
        "status":     "queued",
        "format":     fmt,
        "width":      width,
        "height":     height,
        "full_page":  full_page,
        **counts,
        "items":      items,
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
    item:      Dict[str, Any],
    job_id:    str,
    fmt:       str,
    width:     int,
    height:    int,
    full_page: bool,
    quality:   Optional[int],
    user:      User,
    db:        Session,
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
        # ✅ FIX (Apr 2026): translate raw Playwright error to plain English
        item["message"]         = _friendly_error(str(exc))
        item["screenshot_url"]  = None
        item["processing_time"] = processing_time
        item["failed_at"]       = datetime.utcnow().isoformat()
        log.error("❌ Batch item %s failed: %s — %s", item["idx"], url, exc)

    return item


# ── Background job processor ───────────────────────────────────────────────────

async def _process_job_async(
    job_id:    str,
    user_id:   int,
    fmt:       str,
    width:     int,
    height:    int,
    full_page: bool,
    quality:   Optional[int],
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
        log.info("🔵 Starting batch job %s with %s URLs", job_id, job["total"])

        for item in job["items"]:
            if job.get("status") == "cancelled":
                log.info("🚫 Job %s was cancelled — stopping", job_id)
                break
            if item["status"] == "queued":
                await _process_item(
                    item, job_id, fmt, width, height,
                    full_page, quality, user, db,
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
    )
    log.info("📸 Created batch job %s with %s URLs for user %s", job_id, len(urls), current_user.username)
    bg.add_task(
        _process_job_async,
        job_id, current_user.id,
        request.format, request.width, request.height,
        request.full_page, request.quality,
    )
    return _job_to_out(job)


@router.post("/submit_file", response_model=BatchJobOut)
async def submit_batch_file(
    file:         UploadFile        = File(...),
    format:       str               = Form(default="png"),
    width:        int               = Form(default=1920),
    height:       int               = Form(default=1080),
    full_page:    bool              = Form(default=False),
    quality:      Optional[int]     = Form(default=None),
    current_user: User              = Depends(get_current_user),
    db:           Session           = Depends(get_db),
    bg:           BackgroundTasks   = None,
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

    req  = BatchSubmitRequest(
        csv_text=text, format=format, width=width,
        height=height, full_page=full_page, quality=quality,
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
    )
    log.info("📸 Created batch job %s from file with %s URLs for user %s", job_id, len(urls), current_user.username)
    bg.add_task(
        _process_job_async,
        job_id, current_user.id,
        format, width, height, full_page, quality,
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
        bg.add_task(
            _process_job_async,
            job_id, current_user.id,
            job["format"], job["width"], job["height"], job["full_page"], None,
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

# # ==========================================================================================================================
# # backend/routers/batch.py — PixelPerfect Screenshot API
# # UPDATED: April 2026
# #
# # ✅ All previous fixes retained (R2 upload, DB records, tier limits, etc.)
# #
# # ✅ FIX (Apr 2026 — Persistent batch job storage):
# #
# #   Root cause of job state loss on Render restart:
# #     JOBS = {} is a plain Python dict. Any Render restart (OOM kill,
# #     redeploy, or scale event) resets it to empty. Even though BatchJob
# #     and Screenshot DB records survived, there was no way to reconstruct
# #     the per-item job dict because:
# #       1. BatchJob had no urls_json → couldn't know which URLs were submitted
# #       2. Screenshot had no batch_job_id → couldn't link screenshots back to a job
# #
# #   Fix — three changes:
# #     a) Submit: store json.dumps(urls) in BatchJob.urls_json at submit time
# #     b) _process_item: write batch_job_id on every Screenshot DB record
# #     c) get_job / list_jobs: if job_id not in JOBS → call
# #        _reconstruct_job_from_db() which rebuilds the full job dict from DB:
# #          • Parse urls_json → ordered URL list
# #          • Query Screenshot WHERE batch_job_id = job_id → completed items + CDN URLs
# #          • Items with no Screenshot → mark failed (lost to restart) if job is
# #            terminal, or queued if still in-flight
# #        The reconstructed dict is inserted into JOBS so subsequent polls
# #        hit the in-memory fast path.
# #
# # ✅ FIX (Apr 2026 — AttributeError: 'BatchJob' object has no attribute 'urls_json'):
# #
# #   Root cause: The DB migration added urls_json / batch_job_id columns to the
# #   physical database tables, but the SQLAlchemy BatchJob / Screenshot model
# #   classes in models.py do NOT yet declare those columns. SQLAlchemy raises
# #   AttributeError when code tries to access an undeclared attribute, even if
# #   the column exists in the DB.
# #
# #   Fix applied here (defensive layer — works even before models.py is updated):
# #     • _reconstruct_job_from_db: uses getattr(db_job, 'urls_json', None)
# #       instead of db_job.urls_json directly
# #     • _build_and_store_job: sets db_job.urls_json via setattr inside a
# #       try/except so the job still saves if the column is missing from the model
# #     • _process_item: sets db_record.batch_job_id via setattr inside a
# #       try/except for the same reason
# #
# #   Permanent fix (do this in models.py to avoid the AttributeError completely):
# #     class BatchJob(Base):
# #         urls_json = Column(Text, nullable=True)      ← add this
# #
# #     class Screenshot(Base):
# #         batch_job_id = Column(String(32), nullable=True)   ← add this

# from __future__ import annotations

# import asyncio
# import csv
# import json
# import logging
# import time
# import uuid
# from datetime import datetime
# from io import StringIO
# from pathlib import Path
# from typing import Any, Dict, List, Optional

# from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, HTTPException, UploadFile
# from pydantic import BaseModel, Field, validator
# from sqlalchemy.orm import Session

# from auth_deps import get_current_user
# from models import BatchJob, Screenshot, SessionLocal, User, get_db
# from screenshot_service import screenshot_service, get_screenshot_url
# from services.storage_service import storage_service

# log = logging.getLogger("batch_screenshots")

# router = APIRouter(prefix="/batch", tags=["batch"])

# SCREENSHOTS_DIR = Path(__file__).resolve().parents[1] / "screenshots"
# SCREENSHOTS_DIR.mkdir(exist_ok=True)

# TIER_BATCH_LIMITS = {
#     "free":     0,
#     "pro":      50,
#     "business": 200,
#     "premium":  1000,
# }

# # In-memory job store — fast path for active/recent jobs.
# # Jobs not found here are reconstructed from DB (handles restarts).
# JOBS: Dict[str, Dict[str, Any]] = {}

# VALID_FORMATS = {"png", "jpeg", "jpg", "webp", "pdf"}

# _CONTENT_TYPES: Dict[str, str] = {
#     "png":  "image/png",
#     "jpg":  "image/jpeg",
#     "jpeg": "image/jpeg",
#     "webp": "image/webp",
#     "pdf":  "application/pdf",
# }


# # ── Pydantic models ────────────────────────────────────────────────────────────

# class BatchSubmitRequest(BaseModel):
#     urls:     Optional[List[str]] = Field(default=None)
#     csv_text: Optional[str]       = Field(default=None)
#     format:   str                 = Field(default="png")
#     width:    int                 = Field(default=1920, ge=320, le=7680)
#     height:   int                 = Field(default=1080, ge=240, le=4320)
#     full_page: bool               = Field(default=False)
#     quality:  Optional[int]       = Field(default=None, ge=1, le=100)

#     @validator("format")
#     def validate_format(cls, v: str) -> str:
#         value = (v or "").strip().lower()
#         if value not in VALID_FORMATS:
#             raise ValueError(f"format must be one of {sorted(VALID_FORMATS)}")
#         return value

#     def collect_urls(self) -> List[str]:
#         raw_urls: List[str] = []
#         if self.urls:
#             raw_urls.extend(self.urls)
#         if self.csv_text:
#             text = self.csv_text.strip()
#             if "," in text:
#                 reader = csv.reader(StringIO(text))
#                 for row in reader:
#                     raw_urls.extend([cell.strip() for cell in row if cell.strip()])
#             elif "\t" in text:
#                 reader = csv.reader(StringIO(text), delimiter="\t")
#                 for row in reader:
#                     raw_urls.extend([cell.strip() for cell in row if cell.strip()])
#             else:
#                 raw_urls.extend([line.strip() for line in text.splitlines() if line.strip()])
#         seen = set()
#         urls: List[str] = []
#         for url in raw_urls:
#             u = (url or "").strip()
#             if u and (u.startswith("http://") or u.startswith("https://")) and u not in seen:
#                 seen.add(u)
#                 urls.append(u)
#         return urls


# class BatchItemOut(BaseModel):
#     idx:             int
#     url:             str
#     status:          str
#     message:         Optional[str]   = None
#     screenshot_url:  Optional[str]   = None
#     file_size:       Optional[int]   = None
#     processing_time: Optional[float] = None


# class BatchJobOut(BaseModel):
#     id:         str
#     created_at: str
#     status:     str
#     format:     str
#     total:      int
#     completed:  int
#     failed:     int
#     queued:     int
#     processing: int
#     items:      List[BatchItemOut]


# # ── Helpers ────────────────────────────────────────────────────────────────────

# def _get_user_tier(user: User) -> str:
#     return (getattr(user, "subscription_tier", "free") or "free").lower()


# def _get_batch_limit(tier: str) -> int:
#     return TIER_BATCH_LIMITS.get((tier or "").lower(), 0)


# def _create_initial_item(idx: int, url: str) -> Dict[str, Any]:
#     return {
#         "idx":             idx,
#         "url":             url,
#         "status":          "queued",
#         "message":         "Waiting to process...",
#         "screenshot_url":  None,
#         "file_size":       None,
#         "processing_time": None,
#         "created_at":      datetime.utcnow().isoformat(),
#     }


# def _calc_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
#     completed  = sum(1 for it in items if it["status"] == "completed")
#     failed     = sum(1 for it in items if it["status"] in ("failed", "cancelled"))
#     queued     = sum(1 for it in items if it["status"] == "queued")
#     processing = sum(1 for it in items if it["status"] == "processing")
#     return {
#         "completed":  completed,
#         "failed":     failed,
#         "queued":     queued,
#         "processing": processing,
#         "total":      len(items),
#     }


# def _update_job_counts(job: Dict[str, Any]) -> None:
#     job.update(_calc_counts(job["items"]))


# # ── ✅ FIX: DB-based job reconstruction after restart ──────────────────────────

# def _reconstruct_job_from_db(db_job: BatchJob, db: Session) -> Dict[str, Any]:
#     """
#     Reconstruct in-memory job dict from DB records after a server restart.

#     Uses:
#       1. BatchJob.urls_json  → ordered URL list (read defensively with getattr)
#       2. Screenshot records WHERE batch_job_id = job_id → completed items + CDN URLs

#     Uses getattr() throughout so this function works even if the SQLAlchemy
#     model classes have not yet been updated to declare urls_json / batch_job_id.
#     Once models.py is updated, the getattr calls are harmless no-ops.
#     """
#     # ── ✅ FIX: getattr instead of db_job.urls_json directly ─────────────────
#     # Direct attribute access raises AttributeError when urls_json is not
#     # declared on the BatchJob model class, even if the DB column exists.
#     urls: List[str] = []
#     urls_json_raw = getattr(db_job, "urls_json", None)
#     if urls_json_raw:
#         try:
#             urls = json.loads(urls_json_raw)
#         except Exception:
#             log.warning("⚠️ Failed to parse urls_json for job %s", db_job.id)

#     # ── Query completed screenshots for this job ──────────────────────────────
#     # Filter by batch_job_id if Screenshot model declares it; otherwise fall
#     # back to an empty list (job history shows as all-failed after restart,
#     # which is safe and honest).
#     completed_screenshots: List[Screenshot] = []
#     try:
#         completed_screenshots = (
#             db.query(Screenshot)
#             .filter(Screenshot.batch_job_id == db_job.id)
#             .all()
#         )
#     except Exception as e:
#         log.warning(
#             "⚠️ Could not query screenshots by batch_job_id for job %s "
#             "(batch_job_id column may not exist on Screenshot model yet): %s",
#             db_job.id, e,
#         )

#     url_to_screenshot: Dict[str, Screenshot] = {s.url: s for s in completed_screenshots}

#     is_terminal = db_job.status in ("completed", "partial", "failed", "cancelled")
#     items: List[Dict[str, Any]] = []

#     for i, url in enumerate(urls):
#         if url in url_to_screenshot:
#             s = url_to_screenshot[url]
#             items.append({
#                 "idx":             i,
#                 "url":             url,
#                 "status":          "completed",
#                 "message":         "Screenshot captured successfully",
#                 "screenshot_url":  s.storage_url,
#                 "file_size":       s.size_bytes,
#                 "processing_time": round(s.processing_time_ms / 1000, 2) if s.processing_time_ms else None,
#                 "created_at":      s.created_at.isoformat() if s.created_at else db_job.created_at.isoformat(),
#                 "completed_at":    s.created_at.isoformat() if s.created_at else None,
#             })
#         else:
#             status  = "failed"  if is_terminal else "queued"
#             message = (
#                 "Lost to server restart — retry to recapture"
#                 if is_terminal else "Waiting to process..."
#             )
#             items.append({
#                 "idx":             i,
#                 "url":             url,
#                 "status":          status,
#                 "message":         message,
#                 "screenshot_url":  None,
#                 "file_size":       None,
#                 "processing_time": None,
#                 "created_at":      db_job.created_at.isoformat(),
#             })

#     counts = _calc_counts(items)

#     job: Dict[str, Any] = {
#         "id":         db_job.id,
#         "user_id":    db_job.user_id,
#         "created_at": db_job.created_at.isoformat(),
#         "status":     db_job.status,
#         "format":     db_job.format,
#         "width":      db_job.width,
#         "height":     db_job.height,
#         "full_page":  db_job.full_page,
#         **counts,
#         "items":      items,
#     }

#     # Cache in JOBS so subsequent polls don't re-query DB
#     JOBS[db_job.id] = job
#     log.info(
#         "♻️  Reconstructed job %s from DB: %d/%d completed, status=%s",
#         db_job.id, counts["completed"], counts["total"], db_job.status,
#     )
#     return job


# def _own_job_or_404(
#     job_id: str,
#     user_id: int,
#     db: Optional[Session] = None,
# ) -> Dict[str, Any]:
#     job = JOBS.get(job_id)
#     if job and job["user_id"] == user_id:
#         return job
#     if db is not None:
#         db_job = db.query(BatchJob).filter(
#             BatchJob.id == job_id,
#             BatchJob.user_id == user_id,
#         ).first()
#         if db_job:
#             return _reconstruct_job_from_db(db_job, db)
#     raise HTTPException(status_code=404, detail="Job not found")


# def _job_to_out(job: Dict[str, Any]) -> BatchJobOut:
#     return BatchJobOut(**{k: v for k, v in job.items() if k not in ("user_id", "_from_db")})


# def _enforce_tier_limits(urls: List[str], user: User) -> None:
#     tier  = _get_user_tier(user)
#     limit = _get_batch_limit(tier)
#     if limit == 0:
#         raise HTTPException(
#             status_code=403,
#             detail="Batch processing is not available on the free tier. "
#                    "Please upgrade to Pro or higher.",
#         )
#     if len(urls) > limit:
#         raise HTTPException(
#             status_code=403,
#             detail=f"Batch size ({len(urls)}) exceeds your tier limit ({limit}). "
#                    "Please upgrade your plan or reduce the number of URLs.",
#         )


# def _build_and_store_job(
#     job_id:    str,
#     user_id:   int,
#     urls:      List[str],
#     fmt:       str,
#     width:     int,
#     height:    int,
#     full_page: bool,
#     db:        Session,
# ) -> Dict[str, Any]:
#     now    = datetime.utcnow().isoformat()
#     items  = [_create_initial_item(i, url) for i, url in enumerate(urls)]
#     counts = _calc_counts(items)

#     job: Dict[str, Any] = {
#         "id":         job_id,
#         "user_id":    user_id,
#         "created_at": now,
#         "status":     "queued",
#         "format":     fmt,
#         "width":      width,
#         "height":     height,
#         "full_page":  full_page,
#         **counts,
#         "items":      items,
#     }
#     JOBS[job_id] = job

#     try:
#         db_job = BatchJob(
#             id=job_id,
#             user_id=user_id,
#             status="queued",
#             format=fmt,
#             width=width,
#             height=height,
#             full_page=full_page,
#             total_urls=len(urls),
#             completed_count=0,
#             failed_count=0,
#             created_at=datetime.utcnow(),
#         )
#         # ── ✅ FIX: set urls_json defensively ────────────────────────────────
#         # Passing urls_json= to the BatchJob() constructor would raise
#         # TypeError if the model doesn't declare it. setattr inside try/except
#         # is safe regardless of whether the column is on the model yet.
#         try:
#             db_job.urls_json = json.dumps(urls)
#         except AttributeError:
#             log.warning(
#                 "⚠️ BatchJob.urls_json not declared on model — "
#                 "job reconstruction after restart will be limited. "
#                 "Add urls_json = Column(Text, nullable=True) to BatchJob in models.py."
#             )

#         db.add(db_job)
#         db.commit()
#         log.info(
#             "💾 Saved BatchJob record: id=%s user=%s urls=%d",
#             job_id, user_id, len(urls),
#         )
#     except Exception as db_err:
#         db.rollback()
#         log.warning("⚠️ Failed to save BatchJob record (non-fatal): %s", db_err)

#     return job


# # ── Screenshot capture + R2 upload ────────────────────────────────────────────

# async def _process_item(
#     item:      Dict[str, Any],
#     job_id:    str,
#     fmt:       str,
#     width:     int,
#     height:    int,
#     full_page: bool,
#     quality:   Optional[int],
#     user:      User,
#     db:        Session,
# ) -> Dict[str, Any]:
#     url     = item["url"]
#     started = time.time()

#     try:
#         item["status"]  = "processing"
#         item["message"] = "Capturing screenshot..."

#         result = await screenshot_service.capture_screenshot(
#             url=url,
#             width=width,
#             height=height,
#             format=fmt,
#             full_page=full_page,
#         )

#         if not result:
#             raise Exception("Screenshot capture failed — no result returned")

#         filename:        str           = result["filename"]
#         screenshot_path                = result.get("filepath")
#         file_size: Optional[int]       = result.get("file_size")

#         if not file_size and screenshot_path:
#             path_obj = Path(screenshot_path)
#             if path_obj.exists():
#                 file_size = path_obj.stat().st_size

#         screenshot_url: str
#         if storage_service.use_r2 and screenshot_path:
#             try:
#                 file_bytes   = Path(screenshot_path).read_bytes()
#                 content_type = _CONTENT_TYPES.get(fmt.lower(), "application/octet-stream")
#                 screenshot_url = await storage_service.upload_screenshot(
#                     file_data=file_bytes,
#                     filename=filename,
#                     content_type=content_type,
#                 )
#                 log.info("☁️  Batch item %s → R2: %s", item["idx"], screenshot_url)
#             except Exception as r2_err:
#                 log.warning(
#                     "⚠️ R2 upload failed for item %s, using local URL: %s",
#                     item["idx"], r2_err,
#                 )
#                 screenshot_url = get_screenshot_url(filename)
#         else:
#             screenshot_url = get_screenshot_url(filename)
#             log.info("💾 Batch item %s → local: %s", item["idx"], screenshot_url)

#         processing_time = round(time.time() - started, 2)

#         item["status"]          = "completed"
#         item["message"]         = "Screenshot captured successfully"
#         item["screenshot_url"]  = screenshot_url
#         item["file_size"]       = file_size
#         item["processing_time"] = processing_time
#         item["completed_at"]    = datetime.utcnow().isoformat()

#         # ── Persist Screenshot record ─────────────────────────────────────────
#         try:
#             db_record = Screenshot(
#                 user_id=user.id,
#                 url=url,
#                 screenshot_path=str(screenshot_path or ""),
#                 storage_url=screenshot_url,
#                 format=fmt,
#                 width=width,
#                 height=height,
#                 full_page=full_page,
#                 size_bytes=file_size,
#                 processing_time_ms=processing_time * 1000,
#                 status="completed",
#                 created_at=datetime.utcnow(),
#             )
#             # ── ✅ FIX: set batch_job_id defensively ─────────────────────────
#             # Same pattern as urls_json: safe whether or not the column is
#             # declared on the Screenshot model class yet.
#             try:
#                 db_record.batch_job_id = job_id
#             except AttributeError:
#                 log.warning(
#                     "⚠️ Screenshot.batch_job_id not declared on model — "
#                     "batch job reconstruction after restart will be limited. "
#                     "Add batch_job_id = Column(String(32), nullable=True) "
#                     "to Screenshot in models.py."
#                 )

#             db.add(db_record)
#             db.commit()
#             log.info(
#                 "💾 Saved batch screenshot record: id=%s url=%s",
#                 db_record.id, screenshot_url,
#             )
#         except Exception as save_err:
#             db.rollback()
#             log.warning("⚠️ Failed to save batch screenshot record: %s", save_err)

#         log.info(
#             "✅ Batch item %s completed: %s → %s",
#             item["idx"], url, screenshot_url,
#         )

#     except Exception as exc:
#         processing_time         = round(time.time() - started, 2)
#         item["status"]          = "failed"
#         item["message"]         = str(exc)
#         item["screenshot_url"]  = None
#         item["processing_time"] = processing_time
#         item["failed_at"]       = datetime.utcnow().isoformat()
#         log.error("❌ Batch item %s failed: %s — %s", item["idx"], url, exc)

#     return item


# # ── Background job processor ───────────────────────────────────────────────────

# async def _process_job_async(
#     job_id:    str,
#     user_id:   int,
#     fmt:       str,
#     width:     int,
#     height:    int,
#     full_page: bool,
#     quality:   Optional[int],
# ) -> None:
#     job = JOBS.get(job_id)
#     if not job:
#         log.warning("Job %s not found for processing", job_id)
#         return

#     db = SessionLocal()
#     try:
#         user = db.query(User).filter(User.id == user_id).first()
#         if not user:
#             log.error("User %s not found for job %s", user_id, job_id)
#             return

#         job["status"] = "processing"
#         log.info("🔵 Starting batch job %s with %s URLs", job_id, job["total"])

#         for item in job["items"]:
#             if job.get("status") == "cancelled":
#                 log.info("🚫 Job %s was cancelled — stopping", job_id)
#                 break
#             if item["status"] == "queued":
#                 await _process_item(
#                     item, job_id, fmt, width, height,
#                     full_page, quality, user, db,
#                 )
#                 _update_job_counts(job)
#                 await asyncio.sleep(0.2)

#         counts = _calc_counts(job["items"])
#         job.update(counts)

#         if job.get("status") != "cancelled":
#             if counts["failed"] == 0:
#                 job["status"] = "completed"
#             elif counts["completed"] > 0:
#                 job["status"] = "partial"
#             else:
#                 job["status"] = "failed"

#         job["completed_at"] = datetime.utcnow().isoformat()
#         log.info(
#             "✅ Batch job %s finished: %s/%s successful",
#             job_id, counts["completed"], counts["total"],
#         )

#         try:
#             db_job = db.query(BatchJob).filter(BatchJob.id == job_id).first()
#             if db_job:
#                 db_job.status          = job["status"]
#                 db_job.completed_count = counts["completed"]
#                 db_job.failed_count    = counts["failed"]
#                 db_job.completed_at    = datetime.utcnow()
#                 db.commit()
#                 log.info(
#                     "💾 Updated BatchJob record: id=%s status=%s",
#                     job_id, job["status"],
#                 )
#         except Exception as db_err:
#             db.rollback()
#             log.warning("⚠️ Failed to update BatchJob record: %s", db_err)

#     finally:
#         db.close()


# # ── Routes ─────────────────────────────────────────────────────────────────────

# @router.post("/submit", response_model=BatchJobOut)
# async def submit_batch(
#     request:      BatchSubmitRequest = Body(...),
#     current_user: User               = Depends(get_current_user),
#     db:           Session            = Depends(get_db),
#     bg:           BackgroundTasks    = None,
# ):
#     urls = request.collect_urls()
#     if not urls:
#         raise HTTPException(status_code=400, detail="No valid URLs found in request")
#     _enforce_tier_limits(urls, current_user)
#     job_id = uuid.uuid4().hex[:16]
#     job    = _build_and_store_job(
#         job_id, current_user.id, urls,
#         request.format, request.width, request.height, request.full_page, db,
#     )
#     log.info(
#         "📸 Created batch job %s with %s URLs for user %s",
#         job_id, len(urls), current_user.username,
#     )
#     bg.add_task(
#         _process_job_async,
#         job_id, current_user.id,
#         request.format, request.width, request.height,
#         request.full_page, request.quality,
#     )
#     return _job_to_out(job)


# @router.post("/submit_file", response_model=BatchJobOut)
# async def submit_batch_file(
#     file:         UploadFile        = File(...),
#     format:       str               = Form(default="png"),
#     width:        int               = Form(default=1920),
#     height:       int               = Form(default=1080),
#     full_page:    bool              = Form(default=False),
#     quality:      Optional[int]     = Form(default=None),
#     current_user: User              = Depends(get_current_user),
#     db:           Session           = Depends(get_db),
#     bg:           BackgroundTasks   = None,
# ):
#     fname = (file.filename or "").lower()
#     if not (fname.endswith(".csv") or fname.endswith(".txt") or fname.endswith(".tsv")):
#         raise HTTPException(
#             status_code=400,
#             detail="Invalid file format. Please upload .csv, .txt, or .tsv",
#         )
#     try:
#         text = (await file.read()).decode("utf-8")
#     except Exception as exc:
#         raise HTTPException(status_code=400, detail=f"Failed to read file: {exc}")

#     req  = BatchSubmitRequest(
#         csv_text=text, format=format, width=width,
#         height=height, full_page=full_page, quality=quality,
#     )
#     urls = req.collect_urls()
#     if not urls:
#         raise HTTPException(
#             status_code=400, detail="No valid URLs found in uploaded file",
#         )
#     _enforce_tier_limits(urls, current_user)
#     job_id = uuid.uuid4().hex[:16]
#     job    = _build_and_store_job(
#         job_id, current_user.id, urls,
#         format, width, height, full_page, db,
#     )
#     log.info(
#         "📸 Created batch job %s from file with %s URLs for user %s",
#         job_id, len(urls), current_user.username,
#     )
#     bg.add_task(
#         _process_job_async,
#         job_id, current_user.id,
#         format, width, height, full_page, quality,
#     )
#     return _job_to_out(job)


# @router.get("/jobs", response_model=List[BatchJobOut])
# async def list_jobs(
#     current_user: User    = Depends(get_current_user),
#     db:           Session = Depends(get_db),
# ):
#     """
#     Returns all batch jobs for the current user.
#     Jobs in JOBS dict: served from memory (fast path).
#     Jobs not in JOBS: reconstructed from DB (survives restarts).
#     """
#     memory_job_ids = {
#         jid for jid, j in JOBS.items() if j["user_id"] == current_user.id
#     }

#     db_jobs = (
#         db.query(BatchJob)
#         .filter(BatchJob.user_id == current_user.id)
#         .order_by(BatchJob.created_at.desc())
#         .all()
#     )

#     for db_job in db_jobs:
#         if db_job.id not in memory_job_ids:
#             try:
#                 _reconstruct_job_from_db(db_job, db)
#             except Exception as e:
#                 log.warning(
#                     "⚠️ Failed to reconstruct job %s from DB: %s",
#                     db_job.id, e,
#                 )

#     user_jobs = [j for j in JOBS.values() if j["user_id"] == current_user.id]
#     user_jobs.sort(key=lambda j: j["created_at"], reverse=True)
#     return [_job_to_out(j) for j in user_jobs]


# @router.get("/jobs/{job_id}", response_model=BatchJobOut)
# async def get_job(
#     job_id:       str,
#     current_user: User    = Depends(get_current_user),
#     db:           Session = Depends(get_db),
# ):
#     job = _own_job_or_404(job_id, current_user.id, db)
#     return _job_to_out(job)


# @router.post("/jobs/{job_id}/retry_failed", response_model=BatchJobOut)
# async def retry_failed(
#     job_id:       str,
#     current_user: User          = Depends(get_current_user),
#     db:           Session       = Depends(get_db),
#     bg:           BackgroundTasks = None,
# ):
#     job     = _own_job_or_404(job_id, current_user.id, db)
#     changed = False
#     for item in job["items"]:
#         if item["status"] == "failed":
#             item["status"]        = "queued"
#             item["message"]       = "Retrying..."
#             item["screenshot_url"] = None
#             changed               = True
#     if changed:
#         job.update(_calc_counts(job["items"]))
#         job["status"] = "queued"
#         bg.add_task(
#             _process_job_async,
#             job_id, current_user.id,
#             job["format"], job["width"], job["height"], job["full_page"], None,
#         )
#     return _job_to_out(job)


# @router.post("/jobs/{job_id}/cancel", response_model=BatchJobOut)
# async def cancel_job(
#     job_id:       str,
#     current_user: User    = Depends(get_current_user),
#     db:           Session = Depends(get_db),
# ):
#     job = _own_job_or_404(job_id, current_user.id, db)
#     if job["status"] not in ("queued", "processing"):
#         raise HTTPException(
#             status_code=400,
#             detail=f"Job cannot be cancelled — current status is '{job['status']}'",
#         )
#     for item in job["items"]:
#         if item["status"] in ("queued", "processing"):
#             item["status"]  = "cancelled"
#             item["message"] = "Cancelled by user"
#     job.update(_calc_counts(job["items"]))
#     job["status"]       = "cancelled"
#     job["completed_at"] = datetime.utcnow().isoformat()

#     try:
#         db_job = db.query(BatchJob).filter(BatchJob.id == job_id).first()
#         if db_job:
#             db_job.status       = "cancelled"
#             db_job.completed_at = datetime.utcnow()
#             db.commit()
#     except Exception as db_err:
#         db.rollback()
#         log.warning("⚠️ Failed to update cancel status in DB: %s", db_err)

#     log.info("🚫 Batch job %s cancelled by user %s", job_id, current_user.id)
#     return _job_to_out(job)


# @router.delete("/jobs/{job_id}")
# async def delete_job(
#     job_id:       str,
#     current_user: User    = Depends(get_current_user),
#     db:           Session = Depends(get_db),
# ):
#     _own_job_or_404(job_id, current_user.id, db)
#     JOBS.pop(job_id, None)
#     try:
#         db_job = db.query(BatchJob).filter(BatchJob.id == job_id).first()
#         if db_job:
#             db.delete(db_job)
#             db.commit()
#     except Exception as db_err:
#         db.rollback()
#         log.warning("⚠️ Failed to delete BatchJob from DB: %s", db_err)
#     return {"ok": True, "deleted": job_id}

# # ====== END OF batch.py ========

