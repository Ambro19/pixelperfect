# =================================================================================================
# backend/routers/batch.py
# PixelPerfect Batch Screenshot Processing
# Converted from YCD batch.py - maintains same robustness and professional architecture
# - Batch screenshot processing for multiple URLs
# - Proper usage tracking integration
# - Professional error handling and logging
# - Webhook notifications for completion
# - S3/R2 storage integration
# =================================================================================================

from __future__ import annotations

import os
import re
import json
import uuid
import hmac
import hashlib
import asyncio
import time
from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Body, BackgroundTasks
from pydantic import BaseModel, Field, validator, HttpUrl
from sqlalchemy.orm import Session
import jwt
import logging

# Optional deps (best-effort)
try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None

try:
    import boto3  # type: ignore
    from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
except Exception:  # pragma: no cover
    boto3 = None
    BotoCoreError = ClientError = Exception

from models import User, Screenshot, get_db, get_tier_limits
from services.screenshot_service import screenshot_service
from services.storage_service import storage_service

log = logging.getLogger("batch")
router = APIRouter(prefix="/api/v1/batch", tags=["batch"])

# ---- Auth config (match main.py) --------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "devsecret")
ALGORITHM = os.getenv("ALGORITHM", "HS256")

# ---- Screenshots directory ----
SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

# ---- Batch Size Caps by Tier ------------------------------------------------------------------
FREE_MAX_BATCH = 0  # Free tier: no batch processing
STARTER_MAX_BATCH = 10  # Starter: small batches
PRO_MAX_BATCH = 50  # Pro: medium batches
BUSINESS_MAX_BATCH = 100  # Business: large batches
DEFAULT_MAX_BATCH = 300  # Safety cap

