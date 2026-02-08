# =================================================================================================
# backend/routers/batch.py
# PixelPerfect Batch Screenshot Router - PRODUCTION READY
# =================================================================================================
# Batch screenshot processing with full format support: PNG, JPEG, WebP, PDF
# CSV/TXT/TSV file upload support for bulk URL processing
# Author: OneTechly
# Updated: February 2026
# =================================================================================================

from __future__ import annotations

import os
import re
import json
import uuid
import time
import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path
from io import StringIO
import csv

from fastapi import APIRouter, Depends, HTTPException, Request, Body, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session
import logging

from models import User, Screenshot, get_db
from auth_deps import get_current_user
from screenshot_service import screenshot_service

log = logging.getLogger("batch_screenshots")
router = APIRouter(prefix="/api/v1/batch", tags=["batch"])

# ---- Configuration ----
SCREENSHOTS_DIR = Path(__file__).resolve().parents[1] / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

# Tier limits for batch processing
TIER_BATCH_LIMITS = {
    "free": 0,        # No batch processing on free tier
    "pro": 50,        # Up to 50 URLs per batch
    "business": 200,  # Up to 200 URLs per batch  
    "premium": 1000,  # Up to 1000 URLs per batch
}

# ---- In-memory job store ----
JOBS: Dict[str, Dict[str, Any]] = {}

# ---- Pydantic models ----
VALID_FORMATS = {"png", "jpeg", "jpg", "webp", "pdf"}

class BatchSubmitRequest(BaseModel):
    """
    Batch screenshot submission request.
    Supports either direct URL list or CSV text.
    """
    urls: Optional[List[str]] = Field(default=None, description="List of URLs to screenshot")
    csv_text: Optional[str] = Field(default=None, description="CSV/TXT/TSV format URLs")
    format: str = Field(default="png", description="Output format: png, jpeg, webp, pdf")
    width: int = Field(default=1920, ge=320, le=7680, description="Viewport width")
    height: int = Field(default=1080, ge=240, le=4320, description="Viewport height")
    full_page: bool = Field(default=False, description="Capture full page (scrolling)")
    quality: Optional[int] = Field(default=None, ge=1, le=100, description="Quality for JPEG/WebP (1-100)")

    @validator("format")
    def validate_format(cls, v):
        vv = (v or "").strip().lower()
        if vv not in VALID_FORMATS:
            raise ValueError(f"format must be one of {sorted(VALID_FORMATS)}")
        return vv

    def collect_urls(self) -> List[str]:
        """Extract and deduplicate URLs from urls list or csv_text."""
        raw_urls: List[str] = []
        
        # Collect from direct URL list
        if self.urls:
            raw_urls.extend(self.urls)
        
        # Collect from CSV text (supports CSV, TSV, or newline-separated)
        if self.csv_text:
            # Try to detect delimiter
            text = self.csv_text.strip()
            
            # Check if it's comma-separated
            if ',' in text:
                reader = csv.reader(StringIO(text))
                for row in reader:
                    raw_urls.extend([cell.strip() for cell in row if cell.strip()])
            # Check if it's tab-separated
            elif '\t' in text:
                reader = csv.reader(StringIO(text), delimiter='\t')
                for row in reader:
                    raw_urls.extend([cell.strip() for cell in row if cell.strip()])
            # Otherwise treat as newline-separated
            else:
                lines = text.split('\n')
                raw_urls.extend([line.strip() for line in lines if line.strip()])
        
        # Deduplicate while preserving order
        seen = set()
        urls: List[str] = []
        for url in raw_urls:
            url = url.strip()
            # Basic URL validation
            if url and (url.startswith('http://') or url.startswith('https://')):
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
        
        return urls

class BatchItemOut(BaseModel):
    """Individual item in a batch job."""
    idx: int
    url: str
    status: str  # queued, processing, completed, failed
    message: Optional[str] = None
    screenshot_url: Optional[str] = None
    file_size: Optional[int] = None
    processing_time: Optional[float] = None

