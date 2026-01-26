# ============================================================================
# SCREENSHOT SERVICE - PixelPerfect API (SYNC Playwright + async wrapper)
# ============================================================================
# File: backend/screenshot_service.py
# Author: OneTechly
# Updated: Jan 2026 (Production-ready hardening)
#
# Fixes:
# ✅ Python 3.12 safe: uses get_running_loop()
# ✅ Prevents double init under reload/concurrency (thread lock)
# ✅ Clean close sequence
# ✅ Better error messages for missing Playwright browsers
# ✅ Keeps your async interface intact
# ============================================================================

import os
import secrets
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from playwright.sync_api import sync_playwright, Browser, Error as PlaywrightError

logger = logging.getLogger("pixelperfect")

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
SCREENSHOTS_DIR = Path(__file__).resolve().parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

DEFAULT_TIMEOUT = 30_000
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "webp", "pdf"]

# Thread pool for sync Playwright (keep small)
_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="playwright")

# Guard against double-init / double-close (reload / concurrent requests)
_init_lock = threading.Lock()


class ScreenshotService:
    def __init__(self):
        self.browser: Optional[Browser] = None
        self.playwright = None
        self._initialized = False
        self._init_error: Optional[str] = None

    # ---------------------------
    # Init / Close
    # ---------------------------
    async def initialize(self) -> None:
        """Initialize Playwright browser (safe to call multiple times)."""
        if self._initialized:
            return

        # If we already tried and failed, keep failing fast with same message
        if self._init_error:
            raise RuntimeError(self._init_error)

        import asyncio
        loop = asyncio.get_running_loop()

        # Ensure only one init happens at a time
        def guarded_init():
            with _init_lock:
                if self._initialized:
                    return
                self._sync_initialize()
                self._initialized = True

        try:
            await loop.run_in_executor(_executor, guarded_init)
            logger.info("✅ Playwright browser initialized (sync mode)")
        except Exception as e:
            msg = _friendly_playwright_init_error(e)
            self._init_error = msg
            logger.error("❌ Failed to initialize Playwright: %s", msg)
            raise RuntimeError(msg) from e

    def _sync_initialize(self) -> None:
        """Sync initialization (runs in executor thread)."""
        self.playwright = sync_playwright().start()

        # NOTE: Keep args minimal and stable across Windows + Linux
        self.browser = self.playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

    async def close(self) -> None:
        """Close browser safely (called on shutdown)."""
        if not self._initialized and not self.browser and not self.playwright:
            return

        import asyncio
        loop = asyncio.get_running_loop()

        def guarded_close():
            with _init_lock:
                self._sync_close()
                self._initialized = False
                self._init_error = None

        try:
            await loop.run_in_executor(_executor, guarded_close)
            logger.info("🔒 Playwright browser closed")
        except Exception:
            logger.exception("❌ Failed while closing Playwright (non-fatal)")

    def _sync_close(self) -> None:
        """Sync close (runs in executor thread)."""
        try:
            if self.browser:
                self.browser.close()
        finally:
            self.browser = None
            if self.playwright:
                self.playwright.stop()
            self.playwright = None

    # ---------------------------
    # Public capture API
    # ---------------------------
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

        fmt = (format or "png").lower().strip()
        if fmt not in SUPPORTED_FORMATS:
            raise ValueError(f"Unsupported format: {fmt}. Must be one of: {SUPPORTED_FORMATS}")

        if not self._initialized:
            await self.initialize()

        import asyncio
        loop = asyncio.get_running_loop()

        return await loop.run_in_executor(
            _executor,
            self._sync_capture_screenshot,
            url,
            width,
            height,
            fmt,
            full_page,
            dark_mode,
            wait_until,
            timeout,
        )

    def _sync_capture_screenshot(
        self,
        url: str,
        width: int,
        height: int,
        fmt: str,
        full_page: bool,
        dark_mode: bool,
        wait_until: str,
        timeout: int,
    ) -> Dict[str, Any]:

        if not self.browser:
            raise RuntimeError("Playwright browser is not initialized")

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        random_id = secrets.token_hex(8)
        filename = f"screenshot_{timestamp}_{random_id}.{fmt}"
        filepath = SCREENSHOTS_DIR / filename

        context = self.browser.new_context(
            viewport={"width": int(width), "height": int(height)},
            color_scheme="dark" if dark_mode else "light",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        page = context.new_page()

        try:
            logger.info("📸 Capturing screenshot: %s", url)
            page.goto(url, wait_until=wait_until, timeout=int(timeout))
            # networkidle is sometimes flaky on heavy sites; but you already default to it
            # keep it to maintain behavior
            page.wait_for_load_state("networkidle", timeout=int(timeout))

            if fmt == "pdf":
                page.pdf(path=str(filepath), format="A4", print_background=True)
            else:
                options: Dict[str, Any] = {"path": str(filepath), "full_page": bool(full_page)}
                if fmt in ("jpeg", "jpg"):
                    options["type"] = "jpeg"
                    options["quality"] = 90
                elif fmt == "png":
                    options["type"] = "png"
                elif fmt == "webp":
                    options["type"] = "webp"
                page.screenshot(**options)

            file_size = filepath.stat().st_size
            if file_size > MAX_FILE_SIZE:
                try:
                    filepath.unlink(missing_ok=True)  # py3.8+ on windows ok
                except Exception:
                    pass
                raise ValueError(f"Screenshot too large: {file_size} bytes (max: {MAX_FILE_SIZE})")

            logger.info("✅ Screenshot saved: %s (%d bytes)", filename, file_size)

            return {
                "filename": filename,
                "filepath": str(filepath),
                "url": url,
                "width": int(width),
                "height": int(height),
                "format": fmt,
                "full_page": bool(full_page),
                "dark_mode": bool(dark_mode),
                "file_size": int(file_size),
                "created_at": datetime.utcnow(),
            }

        except PlaywrightError as e:
            logger.error("❌ Playwright error: %s", e)
            raise ValueError(f"Failed to capture screenshot: {str(e)}") from e

        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass

    async def delete_screenshot(self, filename: str) -> bool:
        filepath = SCREENSHOTS_DIR / filename
        try:
            if filepath.exists():
                filepath.unlink()
                logger.info("🗑️ Deleted screenshot: %s", filename)
                return True
            return False
        except Exception as e:
            logger.error("❌ Failed to delete %s: %s", filename, e)
            return False


def _friendly_playwright_init_error(e: Exception) -> str:
    s = str(e) or e.__class__.__name__

    # Most common issue after installing playwright python package:
    # browsers not installed: "Executable doesn't exist" or similar
    lower = s.lower()
    if "executable doesn't exist" in lower or "browser_type.launch" in lower or "playwright" in lower and "install" in lower:
        return (
            f"{s}\n\n"
            "Playwright browsers may be missing.\n"
            "Run:\n"
            "  python -m playwright install chromium\n"
        )

    # Windows policy issues (just in case someone reverts run.py)
    if "notimplementederror" in lower and "subprocess" in lower:
        return (
            f"{s}\n\n"
            "Windows event loop policy does not support subprocesses.\n"
            "Use WindowsProactorEventLoopPolicy() in run.py/main.py.\n"
        )

    return s


# Singleton
screenshot_service = ScreenshotService()


# Convenience helpers (kept for compatibility)
async def capture_screenshot_async(
    url: str,
    width: int = 1920,
    height: int = 1080,
    format: str = "png",
    full_page: bool = False,
    dark_mode: bool = False,
) -> Dict[str, Any]:
    return await screenshot_service.capture_screenshot(
        url=url,
        width=width,
        height=height,
        format=format,
        full_page=full_page,
        dark_mode=dark_mode,
    )


def get_screenshot_url(filename: str, base_url: str = "") -> str:
    if not base_url:
        base_url = os.getenv("BACKEND_URL", "http://localhost:8000")
    return f"{base_url.rstrip('/')}/screenshots/{filename}"


# Usage tracking (kept)
def increment_user_usage(user, db):
    user.usage_screenshots = (user.usage_screenshots or 0) + 1
    user.usage_api_calls = (user.usage_api_calls or 0) + 1
    db.commit()
    db.refresh(user)


def check_usage_limit(user, tier_limits) -> bool:
    limit = tier_limits.get("screenshots")
    if limit == "unlimited":
        return True
    current_usage = user.usage_screenshots or 0
    return current_usage < limit


# # ============================================================================
# # SCREENSHOT SERVICE - PixelPerfect API (WINDOWS COMPATIBLE)
# # ============================================================================
# # File: backend/screenshot_service.py
# # Author: OneTechly
# # Date: January 2026
# # Purpose: Screenshot capture using SYNC Playwright (Windows compatible)
# # ============================================================================

# import os
# import secrets
# from pathlib import Path
# from datetime import datetime
# from typing import Optional, Dict, Any
# import logging
# from concurrent.futures import ThreadPoolExecutor

# from playwright.sync_api import sync_playwright, Browser, Page, Error as PlaywrightError

# logger = logging.getLogger("pixelperfect")

# # ============================================================================
# # CONFIGURATION
# # ============================================================================

# SCREENSHOTS_DIR = Path(__file__).resolve().parent / "screenshots"
# SCREENSHOTS_DIR.mkdir(exist_ok=True)

# # Browser settings
# DEFAULT_TIMEOUT = 30000  # 30 seconds
# DEFAULT_VIEWPORT = {"width": 1920, "height": 1080}

# # File size limits (bytes)
# MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# # Supported formats
# SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "webp", "pdf"]

# # Thread pool for running sync Playwright
# _executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="playwright")

# # ============================================================================
# # SCREENSHOT CAPTURE CLASS (SYNC VERSION)
# # ============================================================================

# class ScreenshotService:
#     """
#     Screenshot capture service using SYNC Playwright (Windows compatible)
    
#     Features:
#     - Multiple formats (PNG, JPEG, WebP, PDF)
#     - Full-page or viewport capture
#     - Dark mode emulation
#     - Custom viewport sizes
#     - Timeout handling
#     - Error recovery
#     - Windows compatible (no async subprocess issues!)
#     """
    
#     def __init__(self):
#         self.browser: Optional[Browser] = None
#         self.playwright = None
#         self._initialized = False
    
#     async def initialize(self):
#         """Initialize Playwright browser (called on startup)"""
#         if self._initialized:
#             return
        
#         try:
#             # Run initialization in thread pool (sync Playwright)
#             import asyncio
#             loop = asyncio.get_event_loop()
#             await loop.run_in_executor(_executor, self._sync_initialize)
#             self._initialized = True
#             logger.info("✅ Playwright browser initialized (sync mode - Windows compatible)")
#         except Exception as e:
#             logger.error(f"❌ Failed to initialize Playwright: {e}")
#             raise
    
#     def _sync_initialize(self):
#         """Sync initialization (runs in thread pool)"""
#         self.playwright = sync_playwright().start()
#         self.browser = self.playwright.chromium.launch(
#             headless=True,
#             args=[
#                 '--no-sandbox',
#                 '--disable-setuid-sandbox',
#                 '--disable-dev-shm-usage',
#                 '--disable-gpu',
#             ]
#         )
    
#     async def close(self):
#         """Close browser (called on shutdown)"""
#         if self.browser:
#             import asyncio
#             loop = asyncio.get_event_loop()
#             await loop.run_in_executor(_executor, self._sync_close)
#             logger.info("🔒 Playwright browser closed")
    
#     def _sync_close(self):
#         """Sync close (runs in thread pool)"""
#         if self.browser:
#             self.browser.close()
#         if self.playwright:
#             self.playwright.stop()
    
#     async def capture_screenshot(
#         self,
#         url: str,
#         width: int = 1920,
#         height: int = 1080,
#         format: str = "png",
#         full_page: bool = False,
#         dark_mode: bool = False,
#         wait_until: str = "networkidle",
#         timeout: int = DEFAULT_TIMEOUT,
#     ) -> Dict[str, Any]:
#         """
#         Capture screenshot of a URL
        
#         Args:
#             url: Website URL to screenshot
#             width: Viewport width in pixels
#             height: Viewport height in pixels
#             format: Output format (png, jpeg, webp, pdf)
#             full_page: Capture entire page height
#             dark_mode: Emulate dark mode
#             wait_until: When to consider navigation succeeded
#             timeout: Maximum time to wait (ms)
        
#         Returns:
#             Dictionary with screenshot metadata
#         """
#         # Validate format
#         format = format.lower()
#         if format not in SUPPORTED_FORMATS:
#             raise ValueError(f"Unsupported format: {format}. Must be one of: {SUPPORTED_FORMATS}")
        
#         # Ensure browser is initialized
#         if not self._initialized:
#             await self.initialize()
        
#         # Run capture in thread pool
#         import asyncio
#         loop = asyncio.get_event_loop()
#         result = await loop.run_in_executor(
#             _executor,
#             self._sync_capture_screenshot,
#             url,
#             width,
#             height,
#             format,
#             full_page,
#             dark_mode,
#             wait_until,
#             timeout,
#         )
        
#         return result
    
#     def _sync_capture_screenshot(
#         self,
#         url: str,
#         width: int,
#         height: int,
#         format: str,
#         full_page: bool,
#         dark_mode: bool,
#         wait_until: str,
#         timeout: int,
#     ) -> Dict[str, Any]:
#         """
#         Sync screenshot capture (runs in thread pool)
#         """
#         # Generate unique filename
#         timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
#         random_id = secrets.token_hex(8)
#         filename = f"screenshot_{timestamp}_{random_id}.{format}"
#         filepath = SCREENSHOTS_DIR / filename
        
#         # Create browser context with viewport
#         context = self.browser.new_context(
#             viewport={"width": width, "height": height},
#             color_scheme="dark" if dark_mode else "light",
#             user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
#         )
        
#         page = context.new_page()
        
#         try:
#             # Navigate to URL
#             logger.info(f"📸 Capturing screenshot: {url}")
#             page.goto(url, wait_until=wait_until, timeout=timeout)
            
#             # Wait for page to be fully loaded
#             page.wait_for_load_state("networkidle", timeout=timeout)
            
#             # Capture screenshot
#             screenshot_options = {
#                 "path": str(filepath),
#                 "full_page": full_page,
#             }
            
#             if format in ["jpeg", "jpg"]:
#                 screenshot_options["quality"] = 90
#                 screenshot_options["type"] = "jpeg"
#             elif format == "png":
#                 screenshot_options["type"] = "png"
            
#             if format == "pdf":
#                 # PDF uses different method
#                 page.pdf(
#                     path=str(filepath),
#                     format="A4",
#                     print_background=True,
#                 )
#             else:
#                 page.screenshot(**screenshot_options)
            
#             # Get file info
#             file_size = filepath.stat().st_size
            
#             # Verify file size
#             if file_size > MAX_FILE_SIZE:
#                 filepath.unlink()  # Delete oversized file
#                 raise ValueError(f"Screenshot too large: {file_size} bytes (max: {MAX_FILE_SIZE})")
            
#             logger.info(f"✅ Screenshot saved: {filename} ({file_size} bytes)")
            
#             return {
#                 "filename": filename,
#                 "filepath": str(filepath),
#                 "url": url,
#                 "width": width,
#                 "height": height,
#                 "format": format,
#                 "full_page": full_page,
#                 "dark_mode": dark_mode,
#                 "file_size": file_size,
#                 "created_at": datetime.utcnow(),
#             }
            
#         except PlaywrightError as e:
#             logger.error(f"❌ Playwright error: {e}")
#             raise ValueError(f"Failed to capture screenshot: {str(e)}")
        
#         except Exception as e:
#             logger.error(f"❌ Unexpected error: {e}")
#             raise
        
#         finally:
#             page.close()
#             context.close()
    
#     async def delete_screenshot(self, filename: str) -> bool:
#         """Delete a screenshot file"""
#         filepath = SCREENSHOTS_DIR / filename
        
#         try:
#             if filepath.exists():
#                 filepath.unlink()
#                 logger.info(f"🗑️ Deleted screenshot: {filename}")
#                 return True
#             return False
#         except Exception as e:
#             logger.error(f"❌ Failed to delete {filename}: {e}")
#             return False


# # ============================================================================
# # SINGLETON INSTANCE
# # ============================================================================

# # Global screenshot service instance
# screenshot_service = ScreenshotService()


# # ============================================================================
# # HELPER FUNCTIONS
# # ============================================================================

# async def capture_screenshot_async(
#     url: str,
#     width: int = 1920,
#     height: int = 1080,
#     format: str = "png",
#     full_page: bool = False,
#     dark_mode: bool = False,
# ) -> Dict[str, Any]:
#     """
#     Convenience function for capturing screenshots
    
#     Usage:
#         result = await capture_screenshot_async(
#             url="https://example.com",
#             format="png",
#             full_page=True
#         )
#     """
#     return await screenshot_service.capture_screenshot(
#         url=url,
#         width=width,
#         height=height,
#         format=format,
#         full_page=full_page,
#         dark_mode=dark_mode,
#     )


# def get_screenshot_url(filename: str, base_url: str = "") -> str:
#     """
#     Get public URL for a screenshot
    
#     Args:
#         filename: Screenshot filename
#         base_url: Base URL of the API (e.g., http://localhost:8000)
    
#     Returns:
#         Full URL to the screenshot
#     """
#     if not base_url:
#         base_url = os.getenv("BACKEND_URL", "http://localhost:8000")
    
#     return f"{base_url.rstrip('/')}/screenshots/{filename}"


# # ============================================================================
# # USAGE TRACKING
# # ============================================================================

# def increment_user_usage(user, db):
#     """Increment user's screenshot usage counter"""
#     user.usage_screenshots = (user.usage_screenshots or 0) + 1
#     user.usage_api_calls = (user.usage_api_calls or 0) + 1
#     db.commit()
#     db.refresh(user)


# def check_usage_limit(user, tier_limits) -> bool:
#     """
#     Check if user has exceeded their screenshot limit
    
#     Returns:
#         True if within limit, False if exceeded
#     """
#     limit = tier_limits.get("screenshots")
    
#     # Unlimited for premium tier
#     if limit == "unlimited":
#         return True
    
#     # Check if limit exceeded
#     current_usage = user.usage_screenshots or 0
#     return current_usage < limit


# # ============================================================================
# # USAGE NOTES
# # ============================================================================

# """
# This is a Windows-compatible version using SYNC Playwright API instead of async.

# Key differences from async version:
# 1. Uses sync_playwright() instead of async_playwright()
# 2. Runs in ThreadPoolExecutor to avoid blocking the event loop
# 3. Works on Windows without subprocess issues!
# 4. Same API interface (async functions still work)

# The async interface is preserved by running sync operations in a thread pool,
# so all your existing async code continues to work!
# """