# ---- Optional Integrations (Business tier) ---------------------------------------
WEBHOOK_URL = os.getenv("BATCH_WEBHOOK_URL")
WEBHOOK_SECRET = os.getenv("BATCH_WEBHOOK_SECRET", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
S3_BUCKET = os.getenv("R2_BUCKET_NAME")  # Using R2 bucket name
S3_PREFIX = os.getenv("BATCH_S3_PREFIX", "batch-results/")

def _auth_user(request: Request, db: Session) -> User:
    """Authenticate user from bearer token"""
    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = auth.split()[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise HTTPException(401, "Bad token")
    except Exception:
        raise HTTPException(401, "Invalid token")
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(401, "User not found")
    return user

# ---- Usage Tracking Functions ----
def increment_user_usage(db: Session, user: User, usage_type: str) -> int:
    """Increment user usage counter"""
    current = getattr(user, f"usage_{usage_type}", 0) or 0
    new_val = current + 1
    setattr(user, f"usage_{usage_type}", new_val)

    now = datetime.utcnow()
    if not getattr(user, "usage_reset_date", None):
        user.usage_reset_date = now
    elif user.usage_reset_date.month != now.month:
        # Reset monthly usage
        user.usage_screenshots = 0
        user.usage_batch_requests = 0
        user.usage_api_calls = 0
        user.usage_reset_date = now
        setattr(user, f"usage_{usage_type}", 1)
        new_val = 1

    try:
        db.commit()
        db.refresh(user)
    except Exception as e:
        log.error(f"Failed to increment usage for {user.username}: {e}")
        db.rollback()
    
    return new_val

def check_usage_limit(user: User, usage_type: str) -> tuple[bool, int, int]:
    """Check if user has reached usage limit"""
    tier_limits = get_tier_limits(user.subscription_tier or "free")
    current = getattr(user, f"usage_{usage_type}", 0) or 0
    
    if usage_type == "screenshots":
        limit = tier_limits["screenshots"]
    elif usage_type == "batch_requests":
        limit = tier_limits["batch_requests"]
    else:
        limit = 0
    
    return current < limit, current, limit

# ---- URL Validation ----
def validate_url(url: str) -> str:
    """Validate and normalize URL"""
    url = url.strip()
    
    # Add http:// if no protocol
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    # Basic URL validation
    if not re.match(r'https?://.+\..+', url):
        raise ValueError(f"Invalid URL format: {url}")
    
    return url

# ---- In-memory job store ----------------------------------------------------
JOBS: Dict[str, Dict[str, Any]] = {}

SNAPSHOT_FILE = os.getenv("BATCH_SNAPSHOT_FILE", "./.batch_screenshot_jobs.json")
ENABLE_SNAPSHOT = os.getenv("ENVIRONMENT", "development") != "production"

def _save_snapshot():
    """Save jobs to disk for persistence"""
    if not ENABLE_SNAPSHOT:
        return
    try:
        data = {"jobs": JOBS, "saved_at": datetime.utcnow().isoformat()}
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:  # pragma: no cover
        log.debug("snapshot save skipped: %s", e)

def _load_snapshot():
    """Load jobs from disk"""
    if not ENABLE_SNAPSHOT:
        return
    try:
        if os.path.exists(SNAPSHOT_FILE):
            with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                JOBS.clear()
                JOBS.update(data.get("jobs", {}))
                log.info("Restored %d batch screenshot jobs from snapshot", len(JOBS))
    except Exception as e:  # pragma: no cover
        log.debug("snapshot load failed: %s", e)

_load_snapshot()

# ---- Pydantic Models --------------------------------------------------------

class BatchScreenshotRequest(BaseModel):
    """Request model for batch screenshot submission"""
    urls: List[str] = Field(..., description="List of URLs to screenshot", min_items=1, max_items=100)
    width: int = Field(default=1920, ge=320, le=3840, description="Screenshot width")
    height: int = Field(default=1080, ge=240, le=2160, description="Screenshot height")
    full_page: bool = Field(default=False, description="Capture full page")
    format: str = Field(default="png", description="Image format (png, jpeg, webp)")
    quality: Optional[int] = Field(default=None, ge=0, le=100, description="JPEG quality")
    dark_mode: bool = Field(default=False, description="Enable dark mode")
    delay: int = Field(default=0, ge=0, le=10, description="Delay before screenshot (seconds)")

    @validator("format")
    def validate_format(cls, v):
        if v.lower() not in ["png", "jpeg", "webp"]:
            raise ValueError("Format must be png, jpeg, or webp")
        return v.lower()
    
    @validator("urls")
    def validate_urls(cls, v):
        """Validate and normalize URLs"""
        validated = []
        for url in v:
            try:
                normalized = validate_url(url)
                validated.append(normalized)
            except ValueError as e:
                log.warning(f"Invalid URL skipped: {e}")
        
        if not validated:
            raise ValueError("No valid URLs provided")
        
        return validated

class BatchItemOut(BaseModel):
    """Individual screenshot item in batch"""
    idx: int
    url: str
    status: str
    message: Optional[str] = None
    screenshot_id: Optional[str] = None
    screenshot_url: Optional[str] = None
    file_size: Optional[int] = None
    file_size_mb: Optional[float] = None
    processing_time_ms: Optional[float] = None
    error: Optional[str] = None

class BatchJobOut(BaseModel):
    """Batch job response"""
    id: str
    created_at: str
    status: str
    total: int
    completed: int
    failed: int
    queued: int
    processing: int
    options: Dict[str, Any]
    items: List[BatchItemOut]
    completed_at: Optional[str] = None

# ---- Core Processing Functions -------------------------

def _create_initial_item(idx: int, url: str) -> Dict[str, Any]:
    """Create initial item with queued status"""
    return {
        "idx": idx,
        "url": url,
        "status": "queued",
        "message": "Waiting to process...",
        "screenshot_id": None,
        "screenshot_url": None,
        "file_size": None,
        "file_size_mb": None,
        "processing_time_ms": None,
        "error": None,
        "created_at": datetime.utcnow().isoformat(),
    }

async def _process_item(
    item: Dict[str, Any], 
    options: Dict[str, Any], 
    user: User, 
    db: Session
) -> Dict[str, Any]:
    """Process a single screenshot item"""
    url = item["url"]
    start_time = time.time()
    
    try:
        # Update to processing status
        item["status"] = "processing"
        item["message"] = f"Capturing screenshot..."
        
        # Check usage limits
        can_process, current, limit = check_usage_limit(user, "screenshots")
        
        if not can_process:
            item["status"] = "failed"
            item["message"] = f"Monthly limit reached ({current}/{limit})"
            item["error"] = "usage_limit_exceeded"
            return item
        
        # Initialize screenshot service if needed
        if not screenshot_service.browser:
            await screenshot_service.initialize()
        
        # Capture screenshot
        screenshot_bytes = await screenshot_service.capture_screenshot(
            url=url,
            width=options.get("width", 1920),
            height=options.get("height", 1080),
            full_page=options.get("full_page", False),
            format=options.get("format", "png"),
            quality=options.get("quality"),
            delay=options.get("delay", 0),
            dark_mode=options.get("dark_mode", False),
            remove_elements=None
        )
        
        processing_time = (time.time() - start_time) * 1000  # ms
        
        # Generate ID and filename
        screenshot_id = str(uuid.uuid4())
        filename = f"batch/{user.id}/{screenshot_id}.{options.get('format', 'png')}"
        
        # Save to storage
        try:
            screenshot_url = await storage_service.upload_screenshot(
                file_data=screenshot_bytes,
                filename=filename,
                content_type=f"image/{options.get('format', 'png')}"
            )
        except Exception as e:
            log.warning(f"Storage upload failed, using local: {e}")
            # Local fallback
            local_dir = SCREENSHOTS_DIR / str(user.id) / "batch"
            local_dir.mkdir(parents=True, exist_ok=True)
            local_path = local_dir / f"{screenshot_id}.{options.get('format', 'png')}"
            local_path.write_bytes(screenshot_bytes)
            screenshot_url = f"/screenshots/{user.id}/batch/{screenshot_id}.{options.get('format', 'png')}"
        
        # Save to database
        screenshot_record = Screenshot(
            id=screenshot_id,
            user_id=user.id,
            url=url,
            width=options.get("width", 1920),
            height=options.get("height", 1080),
            full_page=options.get("full_page", False),
            format=options.get("format", "png"),
            quality=options.get("quality"),
            delay_seconds=options.get("delay", 0),
            dark_mode=options.get("dark_mode", False),
            size_bytes=len(screenshot_bytes),
            storage_url=screenshot_url,
            storage_key=filename,
            processing_time_ms=processing_time,
            status="completed",
            created_at=datetime.utcnow()
        )
        db.add(screenshot_record)
        
        # Increment usage
        increment_user_usage(db, user, "screenshots")
        
        db.commit()
        
        # Update item status
        item["status"] = "completed"
        item["message"] = "Screenshot captured successfully"
        item["screenshot_id"] = screenshot_id
        item["screenshot_url"] = screenshot_url
        item["file_size"] = len(screenshot_bytes)
        item["file_size_mb"] = round(len(screenshot_bytes) / (1024 * 1024), 2)
        item["processing_time_ms"] = round(processing_time, 2)
        item["processed_at"] = datetime.utcnow().isoformat()
        
        log.info(f"Successfully captured screenshot for {url} in {processing_time:.0f}ms")
        
    except Exception as e:
        log.error(f"Screenshot failed for {url}: {e}", exc_info=True)
        item["status"] = "failed"
        item["message"] = str(e)
        item["error"] = type(e).__name__
        item["failed_at"] = datetime.utcnow().isoformat()
        db.rollback()
    
    return item

async def _process_job_async(job_id: str, user_id: int):
    """Process all items in a batch job asynchronously"""
    if job_id not in JOBS:
        log.warning(f"Job {job_id} not found for processing")
        return
        
    job = JOBS[job_id]
    options = job["options"]
    
    # Get database session and user
    from models import get_db
    with next(get_db()) as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            log.error(f"User {user_id} not found for job {job_id}")
            return
    
        # Update job status
        job["status"] = "processing"
        _save_snapshot()
        
        log.info(f"Starting batch screenshot job {job_id} for user {user.username}")
        
        # Process each queued item
        for item in job["items"]:
            if item["status"] == "queued":
                await _process_item(item, options, user, db)
                _update_job_counts(job)
                _save_snapshot()
                # Small delay between items
                await asyncio.sleep(0.5)
        
        # Final job status update
        counts = _calc_counts(job["items"])
        job.update(counts)
        
        if counts["failed"] == 0:
            job["status"] = "completed"
        elif counts["completed"] > 0:
            job["status"] = "partial"
        else:
            job["status"] = "failed"
        
        job["completed_at"] = datetime.utcnow().isoformat()
        
        # Increment batch request usage
        increment_user_usage(db, user, "batch_requests")
        
        _save_snapshot()
        
        log.info(f"Job {job_id} completed: {counts['completed']}/{counts['total']} successful")

def _calc_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    """Calculate job statistics"""
    completed = sum(1 for it in items if it["status"] == "completed")
    failed = sum(1 for it in items if it["status"] == "failed")
    queued = sum(1 for it in items if it["status"] == "queued")
    processing = sum(1 for it in items if it["status"] == "processing")
    return {
        "completed": completed, 
        "failed": failed, 
        "queued": queued,
        "processing": processing,
        "total": len(items)
    }

def _update_job_counts(job: Dict[str, Any]):
    """Update job counts based on current item statuses"""
    counts = _calc_counts(job["items"])
    job.update(counts)

def _own_job_or_404(job_id: str, user_id: int) -> Dict[str, Any]:
    """Verify job ownership"""
    job = JOBS.get(job_id)
    if not job or job["user_id"] != user_id:
        raise HTTPException(404, "Job not found")
    return job

# ---- Optional Integrations -----------------------------------------------------------

def _sign(payload: bytes) -> str:
    """Sign webhook payload"""
    if not WEBHOOK_SECRET:
        return ""
    return hmac.new(WEBHOOK_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()

def _notify_webhook(job_public: Dict[str, Any]):
    """Send webhook notification"""
    if not WEBHOOK_URL or not requests:
        return
    try:
        data = json.dumps({"type": "batch.completed", "job": job_public}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        sig = _sign(data)
        if sig:
            headers["X-Signature"] = sig
        requests.post(WEBHOOK_URL, data=data, headers=headers, timeout=10)
        log.info(f"Sent webhook notification for job {job_public['id']}")
    except Exception as e:  # pragma: no cover
        log.warning("webhook notify failed: %s", e)

def _notify_slack(job_public: Dict[str, Any]):
    """Send Slack notification"""
    if not SLACK_WEBHOOK_URL or not requests:
        return
    try:
        completed = job_public.get("completed", 0)
        total = job_public.get("total", 0)
        text = f"📸 Batch screenshot job {job_public.get('id')} finished: {completed}/{total} succeeded."
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=8)
    except Exception as e:  # pragma: no cover
        log.warning("slack notify failed: %s", e)

def _upload_s3(job_public: Dict[str, Any]):
    """Upload job summary to S3/R2"""
    if not S3_BUCKET or not boto3:
        return
    try:
        s3 = boto3.client("s3")
        key = f"{S3_PREFIX}{job_public['id']}.json"
        body = json.dumps(job_public, ensure_ascii=False).encode("utf-8")
        s3.put_object(Bucket=S3_BUCKET, Key=key, Body=body, ContentType="application/json")
        log.info("Uploaded batch summary to s3://%s/%s", S3_BUCKET, key)
    except (BotoCoreError, ClientError, Exception) as e:  # pragma: no cover
        log.warning("s3 upload failed: %s", e)

# ---- API Endpoints -------------------------

@router.post("/submit", response_model=BatchJobOut)
async def submit_batch(
    payload: BatchScreenshotRequest = Body(...),
    request: Request = None,
    db: Session = Depends(get_db),
    bg: BackgroundTasks = None,
):
    """
    Submit a batch screenshot job
    
    Process multiple URLs in a single batch request. Batch size limits depend on tier:
    - Free: No batch processing
    - Starter: 10 URLs
    - Pro: 50 URLs
    - Business: 100 URLs
    """
    user = _auth_user(request, db)
    urls = payload.urls
    
    if not urls:
        raise HTTPException(400, "No valid URLs provided")

    # Enforce tier caps
    tier = (getattr(user, "subscription_tier", "free") or "free").lower()
    tier_limits = get_tier_limits(tier)
    max_batch = tier_limits.get("max_batch_size", 0)
    
    if max_batch == 0:
        raise HTTPException(
            403, 
            "Batch processing not available in Free tier. Please upgrade to Pro or Business."
        )
    
    if len(urls) > max_batch:
        urls = urls[:max_batch]
        log.info(f"Limited batch for user {user.username} to {max_batch} URLs (tier: {tier})")

    # Check batch request usage limits
    can_process, current, limit = check_usage_limit(user, "batch_requests")
    
    if not can_process:
        raise HTTPException(
            429, 
            f"Monthly batch request limit reached ({current}/{limit}). Please upgrade your plan."
        )
    
    # Check screenshot usage limits
    can_screenshot, screenshot_current, screenshot_limit = check_usage_limit(user, "screenshots")
    remaining_capacity = max(0, screenshot_limit - screenshot_current) if screenshot_limit != float("inf") else len(urls)
    
    if remaining_capacity == 0:
        raise HTTPException(
            429, 
            f"Monthly screenshot limit reached ({screenshot_current}/{screenshot_limit}). Please upgrade your plan."
        )
    
    # Limit batch size based on remaining screenshot capacity
    if remaining_capacity < len(urls):
        urls = urls[:remaining_capacity]
        log.info(f"Limited batch for user {user.username} to {remaining_capacity} URLs due to usage limits")

    job_id = uuid.uuid4().hex[:16]
    now = datetime.utcnow().isoformat()

    # Store request options
    options = {
        "width": payload.width,
        "height": payload.height,
        "full_page": payload.full_page,
        "format": payload.format,
        "quality": payload.quality,
        "dark_mode": payload.dark_mode,
        "delay": payload.delay
    }

    # Create initial items
    items = [_create_initial_item(i, url) for i, url in enumerate(urls)]
    counts = _calc_counts(items)

    job = {
        "id": job_id,
        "user_id": user.id,
        "created_at": now,
        "status": "queued",
        "options": options,
        **counts,
        "items": items,
        "completed_at": None
    }
    JOBS[job_id] = job
    _save_snapshot()

    log.info(f"Created batch job {job_id} with {len(urls)} items for user {user.username}")

    # Start async processing
    if bg:
        bg.add_task(_process_job_async, job_id, user.id)

    public_job = {k: v for k, v in job.items() if k != "user_id"}

    # Business tier integrations
    if tier == "business" and bg is not None:
        bg.add_task(_notify_webhook, public_job)
        bg.add_task(_notify_slack, public_job)
        bg.add_task(_upload_s3, public_job)

    return BatchJobOut(**public_job)

@router.get("/jobs", response_model=List[BatchJobOut])
def list_jobs(request: Request, db: Session = Depends(get_db)):
    """List all batch jobs for the current user"""
    user = _auth_user(request, db)
    rows = sorted(
        [j for j in JOBS.values() if j["user_id"] == user.id],
        key=lambda j: j["created_at"],
        reverse=True,
    )
    out = []
    for j in rows:
        out.append(BatchJobOut(**{k: v for k, v in j.items() if k != "user_id"}))
    return out

@router.get("/jobs/{job_id}", response_model=BatchJobOut)
def get_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    """Get details of a specific batch job"""
    user = _auth_user(request, db)
    job = _own_job_or_404(job_id, user.id)
    return BatchJobOut(**{k: v for k, v in job.items() if k != "user_id"})

@router.post("/jobs/{job_id}/retry_failed", response_model=BatchJobOut)
async def retry_failed(
    job_id: str, 
    request: Request, 
    db: Session = Depends(get_db), 
    bg: BackgroundTasks = None
):
    """Retry failed screenshots in a batch job"""
    user = _auth_user(request, db)
    job = _own_job_or_404(job_id, user.id)
    
    # Reset failed items to queued
    changed = False
    for item in job["items"]:
        if item["status"] == "failed":
            item["status"] = "queued"
            item["message"] = "Retrying..."
            item["screenshot_url"] = None
            item["error"] = None
            changed = True

    if changed:
        counts = _calc_counts(job["items"])
        job.update(counts)
        job["status"] = "queued"
        job["completed_at"] = None
        _save_snapshot()
        
        log.info(f"Retrying failed items in job {job_id}")
        
        # Start async processing for retry
        if bg:
            bg.add_task(_process_job_async, job_id, user.id)
    
    return BatchJobOut(**{k: v for k, v in job.items() if k != "user_id"})

@router.delete("/jobs/{job_id}", response_model=dict)
def delete_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    """Delete a batch job"""
    user = _auth_user(request, db)
    job = _own_job_or_404(job_id, user.id)
    JOBS.pop(job_id, None)
    _save_snapshot()
    log.info(f"Deleted batch job {job_id} for user {user.username}")
    return {"ok": True, "deleted": job_id}

# ============= End Batch Screenshot Module =============