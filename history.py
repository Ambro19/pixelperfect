# ============================================================================
# HISTORY + ACTIVITY API - PixelPerfect Screenshot API
# File: backend/history.py
# Provides endpoints the frontend expects:
# ✅ /api/v1/user/screenshot-history
# ✅ /api/v1/user/recent-activity
# ✅ /api/v1/user/activity-summary
# ============================================================================

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from auth_deps import get_current_user
from models import Screenshot, User, get_db

log = logging.getLogger("pixelperfect.history")

router = APIRouter(prefix="/api/v1/user", tags=["history"])

def _activity_icon(fmt: str) -> str:
    f = (fmt or "").lower()
    if f == "png":
        return "🖼️"
    if f in ("jpeg", "jpg"):
        return "📷"
    if f == "webp":
        return "🎨"
    if f == "pdf":
        return "📄"
    return "📸"

def _activity_action(fmt: str) -> str:
    f = (fmt or "").lower()
    if f == "png":
        return "PNG Screenshot"
    if f in ("jpeg", "jpg"):
        return "JPEG Screenshot"
    if f == "webp":
        return "WebP Screenshot"
    if f == "pdf":
        return "PDF Screenshot"
    return "Screenshot Captured"

@router.get("/screenshot-history")
def screenshot_history(
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    try:
        q = (
            db.query(Screenshot)
            .filter(Screenshot.user_id == current_user.id)
            .order_by(desc(Screenshot.created_at))
        )

        total = q.count()
        rows = q.offset(offset).limit(limit).all()

        screenshots = []
        for s in rows:
            screenshots.append(
                {
                    "id": str(s.id),
                    "url": s.url,
                    "width": s.width,
                    "height": s.height,
                    "format": (s.format or "png").lower(),
                    "full_page": bool(s.full_page) if s.full_page is not None else False,
                    "dark_mode": bool(s.dark_mode) if s.dark_mode is not None else False,
                    "size_bytes": int(s.size_bytes or 0),
                    "status": s.status or "completed",
                    "created_at": (s.created_at or datetime.utcnow()).isoformat(),
                    "processing_time": (float(s.processing_time_ms) / 1000.0) if s.processing_time_ms else None,
                    "error_message": s.error_message,
                    # prefer storage_url (your capture endpoint sets it)
                    "screenshot_url": s.storage_url or None,
                }
            )

        return {
            "screenshots": screenshots,
            "total": total,
            "limit": limit,
            "offset": offset,
            "user_id": current_user.id,
            "fetched_at": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        log.exception("History error for user %s", current_user.id)
        raise HTTPException(status_code=500, detail="Failed to fetch screenshot history") from e

@router.get("/recent-activity")
def recent_activity(
    limit: int = 15,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    try:
        rows = (
            db.query(Screenshot)
            .filter(Screenshot.user_id == current_user.id)
            .order_by(desc(Screenshot.created_at))
            .limit(limit)
            .all()
        )

        activities = []
        for s in rows:
            fmt = (s.format or "png").lower()
            activities.append(
                {
                    "id": str(s.id),
                    "action": _activity_action(fmt),
                    "description": f"{_activity_action(fmt)} of {s.url}",
                    "timestamp": (s.created_at or datetime.utcnow()).isoformat(),
                    "icon": _activity_icon(fmt),
                    "category": fmt,
                    "type": "screenshot",
                    "url": s.url,
                    "width": s.width,
                    "height": s.height,
                    "format": fmt,
                    "size_bytes": int(s.size_bytes or 0),
                    "full_page": bool(s.full_page) if s.full_page is not None else False,
                    "dark_mode": bool(s.dark_mode) if s.dark_mode is not None else False,
                    "status": s.status or "completed",
                    "processing_time": (float(s.processing_time_ms) / 1000.0) if s.processing_time_ms else None,
                    "screenshot_url": s.storage_url or None,
                    "error_message": s.error_message,
                }
            )

        return {
            "activities": activities,
            "total": len(activities),
            "user_id": current_user.id,
            "fetched_at": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        log.exception("Recent activity error for user %s", current_user.id)
        raise HTTPException(status_code=500, detail="Failed to fetch recent activity") from e

@router.get("/activity-summary")
def activity_summary(
    days: int = 30,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    try:
        since = datetime.utcnow() - timedelta(days=days)
        rows = (
            db.query(Screenshot)
            .filter(Screenshot.user_id == current_user.id)
            .filter(Screenshot.created_at >= since)
            .all()
        )

        stats = {"total": 0, "png": 0, "jpeg": 0, "webp": 0, "pdf": 0, "other": 0}
        for s in rows:
            stats["total"] += 1
            fmt = (s.format or "").lower()
            if fmt == "png":
                stats["png"] += 1
            elif fmt in ("jpeg", "jpg"):
                stats["jpeg"] += 1
            elif fmt == "webp":
                stats["webp"] += 1
            elif fmt == "pdf":
                stats["pdf"] += 1
            else:
                stats["other"] += 1

        stats.update(
            {
                "period_days": days,
                "from_date": since.isoformat(),
                "to_date": datetime.utcnow().isoformat(),
            }
        )
        return stats

    except Exception as e:
        log.exception("Summary error for user %s", current_user.id)
        raise HTTPException(status_code=500, detail="Failed to fetch activity summary") from e