class BatchJobOut(BaseModel):
    """Batch job response."""
    id: str
    created_at: str
    status: str  # queued, processing, completed, partial, failed
    format: str
    total: int
    completed: int
    failed: int
    queued: int
    processing: int
    items: List[BatchItemOut]

# ---- Helper functions ----

def _get_user_tier(user: User) -> str:
    """Get user's subscription tier."""
    return getattr(user, "subscription_tier", "free") or "free"

def _get_batch_limit(tier: str) -> int:
    """Get batch processing limit for tier."""
    return TIER_BATCH_LIMITS.get(tier.lower(), 0)

def _create_initial_item(idx: int, url: str) -> Dict[str, Any]:
    """Create initial batch item with queued status."""
    return {
        "idx": idx,
        "url": url,
        "status": "queued",
        "message": "Waiting to process...",
        "screenshot_url": None,
        "file_size": None,
        "processing_time": None,
        "created_at": datetime.utcnow().isoformat(),
    }

async def _process_item(
    item: Dict[str, Any],
    format: str,
    width: int,
    height: int,
    full_page: bool,
    quality: Optional[int],
    user: User,
    db: Session,
) -> Dict[str, Any]:
    """Process a single screenshot in the batch."""
    url = item["url"]
    start_time = time.time()
    
    try:
        item["status"] = "processing"
        item["message"] = f"Capturing screenshot..."
        
        # Capture screenshot using screenshot service
        result = await screenshot_service.capture_screenshot(
            url=url,
            width=width,
            height=height,
            format=format,
            full_page=full_page,
            quality=quality,
        )
        
        if not result or not result.get("screenshot_path"):
            raise Exception("Screenshot capture failed")
        
        screenshot_path = Path(result["screenshot_path"])
        
        if not screenshot_path.exists():
            raise Exception("Screenshot file not found")
        
        # Get file info
        file_size = screenshot_path.stat().st_size
        processing_time = time.time() - start_time
        
        # Create relative URL for screenshot
        screenshot_url = f"/screenshots/{screenshot_path.name}"
        
        # Update item status
        item["status"] = "completed"
        item["message"] = "Screenshot captured successfully"
        item["screenshot_url"] = screenshot_url
        item["file_size"] = file_size
        item["processing_time"] = round(processing_time, 2)
        item["completed_at"] = datetime.utcnow().isoformat()
        
        # Save to database
        try:
            screenshot_record = Screenshot(
                user_id=user.id,
                url=url,
                screenshot_path=str(screenshot_path),
                format=format,
                width=width,
                height=height,
                full_page=full_page,
                file_size=file_size,
                processing_time=processing_time,
                created_at=datetime.utcnow(),
            )
            db.add(screenshot_record)
            db.commit()
        except Exception as e:
            log.warning(f"Failed to save screenshot record: {e}")
        
        log.info(f"✅ Batch item {item['idx']} completed: {url} in {processing_time:.2f}s")
        
    except Exception as e:
        processing_time = time.time() - start_time
        log.error(f"❌ Batch item {item['idx']} failed: {url} - {e}")
        
        item["status"] = "failed"
        item["message"] = str(e)
        item["screenshot_url"] = None
        item["processing_time"] = round(processing_time, 2)
        item["failed_at"] = datetime.utcnow().isoformat()
    
    return item

def _calc_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    """Calculate job status counts."""
    completed = sum(1 for it in items if it["status"] == "completed")
    failed = sum(1 for it in items if it["status"] == "failed")
    queued = sum(1 for it in items if it["status"] == "queued")
    processing = sum(1 for it in items if it["status"] == "processing")
    
    return {
        "completed": completed,
        "failed": failed,
        "queued": queued,
        "processing": processing,
        "total": len(items),
    }

def _update_job_counts(job: Dict[str, Any]):
    """Update job counts based on current item statuses."""
    counts = _calc_counts(job["items"])
    job.update(counts)

