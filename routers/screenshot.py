# backend/routers/screenshot.py
# PixelPerfect Screenshot API Router - Production Ready
# Fixed imports and error handling

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, HttpUrl, Field
from typing import Optional, List
from datetime import datetime
import uuid
import httpx
import logging

# Correct imports
from auth_deps import get_current_user
from models import User, get_db, Screenshot, get_tier_limits
from services.screenshot_service import screenshot_service
from services.storage_service import storage_service

logger = logging.getLogger("pixelperfect")

router = APIRouter(prefix="/api/v1/screenshot", tags=["Screenshot"])

# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class ScreenshotRequest(BaseModel):
    """Screenshot request model"""
    url: HttpUrl
    width: int = Field(default=1920, ge=320, le=3840)
    height: int = Field(default=1080, ge=240, le=2160)
    full_page: bool = False
    format: str = Field(default="png", pattern="^(png|jpeg|webp)$")
    quality: Optional[int] = Field(default=None, ge=0, le=100)
    delay: int = Field(default=0, ge=0, le=10)
    dark_mode: bool = False
    remove_elements: Optional[List[str]] = None
    return_url: bool = True


class ScreenshotResponse(BaseModel):
    """Screenshot response model"""
    url: str
    screenshot_url: Optional[str] = None
    screenshot_id: str
    width: int
    height: int
    format: str
    size_bytes: int
    created_at: str
    usage: dict


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def check_user_screenshot_limit(user: User) -> tuple[bool, int, int]:
    """
    Check if user can create a screenshot
    
    Returns:
        (can_use, current_usage, limit)
    """
    tier_limits = get_tier_limits(user.subscription_tier or "free")
    current = user.usage_screenshots or 0
    limit = tier_limits["screenshots"]
    
    return (current < limit, current, limit)


def increment_user_usage(user: User, db, usage_type: str = "screenshots"):
    """Increment usage counter"""
    if usage_type == "screenshots":
        user.usage_screenshots = (user.usage_screenshots or 0) + 1
    elif usage_type == "batch_requests":
        user.usage_batch_requests = (user.usage_batch_requests or 0) + 1
    
    user.usage_api_calls = (user.usage_api_calls or 0) + 1
    db.commit()


# ============================================================================
# SCREENSHOT ENDPOINTS
# ============================================================================

@router.post("/", response_model=ScreenshotResponse)
async def create_screenshot(
    request: ScreenshotRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db = Depends(get_db)
):
    """
    Create a screenshot
    
    ## Parameters
    - **url**: Website URL to screenshot
    - **width**: Viewport width (320-3840px)
    - **height**: Viewport height (240-2160px)
    - **full_page**: Capture full page or viewport only
    - **format**: Image format (png, jpeg, webp)
    - **quality**: JPEG quality 0-100 (optional)
    - **delay**: Delay before screenshot in seconds (0-10)
    - **dark_mode**: Enable dark mode
    - **remove_elements**: CSS selectors to remove (e.g., cookie banners)
    - **return_url**: Return URL or base64 image
    
    ## Returns
    Screenshot metadata and URL
    
    ## Example
    ```bash
    curl -X POST "https://api.pixelperfectapi.net/api/v1/screenshot/" \\
      -H "Authorization: Bearer YOUR_TOKEN" \\
      -H "Content-Type: application/json" \\
      -d '{
        "url": "https://example.com",
        "width": 1920,
        "height": 1080,
        "format": "png"
      }'
    ```
    """
    
    # Check usage limits
    can_use, current, limit = check_user_screenshot_limit(current_user)
    if not can_use:
        raise HTTPException(
            status_code=429,
            detail=f"Screenshot limit reached ({current}/{limit}). Please upgrade your plan."
        )
    
    # Validate tier-specific limits
    tier_limits = get_tier_limits(current_user.subscription_tier or "free")
    
    if request.width > tier_limits.get("max_width", 1920):
        raise HTTPException(
            status_code=400,
            detail=f"Width exceeds tier limit ({tier_limits.get('max_width', 1920)}px). Please upgrade."
        )
    
    if request.format not in tier_limits.get("formats", ["png", "jpeg"]):
        raise HTTPException(
            status_code=400,
            detail=f"Format '{request.format}' not available in your tier. Please upgrade."
        )
    
    try:
        # Initialize screenshot service if needed
        if not screenshot_service.browser:
            logger.info("🔧 Initializing Playwright browser...")
            await screenshot_service.initialize()
        
        # Capture screenshot
        start_time = datetime.utcnow()
        
        screenshot_bytes = await screenshot_service.capture_screenshot(
            url=str(request.url),
            width=request.width,
            height=request.height,
            full_page=request.full_page,
            format=request.format,
            quality=request.quality,
            delay=request.delay,
            dark_mode=request.dark_mode,
            remove_elements=request.remove_elements
        )
        
        processing_time = (datetime.utcnow() - start_time).total_seconds() * 1000  # ms
        
        # Generate ID and filename
        screenshot_id = str(uuid.uuid4())
        filename = f"screenshots/{current_user.id}/{screenshot_id}.{request.format}"
        
        # Save to storage (R2/S3 or local)
        try:
            screenshot_url = await storage_service.upload_screenshot(
                file_data=screenshot_bytes,
                filename=filename,
                content_type=f"image/{request.format}"
            )
            storage_key = filename
        except Exception as e:
            # Fallback to local storage if R2 fails
            logger.warning(f"R2 upload failed, using local storage: {e}")
            import os
            from pathlib import Path
            
            local_dir = Path("screenshots") / str(current_user.id)
            local_dir.mkdir(parents=True, exist_ok=True)
            
            local_path = local_dir / f"{screenshot_id}.{request.format}"
            local_path.write_bytes(screenshot_bytes)
            
            screenshot_url = f"/screenshots/{current_user.id}/{screenshot_id}.{request.format}"
            storage_key = str(local_path)
        
        # Calculate expiry based on tier
        retention_days = tier_limits.get("screenshot_retention_days", 7)
        from datetime import timedelta
        expires_at = datetime.utcnow() + timedelta(days=retention_days)
        
        # Save to database
        screenshot_record = Screenshot(
            id=screenshot_id,
            user_id=current_user.id,
            url=str(request.url),
            width=request.width,
            height=request.height,
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
            created_at=datetime.utcnow()
        )
        db.add(screenshot_record)
        
        # Increment usage
        increment_user_usage(current_user, db, "screenshots")
        
        db.commit()
        db.refresh(screenshot_record)
        db.refresh(current_user)
        
        logger.info(f"✅ Screenshot created: {screenshot_id} for user {current_user.id}")
        
        return ScreenshotResponse(
            url=str(request.url),
            screenshot_url=screenshot_url if request.return_url else None,
            screenshot_id=screenshot_id,
            width=request.width,
            height=request.height,
            format=request.format,
            size_bytes=len(screenshot_bytes),
            created_at=screenshot_record.created_at.isoformat(),
            usage={
                "current": current_user.usage_screenshots,
                "limit": limit,
                "remaining": limit - current_user.usage_screenshots
            }
        )
        
    except httpx.HTTPError as e:
        logger.error(f"HTTP error loading URL {request.url}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to load URL: {str(e)}")
    except Exception as e:
        logger.error(f"Screenshot failed: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Screenshot failed: {str(e)}")


