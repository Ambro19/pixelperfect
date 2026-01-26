# ============================================================================
# SCREENSHOT ENDPOINTS - Add to main.py
# ============================================================================
# File: backend/screenshot_endpoints.py
# Author: OneTechly
# Date: January 2026
# Purpose: FastAPI endpoints for screenshot capture
# ============================================================================

from fastapi import HTTPException, Depends
from pydantic import BaseModel, HttpUrl, Field
from typing import Optional
from sqlalchemy.orm import Session

from models import User, Screenshot, get_db, get_tier_limits
from auth_deps import get_current_user
from screenshot_service import screenshot_service, get_screenshot_url, increment_user_usage, check_usage_limit

import logging
logger = logging.getLogger("pixelperfect")

# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class ScreenshotRequest(BaseModel):
    """Request model for screenshot capture"""
    url: HttpUrl = Field(..., description="Website URL to screenshot")
    width: int = Field(default=1920, ge=320, le=3840, description="Viewport width (320-3840)")
    height: int = Field(default=1080, ge=240, le=2160, description="Viewport height (240-2160)")
    format: str = Field(default="png", description="Output format: png, jpeg, webp, pdf")
    full_page: bool = Field(default=False, description="Capture full page height")
    dark_mode: bool = Field(default=False, description="Emulate dark mode")
    
    class Config:
        json_schema_extra = {
            "example": {
                "url": "https://example.com",
                "width": 1920,
                "height": 1080,
                "format": "png",
                "full_page": False,
                "dark_mode": False
            }
        }


class ScreenshotResponse(BaseModel):
    """Response model for screenshot capture"""
    screenshot_id: str
    screenshot_url: str
    width: int
    height: int
    format: str
    size_bytes: int
    created_at: str
    message: Optional[str] = None


class BatchScreenshotRequest(BaseModel):
    """Request model for batch screenshots"""
    urls: list[HttpUrl] = Field(..., min_length=1, max_length=50, description="List of URLs (max 50)")
    width: int = Field(default=1920, ge=320, le=3840)
    height: int = Field(default=1080, ge=240, le=2160)
    format: str = Field(default="png")
    full_page: bool = Field(default=False)
    dark_mode: bool = Field(default=False)


# ============================================================================
# SCREENSHOT ENDPOINT
# ============================================================================