async def _process_job_async(
    job_id: str,
    user_id: int,
    format: str,
    width: int,
    height: int,
    full_page: bool,
    quality: Optional[int],
):
    """Process all items in a batch job asynchronously."""
    if job_id not in JOBS:
        log.warning(f"Job {job_id} not found for processing")
        return
    
    job = JOBS[job_id]
    
    # Get database session and user
    from models import get_db
    with next(get_db()) as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            log.error(f"User {user_id} not found for job {job_id}")
            return
        
        # Update job status
        job["status"] = "processing"
        
        log.info(f"🔵 Starting batch job {job_id} with {job['total']} URLs")
        
        # Process each queued item
        for item in job["items"]:
            if item["status"] == "queued":
                await _process_item(item, format, width, height, full_page, quality, user, db)
                _update_job_counts(job)
                
                # Small delay between items to avoid overwhelming the system
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
        
        log.info(f"✅ Batch job {job_id} finished: {counts['completed']}/{counts['total']} successful")

def _own_job_or_404(job_id: str, user_id: int) -> Dict[str, Any]:
    """Get job or raise 404 if not found or not owned by user."""
    job = JOBS.get(job_id)
    if not job or job["user_id"] != user_id:
        raise HTTPException(404, "Job not found")
    return job

# ---- Endpoints ----

@router.post("/submit", response_model=BatchJobOut)
async def submit_batch(
    request: BatchSubmitRequest = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    bg: BackgroundTasks = None,
):
    """
    Submit a batch screenshot job.
    
    Supports:
    - Direct URL list via `urls` parameter
    - CSV/TXT/TSV text via `csv_text` parameter
    - File upload via the /submit_file endpoint
    """
    # Get URLs from request
    urls = request.collect_urls()
    
    if not urls:
        raise HTTPException(400, "No valid URLs found in request")
    
    # Check tier limits
    tier = _get_user_tier(current_user)
    limit = _get_batch_limit(tier)
    
    if limit == 0:
        raise HTTPException(
            403,
            "Batch processing is not available on the free tier. Please upgrade to Pro or higher."
        )
    
    if len(urls) > limit:
        raise HTTPException(
            403,
            f"Batch size ({len(urls)}) exceeds your tier limit ({limit}). "
            f"Please upgrade your plan or reduce the number of URLs."
        )
    
    # Create job
    job_id = uuid.uuid4().hex[:16]
    now = datetime.utcnow().isoformat()
    
    items = [_create_initial_item(i, url) for i, url in enumerate(urls)]
    counts = _calc_counts(items)
    
    job = {
        "id": job_id,
        "user_id": current_user.id,
        "created_at": now,
        "status": "queued",
        "format": request.format,
        "width": request.width,
        "height": request.height,
        "full_page": request.full_page,
        **counts,
        "items": items,
    }
    
    JOBS[job_id] = job
    
    log.info(f"📸 Created batch job {job_id} with {len(urls)} URLs for user {current_user.username}")
    
    # Start async processing
    if bg:
        bg.add_task(
            _process_job_async,
            job_id,
            current_user.id,
            request.format,
            request.width,
            request.height,
            request.full_page,
            request.quality,
        )
    
    # Return public job data
    public_job = {k: v for k, v in job.items() if k != "user_id"}
    return BatchJobOut(**public_job)