@router.get("/{screenshot_id}")
async def get_screenshot(
    screenshot_id: str,
    current_user: User = Depends(get_current_user),
    db = Depends(get_db)
):
    """Get screenshot details by ID"""
    screenshot = db.query(Screenshot).filter(
        Screenshot.id == screenshot_id,
        Screenshot.user_id == current_user.id
    ).first()
    
    if not screenshot:
        raise HTTPException(status_code=404, detail="Screenshot not found")
    
    return {
        "id": screenshot.id,
        "url": screenshot.url,
        "screenshot_url": screenshot.storage_url,
        "width": screenshot.width,
        "height": screenshot.height,
        "format": screenshot.format,
        "size_bytes": screenshot.size_bytes,
        "status": screenshot.status,
        "processing_time_ms": screenshot.processing_time_ms,
        "created_at": screenshot.created_at.isoformat(),
        "expires_at": screenshot.expires_at.isoformat() if screenshot.expires_at else None
    }


@router.get("/")
async def list_screenshots(
    limit: int = 20,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db = Depends(get_db)
):
    """List user's screenshots"""
    screenshots = db.query(Screenshot).filter(
        Screenshot.user_id == current_user.id
    ).order_by(Screenshot.created_at.desc()).limit(limit).offset(offset).all()
    
    total = db.query(Screenshot).filter(Screenshot.user_id == current_user.id).count()
    
    return {
        "screenshots": [
            {
                "id": s.id,
                "url": s.url,
                "screenshot_url": s.storage_url,
                "width": s.width,
                "height": s.height,
                "format": s.format,
                "size_bytes": s.size_bytes,
                "status": s.status,
                "created_at": s.created_at.isoformat()
            }
            for s in screenshots
        ],
        "total": total,
        "limit": limit,
        "offset": offset
    }


@router.delete("/{screenshot_id}")
async def delete_screenshot(
    screenshot_id: str,
    current_user: User = Depends(get_current_user),
    db = Depends(get_db)
):
    """Delete a screenshot"""
    screenshot = db.query(Screenshot).filter(
        Screenshot.id == screenshot_id,
        Screenshot.user_id == current_user.id
    ).first()
    
    if not screenshot:
        raise HTTPException(status_code=404, detail="Screenshot not found")
    
    # Try to delete from storage
    try:
        if screenshot.storage_key:
            await storage_service.delete_screenshot(screenshot.storage_key)
    except Exception as e:
        logger.warning(f"Failed to delete screenshot from storage: {e}")
    
    # Delete from database
    db.delete(screenshot)
    db.commit()
    
    logger.info(f"🗑️ Screenshot deleted: {screenshot_id}")
    
    return {"status": "deleted", "screenshot_id": screenshot_id}


@router.get("/stats/usage")
async def get_usage_stats(
    current_user: User = Depends(get_current_user),
    db = Depends(get_db)
):
    """Get detailed usage statistics"""
    tier_limits = get_tier_limits(current_user.subscription_tier or "free")
    
    return {
        "tier": current_user.subscription_tier or "free",
        "usage": {
            "screenshots": {
                "used": current_user.usage_screenshots or 0,
                "limit": tier_limits["screenshots"],
                "remaining": tier_limits["screenshots"] - (current_user.usage_screenshots or 0),
                "percentage": round(((current_user.usage_screenshots or 0) / tier_limits["screenshots"]) * 100, 1)
            },
            "batch_requests": {
                "used": current_user.usage_batch_requests or 0,
                "limit": tier_limits["batch_requests"],
                "remaining": tier_limits["batch_requests"] - (current_user.usage_batch_requests or 0)
            },
            "api_calls": {
                "used": current_user.usage_api_calls or 0
            }
        },
        "limits": tier_limits,
        "reset_date": current_user.usage_reset_at.isoformat() if current_user.usage_reset_at else None
    }
