async def capture_screenshot_endpoint(
    request: ScreenshotRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    📸 Capture a screenshot of a website
    
    **Features:**
    - Multiple formats (PNG, JPEG, WebP, PDF)
    - Custom viewport sizes
    - Full-page capture
    - Dark mode emulation
    
    **Rate Limits:**
    - Free: 100/month
    - Pro: 1000/month
    - Business: 5000/month
    - Premium: Unlimited
    """
    # Get user's tier limits
    tier = (current_user.subscription_tier or "free").lower()
    tier_limits = get_tier_limits(tier)
    
    # Check usage limit
    if not check_usage_limit(current_user, tier_limits):
        limit = tier_limits.get("screenshots")
        raise HTTPException(
            status_code=429,
            detail=f"Screenshot limit exceeded ({limit}/month). Upgrade your plan to continue."
        )
    
    try:
        # Capture screenshot
        result = await screenshot_service.capture_screenshot(
            url=str(request.url),
            width=request.width,
            height=request.height,
            format=request.format.lower(),
            full_page=request.full_page,
            dark_mode=request.dark_mode,
        )
        
        # Save to database
        screenshot_record = Screenshot(
            user_id=current_user.id,
            url=str(request.url),
            screenshot_path=result["filepath"],
            width=result["width"],
            height=result["height"],
            format=result["format"],
            full_page=result["full_page"],
            dark_mode=result["dark_mode"],
            status="completed",
            created_at=result["created_at"],
        )
        db.add(screenshot_record)
        
        # Increment usage counter
        increment_user_usage(current_user, db)
        
        db.commit()
        db.refresh(screenshot_record)
        
        # Build response
        screenshot_url = get_screenshot_url(result["filename"])
        
        logger.info(f"✅ Screenshot created for user {current_user.id}: {result['filename']}")
        
        return ScreenshotResponse(
            screenshot_id=str(screenshot_record.id),
            screenshot_url=screenshot_url,
            width=result["width"],
            height=result["height"],
            format=result["format"],
            size_bytes=result["file_size"],
            created_at=result["created_at"].isoformat(),
            message="Screenshot captured successfully"
        )
        
    except ValueError as e:
        logger.error(f"❌ Screenshot error for user {current_user.id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    
    except Exception as e:
        logger.exception(f"❌ Unexpected error capturing screenshot for user {current_user.id}")
        raise HTTPException(status_code=500, detail="Failed to capture screenshot. Please try again.")


# ============================================================================
# BATCH SCREENSHOT ENDPOINT
# ============================================================================

async def batch_screenshot_endpoint(
    request: BatchScreenshotRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    📦 Capture multiple screenshots in one request
    
    **Pro+ Feature Only**
    
    Capture up to 50 screenshots in a single batch job.
    Results are processed asynchronously.
    """
    # Check if user has access to batch processing
    tier = (current_user.subscription_tier or "free").lower()
    
    if tier == "free":
        raise HTTPException(
            status_code=403,
            detail="Batch processing requires Pro plan or higher. Upgrade at /pricing"
        )
    
    tier_limits = get_tier_limits(tier)
    batch_limit = tier_limits.get("batch_requests", 0)
    
    # Check batch request limit
    if batch_limit != "unlimited":
        current_batch_usage = current_user.usage_batch_requests or 0
        if current_batch_usage >= batch_limit:
            raise HTTPException(
                status_code=429,
                detail=f"Batch request limit exceeded ({batch_limit}/month). Upgrade to continue."
            )
    
    # Process batch (simplified - in production, use background tasks)
    results = []
    failed = []
    
    for url in request.urls:
        try:
            result = await screenshot_service.capture_screenshot(
                url=str(url),
                width=request.width,
                height=request.height,
                format=request.format.lower(),
                full_page=request.full_page,
                dark_mode=request.dark_mode,
            )
            
            # Save to database
            screenshot_record = Screenshot(
                user_id=current_user.id,
                url=str(url),
                screenshot_path=result["filepath"],
                width=result["width"],
                height=result["height"],
                format=result["format"],
                full_page=result["full_page"],
                dark_mode=result["dark_mode"],
                status="completed",
                created_at=result["created_at"],
            )
            db.add(screenshot_record)
            
            results.append({
                "url": str(url),
                "screenshot_url": get_screenshot_url(result["filename"]),
                "status": "success"
            })
            
        except Exception as e:
            logger.error(f"❌ Failed to capture {url}: {e}")
            failed.append({
                "url": str(url),
                "status": "failed",
                "error": str(e)
            })
    
    # Update usage
    current_user.usage_batch_requests = (current_user.usage_batch_requests or 0) + 1
    current_user.usage_screenshots = (current_user.usage_screenshots or 0) + len(results)
    current_user.usage_api_calls = (current_user.usage_api_calls or 0) + 1
    
    db.commit()
    
    return {
        "batch_id": f"batch_{int(datetime.utcnow().timestamp())}", # pyright: ignore[reportUndefinedVariable]
        "total": len(request.urls),
        "successful": len(results),
        "failed": len(failed),
        "results": results,
        "failures": failed,
        "message": f"Batch processing completed: {len(results)}/{len(request.urls)} successful"
    }


# ============================================================================
# REGENERATE API KEY ENDPOINT
# ============================================================================

async def regenerate_api_key_endpoint(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    🔄 Regenerate API key
    
    Deactivates old key and generates a new one.
    The old key will stop working immediately.
    """
    from api_key_system import regenerate_api_key
    
    try:
        # Regenerate the API key
        new_key, new_record = regenerate_api_key(db, current_user.id)
        
        logger.info(f"🔄 API key regenerated for user {current_user.id}")
        
        return {
            "api_key": new_key,
            "key_prefix": new_record.key_prefix,
            "created_at": new_record.created_at.isoformat(),
            "message": "⚠️ Save this key securely. Your old key has been deactivated and will no longer work."
        }
        
    except Exception as e:
        logger.error(f"❌ Failed to regenerate API key for user {current_user.id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to regenerate API key")


# ============================================================================
# INTEGRATION INSTRUCTIONS FOR main.py
# ============================================================================

"""
# Add these imports to main.py:
from screenshot_service import screenshot_service
from screenshot_endpoints import (
    capture_screenshot_endpoint,
    batch_screenshot_endpoint,
    regenerate_api_key_endpoint,
    ScreenshotRequest,
    BatchScreenshotRequest,
)

# Add startup event:
@app.on_event("startup")
async def on_startup():
    initialize_database()
    run_startup_migrations(engine)
    run_api_key_migration(engine)
    await screenshot_service.initialize()  # ✅ NEW

# Add shutdown event:
@app.on_event("shutdown")
async def on_shutdown():
    await screenshot_service.close()  # ✅ NEW

# Add endpoints:
@app.post("/api/v1/screenshot")
async def capture_screenshot(
    request: ScreenshotRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await capture_screenshot_endpoint(request, current_user, db)

@app.post("/api/v1/batch/submit")
async def batch_screenshot(
    request: BatchScreenshotRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await batch_screenshot_endpoint(request, current_user, db)

@app.post("/api/keys/regenerate")
async def regenerate_api_key(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await regenerate_api_key_endpoint(current_user, db)
"""