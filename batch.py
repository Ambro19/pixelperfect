# =================================================================================================
# backend/batch.py
# Enhanced Batch Jobs router with full transcript/audio/video support and usage tracking
# - Supports all three download types: transcript, audio, video
# - Proper usage tracking integration for all types
# - Professional download handling with quality settings
# - FIXED: Intelligent paragraph formatting for consistent, professional transcript output
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
from pydantic import BaseModel, Field, validator
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

from models import User, get_db
from transcript_utils import (
    get_transcript_with_ytdlp,
    download_audio_with_ytdlp, 
    download_video_with_ytdlp,
    get_video_info,
    check_ytdlp_availability
)

log = logging.getLogger("batch")
router = APIRouter(prefix="/batch", tags=["batch"])

# ---- Auth config (match main.py/payment.py) --------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "devsecret")
ALGORITHM = os.getenv("ALGORITHM", "HS256")

# ---- Downloads directory ----
DOWNLOADS_DIR = Path(__file__).resolve().parent / "server_files"
DOWNLOADS_DIR.mkdir(exist_ok=True)

# ---- Caps ------------------------------------------------------------------
PRO_MAX_LINKS = int(os.getenv("BATCH_PRO_MAX_LINKS", "3"))
PREMIUM_MAX_LINKS = int(os.getenv("BATCH_PREMIUM_MAX_LINKS", "2000"))
DEFAULT_MAX_LINKS = int(os.getenv("BATCH_DEFAULT_MAX_LINKS", "300"))  # safety

