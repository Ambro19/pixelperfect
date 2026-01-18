# backend/routers/screenshot.py
# PixelPerfect Screenshot API Router - COMPLETE WITH ALL FEATURES
# Implements: JS execution, device emulation, element selection, webhooks, PDF support
# Author: OneTechly
# Updated: January 2026 - Production-ready

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, HttpUrl, Field
from typing import Optional, List, Dict, Any
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
# PYDANTIC MODELS - ENHANCED WITH ALL FEATURES
# ============================================================================

class ScreenshotRequest(BaseModel):
    """Complete screenshot request model with ALL advertised features"""
    url: HttpUrl
    width: int = Field(default=1920, ge=320, le=3840)
    height: int = Field(default=1080, ge=240, le=2160)
    full_page: bool = False
    format: str = Field(default="png", pattern="^(png|jpeg|webp|pdf)$")
    quality: Optional[int] = Field(default=None, ge=0, le=100)
    delay: int = Field(default=0, ge=0, le=10)
    dark_mode: bool = False
    remove_elements: Optional[List[str]] = None
    return_url: bool = True
    
    # Advanced features
    device: Optional[str] = Field(default=None, description="Device preset: iphone_13, pixel_5, ipad_pro")
    custom_js: Optional[str] = Field(default=None, description="Custom JavaScript to execute", max_length=10000)
    wait_for_selector: Optional[str] = Field(default=None, description="CSS selector to wait for")
    target_element: Optional[str] = Field(default=None, description="Target specific element for cropping")
    webhook_url: Optional[str] = Field(default=None, description="Webhook URL for completion notification")


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
    device_used: Optional[str] = None


class DeviceListResponse(BaseModel):
    """Available devices response"""
    devices: List[str]
    descriptions: Dict[str, str]


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def check_user_screenshot_limit(user: User) -> tuple[bool, int, int]:
    """Check if user can create a screenshot"""
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


def check_feature_access(user: User, feature: str) -> bool:
    """Check if user has access to advanced feature"""
    tier_limits = get_tier_limits(user.subscription_tier or "free")
    tier = (user.subscription_tier or "free").lower()
    
    feature_access = {
        "custom_js": tier in ["pro", "business", "premium"],
        "device_emulation": tier in ["pro", "business", "premium"],
        "element_selection": tier in ["business", "premium"],
        "pdf": tier in ["business", "premium"],
        "webhooks": tier in ["business", "premium"]
    }
    
    return feature_access.get(feature, False)


async def send_webhook_notification(webhook_url: str, screenshot_data: Dict[str, Any]):
    """Send webhook notification (Business tier feature)"""
    if not webhook_url:
        return
    
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                webhook_url,
                json={
                    "event": "screenshot.completed",
                    "data": screenshot_data,
                    "timestamp": datetime.utcnow().isoformat()
                },
                timeout=10.0
            )
        logger.info(f"✅ Webhook notification sent to {webhook_url}")
    except Exception as e:
        logger.warning(f"⚠️ Webhook notification failed: {e}")


# ============================================================================
# SCREENSHOT ENDPOINTS - COMPLETE IMPLEMENTATION
# ============================================================================

