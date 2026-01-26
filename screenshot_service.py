# ============================================================================
# SCREENSHOT SERVICE - PixelPerfect API
# ============================================================================
# File: backend/screenshot_service.py
# Author: OneTechly
# Date: January 2026
# Purpose: Screenshot capture using Playwright with full customization
# ============================================================================

import os
import asyncio
import hashlib
import secrets
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
import logging

from playwright.async_api import async_playwright, Browser, Page, Error as PlaywrightError

logger = logging.getLogger("pixelperfect")

# ============================================================================
# CONFIGURATION
# ============================================================================

SCREENSHOTS_DIR = Path(__file__).resolve().parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

# Browser settings
DEFAULT_TIMEOUT = 30000  # 30 seconds
DEFAULT_VIEWPORT = {"width": 1920, "height": 1080}

# File size limits (bytes)
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Supported formats
SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "webp", "pdf"]

# ============================================================================
# SCREENSHOT CAPTURE CLASS
# ============================================================================

class ScreenshotService:
    """
    Screenshot capture service using Playwright
    
    Features:
    - Multiple formats (PNG, JPEG, WebP, PDF)
    - Full-page or viewport capture
    - Dark mode emulation
    - Custom viewport sizes
    - Timeout handling
    - Error recovery
    """
    
    def __init__(self):
        self.browser: Optional[Browser] = None
        self._lock = asyncio.Lock()
    
    async def initialize(self):
        """Initialize Playwright browser (called on startup)"""
        if self.browser:
            return
        
        try:
            playwright = await async_playwright().start()
            self.browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                ]
            )
            logger.info("✅ Playwright browser initialized")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Playwright: {e}")
            raise
    
    async def close(self):
        """Close browser (called on shutdown)"""
        if self.browser:
            await self.browser.close()
            logger.info("🔒 Playwright browser closed")
    
    async def capture_screenshot(
        self,
        url: str,
        width: int = 1920,
        height: int = 1080,
        format: str = "png",
        full_page: bool = False,
        dark_mode: bool = False,
        wait_until: str = "networkidle",
        timeout: int = DEFAULT_TIMEOUT,
    ) -> Dict[str, Any]:
        """
        Capture screenshot of a URL
        
        Args:
            url: Website URL to screenshot
            width: Viewport width in pixels
            height: Viewport height in pixels
            format: Output format (png, jpeg, webp, pdf)
            full_page: Capture entire page height
            dark_mode: Emulate dark mode
            wait_until: When to consider navigation succeeded
            timeout: Maximum time to wait (ms)
        
        Returns:
            Dictionary with screenshot metadata
        """
        # Validate format
        format = format.lower()
        if format not in SUPPORTED_FORMATS:
            raise ValueError(f"Unsupported format: {format}. Must be one of: {SUPPORTED_FORMATS}")
        
        # Ensure browser is initialized
        if not self.browser:
            await self.initialize()
        
        # Generate unique filename
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        random_id = secrets.token_hex(8)
        filename = f"screenshot_{timestamp}_{random_id}.{format}"
        filepath = SCREENSHOTS_DIR / filename
        
        # Create browser context with viewport
        async with self._lock:
            context = await self.browser.new_context(
                viewport={"width": width, "height": height},
                color_scheme="dark" if dark_mode else "light",
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            
            page = await context.new_page()
            
            try:
                # Navigate to URL
                logger.info(f"📸 Capturing screenshot: {url}")
                await page.goto(url, wait_until=wait_until, timeout=timeout)
                
                # Wait for page to be fully loaded
                await page.wait_for_load_state("networkidle", timeout=timeout)
                
                # Capture screenshot
                screenshot_options = {
                    "path": str(filepath),
                    "full_page": full_page,
                }
                
                if format in ["jpeg", "jpg"]:
                    screenshot_options["quality"] = 90
                    screenshot_options["type"] = "jpeg"
                elif format == "png":
                    screenshot_options["type"] = "png"
                
                if format == "pdf":
                    # PDF uses different method
                    await page.pdf(
                        path=str(filepath),
                        format="A4",
                        print_background=True,
                    )
                else:
                    await page.screenshot(**screenshot_options)
                
                # Get file info
                file_size = filepath.stat().st_size
                
                # Verify file size
                if file_size > MAX_FILE_SIZE:
                    filepath.unlink()  # Delete oversized file
                    raise ValueError(f"Screenshot too large: {file_size} bytes (max: {MAX_FILE_SIZE})")
                
                logger.info(f"✅ Screenshot saved: {filename} ({file_size} bytes)")
                
                return {
                    "filename": filename,
                    "filepath": str(filepath),
                    "url": url,
                    "width": width,
                    "height": height,
                    "format": format,
                    "full_page": full_page,
                    "dark_mode": dark_mode,
                    "file_size": file_size,
                    "created_at": datetime.utcnow(),
                }
                
            except PlaywrightError as e:
                logger.error(f"❌ Playwright error: {e}")
                raise ValueError(f"Failed to capture screenshot: {str(e)}")
            
            except asyncio.TimeoutError:
                logger.error(f"❌ Timeout capturing {url}")
                raise ValueError(f"Screenshot timeout after {timeout}ms")
            
            except Exception as e:
                logger.error(f"❌ Unexpected error: {e}")
                raise
            
            finally:
                await page.close()
                await context.close()
    
    async def delete_screenshot(self, filename: str) -> bool:
        """Delete a screenshot file"""
        filepath = SCREENSHOTS_DIR / filename
        
        try:
            if filepath.exists():
                filepath.unlink()
                logger.info(f"🗑️ Deleted screenshot: {filename}")
                return True
            return False
        except Exception as e:
            logger.error(f"❌ Failed to delete {filename}: {e}")
            return False


# ============================================================================
# SINGLETON INSTANCE
# ============================================================================

# Global screenshot service instance
screenshot_service = ScreenshotService()


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

async def capture_screenshot_async(
    url: str,
    width: int = 1920,
    height: int = 1080,
    format: str = "png",
    full_page: bool = False,
    dark_mode: bool = False,
) -> Dict[str, Any]:
    """
    Convenience function for capturing screenshots
    
    Usage:
        result = await capture_screenshot_async(
            url="https://example.com",
            format="png",
            full_page=True
        )
    """
    return await screenshot_service.capture_screenshot(
        url=url,
        width=width,
        height=height,
        format=format,
        full_page=full_page,
        dark_mode=dark_mode,
    )


def get_screenshot_url(filename: str, base_url: str = "") -> str:
    """
    Get public URL for a screenshot
    
    Args:
        filename: Screenshot filename
        base_url: Base URL of the API (e.g., http://localhost:8000)
    
    Returns:
        Full URL to the screenshot
    """
    if not base_url:
        base_url = os.getenv("BACKEND_URL", "http://localhost:8000")
    
    return f"{base_url.rstrip('/')}/screenshots/{filename}"


# ============================================================================
# USAGE TRACKING
# ============================================================================

def increment_user_usage(user, db):
    """Increment user's screenshot usage counter"""
    user.usage_screenshots = (user.usage_screenshots or 0) + 1
    user.usage_api_calls = (user.usage_api_calls or 0) + 1
    db.commit()
    db.refresh(user)


def check_usage_limit(user, tier_limits) -> bool:
    """
    Check if user has exceeded their screenshot limit
    
    Returns:
        True if within limit, False if exceeded
    """
    limit = tier_limits.get("screenshots")
    
    # Unlimited for premium tier
    if limit == "unlimited":
        return True
    
    # Check if limit exceeded
    current_usage = user.usage_screenshots or 0
    return current_usage < limit


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

"""
# Initialize on startup
await screenshot_service.initialize()

# Capture screenshot
result = await screenshot_service.capture_screenshot(
    url="https://example.com",
    width=1920,
    height=1080,
    format="png",
    full_page=True,
    dark_mode=False
)

# Result:
{
    "filename": "screenshot_20260125_120000_abc123.png",
    "filepath": "/path/to/screenshots/screenshot_20260125_120000_abc123.png",
    "url": "https://example.com",
    "width": 1920,
    "height": 1080,
    "format": "png",
    "full_page": True,
    "dark_mode": False,
    "file_size": 245678,
    "created_at": datetime(2026, 1, 25, 12, 0, 0)
}

# Close on shutdown
await screenshot_service.close()
"""