# ---- Optional Integrations (Premium) ---------------------------------------
WEBHOOK_URL = os.getenv("BATCH_WEBHOOK_URL")
WEBHOOK_SECRET = os.getenv("BATCH_WEBHOOK_SECRET", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
S3_BUCKET = os.getenv("BATCH_S3_BUCKET")
S3_PREFIX = os.getenv("BATCH_S3_PREFIX", "batch-results/")

def _auth_user(request: Request, db: Session) -> User:
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

# ---- Import usage tracking functions from main.py (avoid circular imports) ----
def increment_user_usage(db: Session, user: User, usage_type: str) -> int:
    """Increment user usage counter (imported logic from main.py)"""
    current = getattr(user, f"usage_{usage_type}", 0) or 0
    new_val = current + 1
    setattr(user, f"usage_{usage_type}", new_val)

    now = datetime.utcnow()
    if not getattr(user, "usage_reset_date", None):
        user.usage_reset_date = now
    elif user.usage_reset_date.month != now.month:
        user.usage_clean_transcripts = 0
        user.usage_unclean_transcripts = 0
        user.usage_audio_downloads = 0
        user.usage_video_downloads = 0
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
    """Check if user has reached usage limit (imported logic from main.py)"""
    tier = getattr(user, "subscription_tier", "free")
    limits = {
        "free":   {"clean_transcripts": 5,  "unclean_transcripts": 3,  "audio_downloads": 2,  "video_downloads": 1},
        "pro":    {"clean_transcripts": 100,"unclean_transcripts": 50, "audio_downloads": 50, "video_downloads": 20},
        "premium":{"clean_transcripts": float("inf"), "unclean_transcripts": float("inf"),
                   "audio_downloads": float("inf"), "video_downloads": float("inf")},
    }
    cur = getattr(user, f"usage_{usage_type}", 0) or 0
    limit = limits.get(tier, limits["free"]).get(usage_type, 0)
    return cur < limit, cur, limit

def create_download_record(db: Session, user: User, kind: str, youtube_id: str, **kw):
    """Create download record (imported logic from main.py)"""
    try:
        from models import TranscriptDownload
        rec = TranscriptDownload(
            user_id=user.id,
            youtube_id=youtube_id,
            transcript_type=kind,
            quality=kw.get("quality", "default"),
            file_format=kw.get("file_format", "txt"),
            file_size=kw.get("file_size", 0),
            processing_time=kw.get("processing_time", 0),
            created_at=datetime.utcnow(),
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return rec
    except Exception as e:
        log.error(f"Create download record failed: {e}")
        db.rollback()
        return None

# ---- Simple ID extractor (no import from main.py to avoid circular deps) ---
_YT_PATTERNS = [
    re.compile(r"(?:youtube\.com\/watch\?v=)([^&\n?#]+)", re.I),
    re.compile(r"(?:youtu\.be\/)([^&\n?#]+)", re.I),
    re.compile(r"(?:youtube\.com\/embed\/)([^&\n?#]+)", re.I),
    re.compile(r"(?:youtube\.com\/shorts\/)([^&\n?#]+)", re.I),
    re.compile(r"[?&]v=([^&\n?#]+)", re.I),
]

def extract_youtube_video_id(text: str) -> str:
    s = (text or "").strip()
    for p in _YT_PATTERNS:
        m = p.search(s)
        if m:
            return m.group(1)[:11]
    return s[:11]  # assume raw ID

# ---- ENHANCED: Intelligent Transcript Paragraph Formatting ------------------
def format_transcript_with_paragraphs(transcript: str) -> str:
    """
    Format raw transcript text into readable paragraphs for professional presentation.
    
    This function ensures consistent, professional formatting across all transcripts by:
    - Removing existing excessive line breaks
    - Intelligently grouping sentences into paragraphs
    - Adding proper spacing between paragraphs
    - Handling edge cases (timestamps, special characters, etc.)
    
    Args:
        transcript: Raw transcript text (may or may not have formatting)
        
    Returns:
        Professionally formatted transcript with proper paragraphs
    """
    if not transcript or not transcript.strip():
        return transcript
    
    # Step 1: Normalize the text - remove excessive whitespace and line breaks
    text = transcript.strip()
    
    # Remove multiple spaces
    text = re.sub(r' +', ' ', text)
    
    # Remove multiple line breaks (keep content flowing)
    text = re.sub(r'\n+', ' ', text)
    
    # Step 2: Identify sentence boundaries for intelligent splitting
    # Split on periods, question marks, exclamation marks followed by space and capital letter
    # This regex looks for: . ! ? followed by space(s) and a capital letter or number
    sentences = re.split(r'([.!?]+)\s+(?=[A-Z0-9])', text)
    
    # Reconstruct sentences with their punctuation
    reconstructed_sentences = []
    i = 0
    while i < len(sentences):
        if i + 1 < len(sentences) and sentences[i + 1] in ['.', '!', '?', '..', '...', '!!', '??']:
            reconstructed_sentences.append(sentences[i] + sentences[i + 1])
            i += 2
        else:
            reconstructed_sentences.append(sentences[i])
            i += 1
    
    # Filter out empty sentences
    sentences = [s.strip() for s in reconstructed_sentences if s.strip()]
    
    if not sentences:
        return transcript
    
    # Step 3: Group sentences into paragraphs
    # Create paragraphs of 3-5 sentences for optimal readability
    paragraphs = []
    current_paragraph = []
    sentences_per_paragraph = 4  # Sweet spot for readability
    
    for i, sentence in enumerate(sentences):
        current_paragraph.append(sentence)
        
        # Create a new paragraph when we reach the target length
        # OR when we detect a natural topic shift (optional enhancement)
        if len(current_paragraph) >= sentences_per_paragraph:
            paragraphs.append(' '.join(current_paragraph))
            current_paragraph = []
    
    # Add any remaining sentences as the last paragraph
    if current_paragraph:
        paragraphs.append(' '.join(current_paragraph))
    
    # Step 4: Join paragraphs with double line breaks for visual separation
    formatted_text = '\n\n'.join(paragraphs)
    
    # Step 5: Clean up any potential formatting artifacts
    # Remove any accidental triple+ line breaks
    formatted_text = re.sub(r'\n{3,}', '\n\n', formatted_text)
    
    # Ensure clean ending
    formatted_text = formatted_text.strip()
    
    log.debug(f"Formatted transcript: {len(paragraphs)} paragraphs from {len(sentences)} sentences")
    
    return formatted_text


# ---- In-memory job store ----------------------------------------------------
JOBS: Dict[str, Dict[str, Any]] = {}

SNAPSHOT_FILE = os.getenv("BATCH_SNAPSHOT_FILE", "./.batch_jobs_snapshot.json")
ENABLE_SNAPSHOT = os.getenv("ENVIRONMENT", "development") != "production"

def _save_snapshot():
    if not ENABLE_SNAPSHOT:
        return
    try:
        data = {"jobs": JOBS, "saved_at": datetime.utcnow().isoformat()}
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:  # pragma: no cover
        log.debug("snapshot save skipped: %s", e)

def _load_snapshot():
    if not ENABLE_SNAPSHOT:
        return
    try:
        if os.path.exists(SNAPSHOT_FILE):
            with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                JOBS.clear()
                JOBS.update(data.get("jobs", {}))
                log.info("Restored %d batch jobs from snapshot", len(JOBS))
    except Exception as e:  # pragma: no cover
        log.debug("snapshot load failed: %s", e)

_load_snapshot()

# ---- Pydantic models --------------------------------------------------------
VALID_KINDS = {"transcript", "audio", "video"}

class SubmitBody(BaseModel):
    urls: Optional[List[str]] = Field(default=None, description="List of YouTube URLs or IDs")
    csv_text: Optional[str] = Field(default=None, description="CSV or newline list of URLs/IDs")
    kind: str = Field(default="transcript", description="transcript|audio|video")
    quality: Optional[str] = Field(default=None, description="Quality setting for audio/video")

    @validator("kind")
    def _kind_ok(cls, v):
        vv = (v or "").strip().lower()
        if vv not in VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(VALID_KINDS)}")
        return vv

    def collect_ids(self) -> List[str]:
        raw: List[str] = []
        if self.urls:
            raw.extend(self.urls)
        if self.csv_text:
            parts = re.split(r"[\s,;]+", self.csv_text.strip())
            raw.extend([p for p in parts if p])
        # de-dupe while preserving order
        seen = set()
        ids: List[str] = []
        for entry in raw:
            vid = extract_youtube_video_id(entry)
            if len(vid) == 11 and vid not in seen:
                seen.add(vid)
                ids.append(vid)
        # safety cap before tier rules
        return ids[:DEFAULT_MAX_LINKS]

class ItemOut(BaseModel):
    idx: int
    youtube_id: str
    status: str
    message: Optional[str] = None
    result_type: str
    result_url: Optional[str] = None
    file_size: Optional[int] = None
    file_size_mb: Optional[float] = None
    video_title: Optional[str] = None
    download_links: Optional[Dict[str, str]] = None

class JobOut(BaseModel):
    id: str
    created_at: str
    status: str
    kind: str
    total: int
    completed: int
    failed: int
    queued: int
    processing: int
    items: List[ItemOut]

# ---- Core processing functions with usage tracking -------------------------

def _create_initial_item(idx: int, vid: str, kind: str) -> Dict[str, Any]:
    """Create initial item with queued status for real processing."""
    return {
        "idx": idx,
        "youtube_id": vid,
        "status": "queued",
        "message": "Waiting to process...",
        "result_type": kind,
        "result_url": None,
        "file_size": None,
        "file_size_mb": None,
        "video_title": None,
        "download_links": None,
        "created_at": datetime.utcnow().isoformat(),
    }

def _process_item(item: Dict[str, Any], kind: str, user: User, db: Session, quality: Optional[str] = None) -> Dict[str, Any]:
    """Process a single item with proper usage tracking integration for all types."""
    vid = item["youtube_id"]
    start_time = time.time()
    
    try:
        # Update to processing status
        item["status"] = "processing"
        item["message"] = f"Processing {kind}..."
        
        # Check usage limits before processing based on kind
        usage_type_map = {
            "transcript": "clean_transcripts",  # Default to clean for batch
            "audio": "audio_downloads",
            "video": "video_downloads"
        }
        
        usage_type = usage_type_map.get(kind, "clean_transcripts")
        can_process, current, limit = check_usage_limit(user, usage_type)
        
        if not can_process:
            item["status"] = "failed"
            item["message"] = f"Monthly limit reached ({current}/{limit})"
            return item
        
        # Get video info first
        video_info = get_video_info(vid)
        if video_info:
            item["video_title"] = video_info.get("title", "Unknown Title")
        
        processing_time = 0
        
        if kind == "transcript":
            # Process transcript with ENHANCED FORMATTING
            raw_transcript = get_transcript_with_ytdlp(vid, clean=True)
            if raw_transcript:
                # Apply intelligent paragraph formatting for consistent, professional output
                formatted_transcript = format_transcript_with_paragraphs(raw_transcript)
                
                item["status"] = "completed"
                item["message"] = "Transcript ready"
                item["file_size"] = len(formatted_transcript.encode('utf-8'))
                item["file_size_mb"] = round(item["file_size"] / (1024 * 1024), 2)
                item["download_links"] = {
                    "transcript": f"/batch/download/{vid}/transcript",
                    "view": f"/batch/view/{vid}/transcript"
                }
                
                # Store the FORMATTED transcript content for download
                _store_transcript_result(vid, formatted_transcript)
                
                # Track usage and create record
                processing_time = time.time() - start_time
                increment_user_usage(db, user, usage_type)
                create_download_record(
                    db=db, user=user, kind=usage_type, youtube_id=vid,
                    file_format="txt", file_size=item["file_size"], 
                    processing_time=processing_time
                )
                
                log.info(f"Successfully formatted and stored transcript for {vid}")
                
            else:
                raise Exception("No transcript available")
                
        elif kind == "audio":
            # Process audio download
            if not check_ytdlp_availability():
                raise Exception("Audio processing unavailable")
            
            audio_quality = quality or "medium"
            file_path = download_audio_with_ytdlp(vid, audio_quality, str(DOWNLOADS_DIR))
            if file_path and os.path.exists(file_path):
                file_size = os.path.getsize(file_path)
                filename = os.path.basename(file_path)
                
                item["status"] = "completed"
                item["message"] = "Audio ready for download"
                item["result_url"] = f"/files/{filename}"
                item["file_size"] = file_size
                item["file_size_mb"] = round(file_size / (1024 * 1024), 2)
                item["download_links"] = {
                    "audio": f"/files/{filename}",
                    "direct": f"/download-file/audio/{filename}"
                }
                
                # Track usage and create record
                processing_time = time.time() - start_time
                increment_user_usage(db, user, usage_type)
                create_download_record(
                    db=db, user=user, kind=usage_type, youtube_id=vid,
                    quality=audio_quality, file_format="mp3", file_size=file_size,
                    processing_time=processing_time
                )
                
            else:
                raise Exception("Audio download failed")
                
        elif kind == "video":
            # Process video download
            if not check_ytdlp_availability():
                raise Exception("Video processing unavailable")
                
            video_quality = quality or "720p"
            file_path = download_video_with_ytdlp(vid, video_quality, str(DOWNLOADS_DIR))
            if file_path and os.path.exists(file_path):
                file_size = os.path.getsize(file_path)
                filename = os.path.basename(file_path)
                
                item["status"] = "completed"
                item["message"] = "Video ready for download"
                item["result_url"] = f"/files/{filename}"
                item["file_size"] = file_size
                item["file_size_mb"] = round(file_size / (1024 * 1024), 2)
                item["download_links"] = {
                    "video": f"/files/{filename}",
                    "direct": f"/download-file/video/{filename}"
                }
                
                # Track usage and create record
                processing_time = time.time() - start_time
                increment_user_usage(db, user, usage_type)
                create_download_record(
                    db=db, user=user, kind=usage_type, youtube_id=vid,
                    quality=video_quality, file_format="mp4", file_size=file_size,
                    processing_time=processing_time
                )
                
            else:
                raise Exception("Video download failed")
        
        item["processed_at"] = datetime.utcnow().isoformat()
        log.info(f"Successfully processed {kind} for {vid} in {processing_time:.2f}s")
        
    except Exception as e:
        log.error(f"Processing failed for {vid} ({kind}): {e}")
        item["status"] = "failed"
        item["message"] = str(e)
        item["result_url"] = None
        item["download_links"] = None
        item["failed_at"] = datetime.utcnow().isoformat()
    
    return item

def _store_transcript_result(vid: str, transcript: str):
    """Store transcript content for later download."""
    transcript_file = DOWNLOADS_DIR / f"{vid}_transcript.txt"
    try:
        transcript_file.write_text(transcript, encoding='utf-8')
        log.debug(f"Stored transcript for {vid}: {len(transcript)} chars")
    except Exception as e:
        log.warning(f"Failed to store transcript for {vid}: {e}")

async def _process_job_async(job_id: str, user_id: int, quality: Optional[str] = None):
    """Process all items in a job asynchronously with usage tracking."""
    if job_id not in JOBS:
        log.warning(f"Job {job_id} not found for processing")
        return
        
    job = JOBS[job_id]
    kind = job["kind"]
    
    # Get database session and user for usage tracking
    from models import get_db
    with next(get_db()) as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            log.error(f"User {user_id} not found for job {job_id}")
            return
    
        # Update job status
        job["status"] = "processing"
        _save_snapshot()
        
        log.info(f"Starting batch processing job {job_id} ({kind}) for user {user.username}")
        
        # Process each queued item
        for item in job["items"]:
            if item["status"] == "queued":
                _process_item(item, kind, user, db, quality)
                _update_job_counts(job)
                _save_snapshot()
                # Small delay between items to avoid overwhelming the system
                await asyncio.sleep(1)
        
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
        _save_snapshot()
        
        log.info(f"Job {job_id} completed: {counts['completed']}/{counts['total']} successful ({kind})")

def _calc_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
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
    """Update job counts based on current item statuses."""
    counts = _calc_counts(job["items"])
    job.update(counts)

def _own_job_or_404(job_id: str, user_id: int) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job or job["user_id"] != user_id:
        raise HTTPException(404, "Job not found")
    return job

# ---- Integrations -----------------------------------------------------------

def _sign(payload: bytes) -> str:
    if not WEBHOOK_SECRET:
        return ""
    return hmac.new(WEBHOOK_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()

def _notify_webhook(job_public: Dict[str, Any]):
    if not WEBHOOK_URL or not requests:
        return
    try:
        data = json.dumps({"type": "batch.completed", "job": job_public}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        sig = _sign(data)
        if sig:
            headers["X-Signature"] = sig
        requests.post(WEBHOOK_URL, data=data, headers=headers, timeout=10)
    except Exception as e:  # pragma: no cover
        log.warning("webhook notify failed: %s", e)

def _notify_slack(job_public: Dict[str, Any]):
    if not SLACK_WEBHOOK_URL or not requests:
        return
    try:
        completed = job_public.get("completed", 0)
        total = job_public.get("total", 0)
        kind = job_public.get("kind", "unknown")
        text = f"Batch {job_public.get('id')} ({kind}) finished: {completed}/{total} succeeded."
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=8)
    except Exception as e:  # pragma: no cover
        log.warning("slack notify failed: %s", e)

def _upload_s3(job_public: Dict[str, Any]):
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

# ---- Endpoints with usage validation for all types -------------------------

@router.post("/submit", response_model=JobOut)
async def submit_batch(
    payload: SubmitBody = Body(...),
    request: Request = None,
    db: Session = Depends(get_db),
    bg: BackgroundTasks = None,
):
    user = _auth_user(request, db)
    ids = payload.collect_ids()
    if not ids:
        raise HTTPException(400, "No valid YouTube IDs found")

    # Enforce tier caps
    tier = getattr(user, "subscription_tier", "free") or "free"
    if tier == "pro":
        ids = ids[:PRO_MAX_LINKS]
    elif tier == "premium":
        ids = ids[:PREMIUM_MAX_LINKS]
    else:
        ids = ids[:min(1, DEFAULT_MAX_LINKS)]  # Free has no batch feature

    # Pre-validate usage limits for batch submission based on kind
    usage_type_map = {
        "transcript": "clean_transcripts",
        "audio": "audio_downloads", 
        "video": "video_downloads"
    }
    
    usage_type = usage_type_map.get(payload.kind, "clean_transcripts")
    can_process, current, limit = check_usage_limit(user, usage_type)
    
    # Check if user can process at least some items
    remaining_capacity = max(0, limit - current) if limit != float("inf") else len(ids)
    
    if remaining_capacity == 0:
        raise HTTPException(
            403, 
            f"Monthly limit reached for {payload.kind} ({current}/{limit}). Please upgrade your plan."
        )
    
    # Limit batch size based on remaining capacity
    if remaining_capacity < len(ids):
        ids = ids[:remaining_capacity]
        log.info(f"Limiting {payload.kind} batch for user {user.username} to {remaining_capacity} items due to usage limits")

    job_id = uuid.uuid4().hex[:16]
    now = datetime.utcnow().isoformat()

    # Create initial items with queued status
    items = [_create_initial_item(i, vid, payload.kind) for i, vid in enumerate(ids)]
    counts = _calc_counts(items)

    job = {
        "id": job_id,
        "user_id": user.id,
        "created_at": now,
        "status": "queued",
        "kind": payload.kind,
        "quality": payload.quality,
        **counts,
        "items": items,
    }
    JOBS[job_id] = job
    _save_snapshot()

    log.info(f"Created batch job {job_id} ({payload.kind}) with {len(ids)} items for user {user.username}")

    # Start async processing with user context and quality settings
    if bg:
        bg.add_task(_process_job_async, job_id, user.id, payload.quality)

    public_job = {k: v for k, v in job.items() if k != "user_id"}

    # Premium integrations (fire-and-forget)
    if tier == "premium" and bg is not None:
        bg.add_task(_notify_webhook, public_job)
        bg.add_task(_notify_slack, public_job)
        bg.add_task(_upload_s3, public_job)

    return JobOut(**public_job)

@router.get("/jobs", response_model=List[JobOut])
def list_jobs(request: Request, db: Session = Depends(get_db)):
    user = _auth_user(request, db)
    rows = sorted(
        [j for j in JOBS.values() if j["user_id"] == user.id],
        key=lambda j: j["created_at"],
        reverse=True,
    )
    out = []
    for j in rows:
        out.append(JobOut(**{k: v for k, v in j.items() if k != "user_id"}))
    return out

@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    user = _auth_user(request, db)
    job = _own_job_or_404(job_id, user.id)
    return JobOut(**{k: v for k, v in job.items() if k != "user_id"})

@router.post("/jobs/{job_id}/retry_failed", response_model=JobOut)
async def retry_failed(job_id: str, request: Request, db: Session = Depends(get_db), bg: BackgroundTasks = None):
    user = _auth_user(request, db)
    job = _own_job_or_404(job_id, user.id)
    
    # Reset failed items to queued
    changed = False
    for item in job["items"]:
        if item["status"] == "failed":
            item["status"] = "queued"
            item["message"] = "Retrying..."
            item["result_url"] = None
            item["download_links"] = None
            changed = True

    if changed:
        counts = _calc_counts(job["items"])
        job.update(counts)
        job["status"] = "queued"
        _save_snapshot()
        
        log.info(f"Retrying failed items in job {job_id} ({job['kind']})")
        
        # Start async processing for retry with user context and quality
        if bg:
            bg.add_task(_process_job_async, job_id, user.id, job.get("quality"))
    
    return JobOut(**{k: v for k, v in job.items() if k != "user_id"})

@router.delete("/jobs/{job_id}", response_model=dict)
def delete_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    user = _auth_user(request, db)
    job = _own_job_or_404(job_id, user.id)
    JOBS.pop(job_id, None)
    _save_snapshot()
    log.info(f"Deleted job {job_id} ({job['kind']}) for user {user.username}")
    return {"ok": True, "deleted": job_id}

# ---- Download endpoints -----------------------------------------------------

@router.get("/download/{video_id}/transcript")
async def download_transcript(video_id: str, request: Request, db: Session = Depends(get_db)):
    """Download transcript file for a completed batch item."""
    user = _auth_user(request, db)
    
    transcript_file = DOWNLOADS_DIR / f"{video_id}_transcript.txt"
    if not transcript_file.exists():
        raise HTTPException(404, "Transcript not found")
    
    from fastapi.responses import FileResponse
    return FileResponse(
        path=str(transcript_file),
        media_type="text/plain",
        filename=f"{video_id}_transcript.txt"
    )

@router.get("/view/{video_id}/transcript")
async def view_transcript(video_id: str, request: Request, db: Session = Depends(get_db)):
    """View transcript content directly."""
    user = _auth_user(request, db)
    
    transcript_file = DOWNLOADS_DIR / f"{video_id}_transcript.txt"
    if not transcript_file.exists():
        raise HTTPException(404, "Transcript not found")
    
    content = transcript_file.read_text(encoding='utf-8')
    return {"video_id": video_id, "transcript": content}

# ============= End batch Module =============