@router.post("/", response_model=ScreenshotResponse)
async def create_screenshot(
    request: ScreenshotRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db = Depends(get_db)
):
    """
    Create a screenshot with COMPLETE feature set
    
    ## Core Features
    - **Lightning Fast**: Captures in < 3 seconds
    - **Full Customization**: Viewport, formats, quality
    - **Dark Mode**: Automatic or forced preference
    - **Full Page**: Capture entire page, no height limit
    
    ## Advanced Features (Pro/Business Tiers)
    - **Mobile Device Emulation**: iPhone, Android, iPad presets
    - **Custom JavaScript**: Execute code before capture
    - **Element Selection**: Target & crop specific elements
    - **PDF Generation**: Generate PDF documents (Business)
    - **Webhook Notifications**: Real-time completion alerts (Business)
    
    ## Parameters
    - **url**: Website URL to screenshot
    - **width**: Viewport width (320-3840px)
    - **height**: Viewport height (240-2160px)
    - **full_page**: Capture full page or viewport only
    - **format**: Image format (png, jpeg, webp, pdf)
    - **quality**: JPEG quality 0-100 (optional)
    - **delay**: Delay before screenshot (0-10s)
    - **dark_mode**: Enable dark mode
    - **remove_elements**: CSS selectors to remove
    - **device**: Device preset (iphone_13, pixel_5, ipad_pro)
    - **custom_js**: JavaScript to execute (Pro+)
    - **wait_for_selector**: Wait for element before capture
    - **target_element**: Crop to specific element (Business+)
    - **webhook_url**: Completion notification URL (Business+)
    
    ## Example
    ```bash
    curl -X POST "https://api.pixelperfectapi.net/api/v1/screenshot/" \\
      -H "Authorization: Bearer YOUR_TOKEN" \\
      -H "Content-Type: application/json" \\
      -d '{
        "url": "https://example.com",
        "device": "iphone_13",
        "dark_mode": true,
        "custom_js": "document.querySelector('.banner').remove();"
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
    
    # Validate tier-specific features
    tier_limits = get_tier_limits(current_user.subscription_tier or "free")
    
    # Check format access
    if request.format not in tier_limits.get("formats", ["png", "jpeg"]):
        raise HTTPException(
            status_code=403,
            detail=f"Format '{request.format}' not available in your tier. Please upgrade to Business."
        )
    
    # Check PDF access (Business only)
    if request.format == "pdf" and not check_feature_access(current_user, "pdf"):
        raise HTTPException(
            status_code=403,
            detail="PDF generation requires Business tier. Please upgrade."
        )
    
    # Check custom JavaScript access
    if request.custom_js and not check_feature_access(current_user, "custom_js"):
        raise HTTPException(
            status_code=403,
            detail="Custom JavaScript execution requires Pro tier or higher. Please upgrade."
        )
    
    # Check device emulation access
    if request.device and not check_feature_access(current_user, "device_emulation"):
        raise HTTPException(
            status_code=403,
            detail="Device emulation requires Pro tier or higher. Please upgrade."
        )
    
    # Check element selection access
    if request.target_element and not check_feature_access(current_user, "element_selection"):
        raise HTTPException(
            status_code=403,
            detail="Element selection requires Business tier. Please upgrade."
        )
    
    # Check webhook access
    if request.webhook_url and not check_feature_access(current_user, "webhooks"):
        raise HTTPException(
            status_code=403,
            detail="Webhook notifications require Business tier. Please upgrade."
        )
    
    # Check viewport limits
    if request.width > tier_limits.get("max_width", 1920):
        raise HTTPException(
            status_code=400,
            detail=f"Width exceeds tier limit ({tier_limits.get('max_width', 1920)}px). Please upgrade."
        )
    
    try:
        # Initialize screenshot service if needed
        if not screenshot_service.browser:
            logger.info("🔧 Initializing Playwright browser...")
            await screenshot_service.initialize()
        
        # Capture screenshot with ALL features
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
            remove_elements=request.remove_elements,
            device=request.device,
            custom_js=request.custom_js,
            wait_for_selector=request.wait_for_selector,
            target_element=request.target_element
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
                content_type=f"application/pdf" if request.format == "pdf" else f"image/{request.format}"
            )
            storage_key = filename
        except Exception as e:
            # Fallback to local storage if R2 fails
            logger.warning(f"R2 upload failed, using local storage: {e}")
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
            width=request.width if not request.device else None,
            height=request.height if not request.device else None,
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
        
        # Send webhook notification in background (Business tier)
        if request.webhook_url:
            background_tasks.add_task(
                send_webhook_notification,
                request.webhook_url,
                {
                    "screenshot_id": screenshot_id,
                    "url": str(request.url),
                    "screenshot_url": screenshot_url,
                    "format": request.format,
                    "size_bytes": len(screenshot_bytes),
                    "processing_time_ms": processing_time
                }
            )
        
        return ScreenshotResponse(
            url=str(request.url),
            screenshot_url=screenshot_url if request.return_url else None,
            screenshot_id=screenshot_id,
            width=request.width,
            height=request.height,
            format=request.format,
            size_bytes=len(screenshot_bytes),
            created_at=screenshot_record.created_at.isoformat(),
            device_used=request.device,
            usage={
                "current": current_user.usage_screenshots,
                "limit": limit,
                "remaining": limit - current_user.usage_screenshots
            }
        )
        
    except httpx.HTTPError as e:
        logger.error(f"HTTP error loading URL {request.url}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to load URL: {str(e)}")
    except ValueError as e:
        # Feature validation errors
        logger.error(f"Feature validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Screenshot failed: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Screenshot failed: {str(e)}")


@router.get("/devices", response_model=DeviceListResponse)
async def list_devices(
    current_user: User = Depends(get_current_user)
):
    """
    Get available device presets for mobile screenshots
    
    Requires Pro tier or higher
    """
    if not check_feature_access(current_user, "device_emulation"):
        raise HTTPException(
            status_code=403,
            detail="Device emulation requires Pro tier. Please upgrade."
        )
    
    devices = screenshot_service.get_available_devices()
    
    descriptions = {
        "iphone_13": "iPhone 13 (390x844, iOS 15)",
        "iphone_13_pro_max": "iPhone 13 Pro Max (428x926, iOS 15)",
        "pixel_5": "Google Pixel 5 (393x851, Android 11)",
        "ipad_pro": "iPad Pro 11\" (1024x1366, iOS 15)",
        "desktop": "Desktop (1920x1080, Windows)"
    }
    
    return DeviceListResponse(
        devices=devices,
        descriptions=descriptions
    )


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
    
    # Calculate percentage safely
    screenshots_used = current_user.usage_screenshots or 0
    screenshots_limit = tier_limits["screenshots"]
    percentage = round((screenshots_used / screenshots_limit) * 100, 1) if screenshots_limit > 0 else 0
    
    return {
        "tier": current_user.subscription_tier or "free",
        "usage": {
            "screenshots": {
                "used": screenshots_used,
                "limit": screenshots_limit,
                "remaining": max(0, screenshots_limit - screenshots_used),
                "percentage": percentage
            },
            "batch_requests": {
                "used": current_user.usage_batch_requests or 0,
                "limit": tier_limits["batch_requests"],
                "remaining": max(0, tier_limits["batch_requests"] - (current_user.usage_batch_requests or 0))
            },
            "api_calls": {
                "used": current_user.usage_api_calls or 0
            }
        },
        "limits": tier_limits,
        "reset_date": current_user.usage_reset_at.isoformat() if current_user.usage_reset_at else None,
        "features": {
            "custom_js": check_feature_access(current_user, "custom_js"),
            "device_emulation": check_feature_access(current_user, "device_emulation"),
            "element_selection": check_feature_access(current_user, "element_selection"),
            "pdf": check_feature_access(current_user, "pdf"),
            "webhooks": check_feature_access(current_user, "webhooks")
        }
    }