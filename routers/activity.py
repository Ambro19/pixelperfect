# =================================================================================================
# backend/routers/activity.py
# PixelPerfect Screenshot Activity Tracking
# Converted from YCD activity.py - maintains same robustness and professional architecture
# - Real-time activity updates
# - Screenshot history tracking
# - Activity summaries and statistics
# - Professional categorization and icons
# =================================================================================================

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import logging

from models import get_db, User, Screenshot
from auth_deps import get_current_user

log = logging.getLogger("activity")
router = APIRouter(prefix="/api/v1/user", tags=["activity"])

@router.get("/recent-activity")
async def get_recent_activity(
    limit: int = 15,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get recent user activity from screenshot records
    
    Returns the latest screenshots with rich metadata for activity feed display.
    """
    try:
        # Get recent screenshots for this user
        recent_screenshots = db.query(Screenshot)\
            .filter(Screenshot.user_id == current_user.id)\
            .filter(Screenshot.status == 'completed')\
            .order_by(desc(Screenshot.created_at))\
            .limit(limit)\
            .all()
        
        activities = []
        for screenshot in recent_screenshots:
            # Determine screenshot type and icon
            if screenshot.full_page:
                action = "Captured Full-Page Screenshot"
                icon = "📜"
                screenshot_type = "full_page"
            else:
                action = "Captured Screenshot"
                icon = "📸"
                screenshot_type = "standard"
            
            # Build description with key details
            description = f"Screenshot of {screenshot.url}"
            
            # Add dimensions
            if screenshot.width and screenshot.height:
                description += f" ({screenshot.width}x{screenshot.height})"
            
            # Add format
            if screenshot.format:
                description += f" in {screenshot.format.upper()}"
            
            # Add file size if available
            if screenshot.size_bytes:
                size_mb = screenshot.size_bytes / (1024 * 1024)
                if size_mb >= 1:
                    description += f" - {size_mb:.1f}MB"
                else:
                    size_kb = screenshot.size_bytes / 1024
                    description += f" - {size_kb:.1f}KB"
            
            # Additional features
            features = []
            if screenshot.dark_mode:
                features.append("dark mode")
            if screenshot.delay_seconds and screenshot.delay_seconds > 0:
                features.append(f"{screenshot.delay_seconds}s delay")
            if screenshot.removed_elements:
                features.append("elements removed")
            
            if features:
                description += f" with {', '.join(features)}"
            
            activity = {
                "id": screenshot.id,
                "action": action,
                "description": description,
                "timestamp": screenshot.created_at.isoformat(),
                "icon": icon,
                "type": screenshot_type,
                "url": screenshot.url,
                "screenshot_url": screenshot.storage_url,
                "width": screenshot.width,
                "height": screenshot.height,
                "format": screenshot.format,
                "file_size": screenshot.size_bytes,
                "quality": screenshot.quality,
                "full_page": screenshot.full_page,
                "dark_mode": screenshot.dark_mode,
                "delay_seconds": screenshot.delay_seconds,
                "status": screenshot.status,
                "processing_time_ms": screenshot.processing_time_ms,
                "category": "screenshot"
            }
            activities.append(activity)
        
        return {
            "activities": activities,
            "total": len(activities),
            "user_id": current_user.id,
            "fetched_at": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        log.error(f"Error fetching recent activity for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch recent activity")

@router.get("/screenshot-history")
async def get_screenshot_history(
    limit: int = 50,
    offset: int = 0,
    format_filter: Optional[str] = None,
    full_page_only: Optional[bool] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get comprehensive screenshot history with filtering and pagination
    
    Supports filtering by format, full_page mode, and includes detailed metadata.
    """
    try:
        # Build query with optional filters
        query = db.query(Screenshot)\
            .filter(Screenshot.user_id == current_user.id)
        
        if format_filter:
            query = query.filter(Screenshot.format == format_filter.lower())
        
        if full_page_only is not None:
            query = query.filter(Screenshot.full_page == full_page_only)
        
        # Get paginated results
        screenshots = query\
            .order_by(desc(Screenshot.created_at))\
            .offset(offset)\
            .limit(limit)\
            .all()
        
        screenshot_list = []
        for screenshot in screenshots:
            screenshot_data = {
                "id": screenshot.id,
                "url": screenshot.url,
                "screenshot_url": screenshot.storage_url,
                "width": screenshot.width,
                "height": screenshot.height,
                "format": screenshot.format,
                "quality": screenshot.quality,
                "full_page": screenshot.full_page,
                "dark_mode": screenshot.dark_mode,
                "delay_seconds": screenshot.delay_seconds,
                "removed_elements": screenshot.removed_elements,
                "file_size": screenshot.size_bytes,
                "file_size_mb": round(screenshot.size_bytes / (1024 * 1024), 2) if screenshot.size_bytes else None,
                "processing_time_ms": screenshot.processing_time_ms,
                "status": screenshot.status,
                "error_message": screenshot.error_message,
                "created_at": screenshot.created_at.isoformat(),
                "file_name": f"screenshot_{screenshot.id}.{screenshot.format or 'png'}"
            }
            screenshot_list.append(screenshot_data)
        
        # Get total count for pagination
        total_count = db.query(Screenshot)\
            .filter(Screenshot.user_id == current_user.id)\
            .count()
        
        return {
            "screenshots": screenshot_list,
            "total": total_count,
            "limit": limit,
            "offset": offset,
            "filters": {
                "format": format_filter,
                "full_page_only": full_page_only
            },
            "user_id": current_user.id,
            "fetched_at": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        log.error(f"Error fetching screenshot history for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch screenshot history")

@router.get("/activity-summary")
async def get_activity_summary(
    days: int = 30,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get activity summary statistics
    
    Provides comprehensive statistics about screenshot usage over a specified period.
    """
    try:
        # Calculate date range
        since_date = datetime.utcnow() - timedelta(days=days)
        
        # Get screenshots in date range
        screenshots = db.query(Screenshot)\
            .filter(Screenshot.user_id == current_user.id)\
            .filter(Screenshot.created_at >= since_date)\
            .filter(Screenshot.status == 'completed')\
            .all()
        
        # Calculate comprehensive stats
        stats = {
            "total_screenshots": len(screenshots),
            "standard_screenshots": 0,
            "full_page_screenshots": 0,
            "formats": {
                "png": 0,
                "jpeg": 0,
                "webp": 0,
                "pdf": 0
            },
            "total_size_bytes": 0,
            "total_size_mb": 0,
            "average_processing_time_ms": 0,
            "dark_mode_count": 0,
            "with_delay_count": 0,
            "unique_domains": set(),
            "period_days": days,
            "from_date": since_date.isoformat(),
            "to_date": datetime.utcnow().isoformat()
        }
        
        processing_times = []
        
        for screenshot in screenshots:
            # Count screenshot types
            if screenshot.full_page:
                stats["full_page_screenshots"] += 1
            else:
                stats["standard_screenshots"] += 1
            
            # Count formats
            format_key = (screenshot.format or 'png').lower()
            if format_key in stats["formats"]:
                stats["formats"][format_key] += 1
            
            # Sum file sizes
            if screenshot.size_bytes:
                stats["total_size_bytes"] += screenshot.size_bytes
            
            # Track processing times
            if screenshot.processing_time_ms:
                processing_times.append(screenshot.processing_time_ms)
            
            # Count features
            if screenshot.dark_mode:
                stats["dark_mode_count"] += 1
            
            if screenshot.delay_seconds and screenshot.delay_seconds > 0:
                stats["with_delay_count"] += 1
            
            # Track unique domains
            if screenshot.url:
                try:
                    from urllib.parse import urlparse
                    domain = urlparse(screenshot.url).netloc
                    if domain:
                        stats["unique_domains"].add(domain)
                except Exception:
                    pass
        
        # Calculate averages
        stats["total_size_mb"] = round(stats["total_size_bytes"] / (1024 * 1024), 2)
        
        if processing_times:
            stats["average_processing_time_ms"] = round(sum(processing_times) / len(processing_times), 2)
            stats["fastest_screenshot_ms"] = round(min(processing_times), 2)
            stats["slowest_screenshot_ms"] = round(max(processing_times), 2)
        
        stats["unique_domains"] = len(stats["unique_domains"])
        
        # Add usage percentage if user has limits
        tier_limits = None
        try:
            from models import get_tier_limits
            tier_limits = get_tier_limits(current_user.subscription_tier or "free")
            if tier_limits:
                monthly_limit = tier_limits.get("screenshots", 0)
                current_usage = getattr(current_user, "usage_screenshots", 0) or 0
                
                if monthly_limit != float("inf"):
                    stats["monthly_usage"] = {
                        "current": current_usage,
                        "limit": monthly_limit,
                        "remaining": max(0, monthly_limit - current_usage),
                        "percentage": round((current_usage / monthly_limit) * 100, 1) if monthly_limit > 0 else 0
                    }
                else:
                    stats["monthly_usage"] = {
                        "current": current_usage,
                        "limit": "unlimited",
                        "remaining": "unlimited",
                        "percentage": 0
                    }
        except Exception as e:
            log.warning(f"Could not calculate usage stats: {e}")
        
        return stats
        
    except Exception as e:
        log.error(f"Error fetching activity summary for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch activity summary")

@router.get("/popular-domains")
async def get_popular_domains(
    limit: int = 10,
    days: int = 30,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get most frequently screenshotted domains
    
    Shows which websites the user screenshots most often.
    """
    try:
        since_date = datetime.utcnow() - timedelta(days=days)
        
        screenshots = db.query(Screenshot)\
            .filter(Screenshot.user_id == current_user.id)\
            .filter(Screenshot.created_at >= since_date)\
            .filter(Screenshot.status == 'completed')\
            .all()
        
        domain_counts = {}
        
        for screenshot in screenshots:
            if screenshot.url:
                try:
                    from urllib.parse import urlparse
                    domain = urlparse(screenshot.url).netloc
                    if domain:
                        domain_counts[domain] = domain_counts.get(domain, 0) + 1
                except Exception:
                    pass
        
        # Sort by count and get top N
        popular = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
        
        return {
            "domains": [
                {
                    "domain": domain,
                    "count": count,
                    "percentage": round((count / len(screenshots)) * 100, 1) if screenshots else 0
                }
                for domain, count in popular
            ],
            "total_domains": len(domain_counts),
            "period_days": days,
            "total_screenshots": len(screenshots)
        }
        
    except Exception as e:
        log.error(f"Error fetching popular domains for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch popular domains")

# Helper function to create activity record when screenshots are created
def create_activity_record(
    db: Session, 
    user: User, 
    url: str,
    width: int = 1920,
    height: int = 1080,
    format: str = "png",
    full_page: bool = False,
    file_size: int = None,
    processing_time_ms: float = None,
    status: str = "completed",
    screenshot_id: str = None,
    storage_url: str = None
):
    """
    Helper function to create activity records when screenshots are captured
    
    This is automatically called by the screenshot router but can also be used
    for manual activity logging.
    """
    try:
        record = Screenshot(
            id=screenshot_id or str(uuid.uuid4()), # pyright: ignore[reportUndefinedVariable]
            user_id=user.id,
            url=url,
            width=width,
            height=height,
            format=format,
            full_page=full_page,
            size_bytes=file_size,
            processing_time_ms=processing_time_ms,
            status=status,
            storage_url=storage_url,
            created_at=datetime.utcnow()
        )
        
        db.add(record)
        db.commit()
        db.refresh(record)
        
        log.info(f"Created activity record for user {user.id}, screenshot {screenshot_id}")
        return record
        
    except Exception as e:
        log.error(f"Failed to create activity record: {e}", exc_info=True)
        db.rollback()
        return None

# ============= End Activity Module =============