@router.post("/submit_file", response_model=BatchJobOut)
async def submit_batch_file(
    file: UploadFile = File(...),
    format: str = Form(default="png"),
    width: int = Form(default=1920),
    height: int = Form(default=1080),
    full_page: bool = Form(default=False),
    quality: Optional[int] = Form(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    bg: BackgroundTasks = None,
):
    """
    Submit a batch screenshot job via file upload.
    
    Accepts CSV, TXT, or TSV files containing URLs.
    The file will be automatically parsed to detect the format.
    """
    # Validate file extension
    filename = file.filename.lower()
    if not (filename.endswith('.csv') or filename.endswith('.txt') or filename.endswith('.tsv')):
        raise HTTPException(
            400,
            "Invalid file format. Please upload a .csv, .txt, or .tsv file"
        )
    
    # Read file content
    try:
        content = await file.read()
        text = content.decode('utf-8')
    except Exception as e:
        raise HTTPException(400, f"Failed to read file: {str(e)}")
    
    # Create request with file content
    request = BatchSubmitRequest(
        csv_text=text,
        format=format,
        width=width,
        height=height,
        full_page=full_page,
        quality=quality,
    )
    
    # Process using the main submit endpoint logic
    urls = request.collect_urls()
    
    if not urls:
        raise HTTPException(400, "No valid URLs found in uploaded file")
    
    # Check tier limits
    tier = _get_user_tier(current_user)
    limit = _get_batch_limit(tier)
    
    if limit == 0:
        raise HTTPException(
            403,
            "Batch processing is not available on the free tier. Please upgrade to Pro or higher."
        )
    
    if len(urls) > limit:
        raise HTTPException(
            403,
            f"Batch size ({len(urls)}) exceeds your tier limit ({limit}). "
            f"Please upgrade your plan or reduce the number of URLs."
        )
    
    # Create job
    job_id = uuid.uuid4().hex[:16]
    now = datetime.utcnow().isoformat()
    
    items = [_create_initial_item(i, url) for i, url in enumerate(urls)]
    counts = _calc_counts(items)
    
    job = {
        "id": job_id,
        "user_id": current_user.id,
        "created_at": now,
        "status": "queued",
        "format": format,
        "width": width,
        "height": height,
        "full_page": full_page,
        **counts,
        "items": items,
    }
    
    JOBS[job_id] = job
    
    log.info(f"📸 Created batch job {job_id} from file upload with {len(urls)} URLs for user {current_user.username}")
    
    # Start async processing
    if bg:
        bg.add_task(
            _process_job_async,
            job_id,
            current_user.id,
            format,
            width,
            height,
            full_page,
            quality,
        )
    
    # Return public job data
    public_job = {k: v for k, v in job.items() if k != "user_id"}
    return BatchJobOut(**public_job)

@router.get("/jobs", response_model=List[BatchJobOut])
async def list_jobs(current_user: User = Depends(get_current_user)):
    """List all batch jobs for the current user."""
    user_jobs = [
        job for job in JOBS.values() 
        if job["user_id"] == current_user.id
    ]
    
    # Sort by creation time (newest first)
    user_jobs.sort(key=lambda j: j["created_at"], reverse=True)
    
    # Remove user_id from response
    public_jobs = [
        {k: v for k, v in job.items() if k != "user_id"}
        for job in user_jobs
    ]
    
    return [BatchJobOut(**job) for job in public_jobs]

@router.get("/jobs/{job_id}", response_model=BatchJobOut)
async def get_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
):
    """Get details for a specific batch job."""
    job = _own_job_or_404(job_id, current_user.id)
    
    public_job = {k: v for k, v in job.items() if k != "user_id"}
    return BatchJobOut(**public_job)

@router.post("/jobs/{job_id}/retry_failed", response_model=BatchJobOut)
async def retry_failed(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    bg: BackgroundTasks = None,
):
    """Retry all failed screenshots in a batch job."""
    job = _own_job_or_404(job_id, current_user.id)
    
    # Reset failed items to queued
    changed = False
    for item in job["items"]:
        if item["status"] == "failed":
            item["status"] = "queued"
            item["message"] = "Retrying..."
            item["screenshot_url"] = None
            changed = True
    
    if changed:
        counts = _calc_counts(job["items"])
        job.update(counts)
        job["status"] = "queued"
        
        log.info(f"🔄 Retrying failed items in batch job {job_id}")
        
        # Restart processing
        if bg:
            bg.add_task(
                _process_job_async,
                job_id,
                current_user.id,
                job["format"],
                job["width"],
                job["height"],
                job["full_page"],
                None,  # quality
            )
    
    public_job = {k: v for k, v in job.items() if k != "user_id"}
    return BatchJobOut(**public_job)

@router.delete("/jobs/{job_id}")
async def delete_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
):
    """Delete a batch job."""
    job = _own_job_or_404(job_id, current_user.id)
    
    JOBS.pop(job_id, None)
    
    log.info(f"🗑️ Deleted batch job {job_id}")
    
    return {"ok": True, "deleted": job_id}

# ============= End Batch Screenshot Router =============