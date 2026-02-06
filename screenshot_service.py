# ============================================================================
# SCREENSHOT SERVICE - PixelPerfect API (PRODUCTION READY)
# ============================================================================
# File: backend/screenshot_service.py
# Author: OneTechly
# Updated: February 2026
# ============================================================================
# ✅ PRODUCTION READY
# ✅ Python 3.12 safe: uses get_running_loop()
# ✅ Prevents double init under reload/concurrency (thread lock)
# ✅ Clean close sequence
# ✅ Better error messages for missing Playwright browsers
# ✅ FIXED: Timeout retry logic with 3-tier fallback strategy
# ✅ FIXED: Works with heavy websites (Vogue, NYTimes, WSJ, etc.)
# ✅ FIXED: WebP format support via PNG conversion with Pillow
# ✅ FIXED: Robust format validation and handling
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

# ✅ UPDATED: Increased default timeout for heavy sites
DEFAULT_TIMEOUT = 45_000  # 45 seconds (was 30s)
FALLBACK_TIMEOUT = 60_000  # 60 seconds for retry
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Pillow for WebP conversion (optional dependency)
try:
    from PIL import Image
    PILLOW_AVAILABLE = True
    SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "webp", "pdf"]
    logger.info("✅ Pillow available - WebP format enabled")
except ImportError:
    PILLOW_AVAILABLE = False
    SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "pdf"]
    logger.warning("⚠️ Pillow not available - WebP format disabled")

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
        
        # ✅ CRITICAL FIX: Validate format based on availability
        if fmt not in SUPPORTED_FORMATS:
            if fmt == "webp" and not PILLOW_AVAILABLE:
                raise ValueError(
                    f"WebP format requires Pillow library. Install with: pip install Pillow. "
                    f"Supported formats: {SUPPORTED_FORMATS}"
                )
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
        """
        Capture screenshot with intelligent retry logic for heavy websites.
        
        ✅ CRITICAL FIX: Implements 3-tier fallback strategy
        ✅ Works with heavy sites like Vogue, NYTimes, WSJ
        ✅ FIXED: WebP support via PNG conversion
        ✅ FIXED: Proper format handling for all types
        
        Fallback Strategy:
        1. Try with user-requested wait_until (default: networkidle)
        2. If timeout, retry with 'domcontentloaded' (more lenient)
        3. If still timeout, retry with 'load' (most lenient)
        """

        if not self.browser:
            raise RuntimeError("Playwright browser is not initialized")

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        random_id = secrets.token_hex(8)
        
        # ✅ CRITICAL FIX: Handle WebP specially
        if fmt == "webp":
            # Capture as PNG first, then convert
            temp_fmt = "png"
            temp_filename = f"screenshot_{timestamp}_{random_id}.png"
            final_filename = f"screenshot_{timestamp}_{random_id}.webp"
            temp_filepath = SCREENSHOTS_DIR / temp_filename
            filepath = SCREENSHOTS_DIR / final_filename
        else:
            filename = f"screenshot_{timestamp}_{random_id}.{fmt}"
            filepath = SCREENSHOTS_DIR / filename
            temp_filepath = None

        context = self.browser.new_context(
            viewport={"width": int(width), "height": int(height)},
            color_scheme="dark" if dark_mode else "light",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        page = context.new_page()

        try:
            logger.info("📸 Capturing screenshot: %s (format: %s)", url, fmt)
            
            # ✅ CRITICAL FIX: Multi-tier retry strategy
            page_loaded = False
            last_error = None
            
            # Strategy 1: Try with user-requested wait_until
            try:
                logger.debug(f"Attempt 1: Using wait_until='{wait_until}', timeout={timeout}ms")
                page.goto(url, wait_until=wait_until, timeout=int(timeout))
                page.wait_for_load_state(wait_until, timeout=int(timeout))
                page_loaded = True
                logger.info(f"✅ Page loaded successfully with '{wait_until}' strategy")
                
            except PlaywrightError as e:
                last_error = e
                error_str = str(e)
                
                if "Timeout" in error_str and wait_until == "networkidle":
                    # Strategy 2: Retry with 'domcontentloaded' (more lenient)
                    logger.warning(f"⏱️ Timeout with '{wait_until}', retrying with 'domcontentloaded'")
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=FALLBACK_TIMEOUT)
                        page.wait_for_load_state("domcontentloaded", timeout=FALLBACK_TIMEOUT)
                        page_loaded = True
                        logger.info("✅ Page loaded successfully with 'domcontentloaded' strategy")
                    except PlaywrightError as e2:
                        last_error = e2
                        logger.warning(f"⏱️ Timeout with 'domcontentloaded', trying final fallback")
                        
                        # Strategy 3: Final fallback with 'load' (most lenient)
                        try:
                            page.goto(url, wait_until="load", timeout=FALLBACK_TIMEOUT)
                            page.wait_for_load_state("load", timeout=FALLBACK_TIMEOUT)
                            page_loaded = True
                            logger.info("✅ Page loaded successfully with 'load' strategy (fallback)")
                        except PlaywrightError as e3:
                            last_error = e3
                            logger.error(f"❌ All retry strategies failed")
                            # Let it raise below
                
                elif "Timeout" in error_str:
                    # For non-networkidle timeouts, try 'load' directly
                    logger.warning(f"⏱️ Timeout with '{wait_until}', retrying with 'load'")
                    try:
                        page.goto(url, wait_until="load", timeout=FALLBACK_TIMEOUT)
                        page.wait_for_load_state("load", timeout=FALLBACK_TIMEOUT)
                        page_loaded = True
                        logger.info("✅ Page loaded successfully with 'load' strategy")
                    except PlaywrightError as e2:
                        last_error = e2
                        # Let it raise below
                else:
                    # Non-timeout error, re-raise immediately
                    raise
            
            # If page still didn't load after all retries, raise the last error
            if not page_loaded and last_error:
                raise last_error

            # ✅ Give page extra time to settle before screenshot
            # This helps with dynamic content that loads after page.goto()
            try:
                page.wait_for_timeout(2000)  # Wait 2 seconds for JS to settle
                logger.debug("✅ Extra settling time completed")
            except Exception:
                pass  # Non-critical if this fails

            # ✅ CRITICAL FIX: Take the screenshot with proper format handling
            if fmt == "pdf":
                page.pdf(path=str(filepath), format="A4", print_background=True)
                logger.info("✅ PDF captured successfully")
                
            elif fmt == "webp" and PILLOW_AVAILABLE:
                # Capture as PNG first
                logger.debug("📸 Capturing as PNG for WebP conversion")
                options: Dict[str, Any] = {
                    "path": str(temp_filepath),
                    "full_page": bool(full_page),
                    "type": "png"  # Force PNG
                }
                page.screenshot(**options)
                logger.debug("✅ PNG captured, converting to WebP")
                
                # Convert PNG to WebP using Pillow
                img = Image.open(str(temp_filepath))
                img.save(str(filepath), "WEBP", quality=90, method=6)
                logger.info("✅ Converted PNG to WebP successfully")
                
                # Clean up temp PNG file
                try:
                    temp_filepath.unlink()
                except Exception as e:
                    logger.warning(f"Failed to delete temp PNG: {e}")
                    
            else:
                # Standard PNG/JPEG capture
                options: Dict[str, Any] = {
                    "path": str(filepath),
                    "full_page": bool(full_page)
                }
                
                # ✅ CRITICAL FIX: Playwright only supports "png" and "jpeg" types
                if fmt in ("jpeg", "jpg"):
                    options["type"] = "jpeg"
                    options["quality"] = 90
                else:  # png
                    options["type"] = "png"
                
                page.screenshot(**options)
                logger.info(f"✅ {fmt.upper()} screenshot captured successfully")

            file_size = filepath.stat().st_size
            if file_size > MAX_FILE_SIZE:
                try:
                    filepath.unlink(missing_ok=True)  # py3.8+ on windows ok
                except Exception:
                    pass
                raise ValueError(f"Screenshot too large: {file_size} bytes (max: {MAX_FILE_SIZE})")

            logger.info("✅ Screenshot saved: %s (%d bytes)", filepath.name, file_size)

            return {
                "filename": filepath.name,
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
            
            # ✅ IMPROVED ERROR MESSAGES
            error_msg = str(e)
            if "Timeout" in error_msg:
                # Extract URL from error for better debugging
                url_hint = url[:50] + "..." if len(url) > 50 else url
                raise ValueError(
                    f"Failed to capture screenshot: Page timeout after multiple retry attempts. "
                    f"The website ({url_hint}) may be too slow or have continuous network activity. "
                    f"Try again with a simpler page or contact support."
                ) from e
            else:
                raise ValueError(f"Failed to capture screenshot: {error_msg}") from e

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
            "  python -m playwright install --with-deps chromium\n"
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

# # ========================================

# # ============================================================================
# # SCREENSHOT SERVICE - PixelPerfect API (PRODUCTION READY)
# # ============================================================================
# # File: backend/screenshot_service.py
# # Author: OneTechly
# # Updated: February 2026
# # ============================================================================
# # ✅ PRODUCTION READY
# # ✅ Python 3.12 safe: uses get_running_loop()
# # ✅ Prevents double init under reload/concurrency (thread lock)
# # ✅ Clean close sequence
# # ✅ Better error messages for missing Playwright browsers
# # ✅ FIXED: Timeout retry logic with 3-tier fallback strategy
# # ✅ FIXED: Works with heavy websites (Vogue, NYTimes, WSJ, etc.)
# # ✅ FIXED: WebP format support via PNG conversion with Pillow
# # ✅ FIXED: Robust format validation and handling
# # ============================================================================

# import os
# import secrets
# from pathlib import Path
# from datetime import datetime
# from typing import Optional, Dict, Any
# import logging
# import threading
# from concurrent.futures import ThreadPoolExecutor

# from playwright.sync_api import sync_playwright, Browser, Error as PlaywrightError

# logger = logging.getLogger("pixelperfect")

# # ----------------------------------------------------------------------------
# # CONFIG
# # ----------------------------------------------------------------------------
# SCREENSHOTS_DIR = Path(__file__).resolve().parent / "screenshots"
# SCREENSHOTS_DIR.mkdir(exist_ok=True)

# # ✅ UPDATED: Increased default timeout for heavy sites
# DEFAULT_TIMEOUT = 45_000  # 45 seconds (was 30s)
# FALLBACK_TIMEOUT = 60_000  # 60 seconds for retry
# MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# # Pillow for WebP conversion (optional dependency)
# try:
#     from PIL import Image
#     PILLOW_AVAILABLE = True
#     SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "webp", "pdf"]
#     logger.info("✅ Pillow available - WebP format enabled")
# except ImportError:
#     PILLOW_AVAILABLE = False
#     SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "pdf"]
#     logger.warning("⚠️ Pillow not available - WebP format disabled")

# # Thread pool for sync Playwright (keep small)
# _executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="playwright")

# # Guard against double-init / double-close (reload / concurrent requests)
# _init_lock = threading.Lock()


# class ScreenshotService:
#     def __init__(self):
#         self.browser: Optional[Browser] = None
#         self.playwright = None
#         self._initialized = False
#         self._init_error: Optional[str] = None

#     # ---------------------------
#     # Init / Close
#     # ---------------------------
#     async def initialize(self) -> None:
#         """Initialize Playwright browser (safe to call multiple times)."""
#         if self._initialized:
#             return

#         # If we already tried and failed, keep failing fast with same message
#         if self._init_error:
#             raise RuntimeError(self._init_error)

#         import asyncio
#         loop = asyncio.get_running_loop()

#         # Ensure only one init happens at a time
#         def guarded_init():
#             with _init_lock:
#                 if self._initialized:
#                     return
#                 self._sync_initialize()
#                 self._initialized = True

#         try:
#             await loop.run_in_executor(_executor, guarded_init)
#             logger.info("✅ Playwright browser initialized (sync mode)")
#         except Exception as e:
#             msg = _friendly_playwright_init_error(e)
#             self._init_error = msg
#             logger.error("❌ Failed to initialize Playwright: %s", msg)
#             raise RuntimeError(msg) from e

#     def _sync_initialize(self) -> None:
#         """Sync initialization (runs in executor thread)."""
#         self.playwright = sync_playwright().start()

#         # NOTE: Keep args minimal and stable across Windows + Linux
#         self.browser = self.playwright.chromium.launch(
#             headless=True,
#             args=[
#                 "--no-sandbox",
#                 "--disable-setuid-sandbox",
#                 "--disable-dev-shm-usage",
#                 "--disable-gpu",
#             ],
#         )

#     async def close(self) -> None:
#         """Close browser safely (called on shutdown)."""
#         if not self._initialized and not self.browser and not self.playwright:
#             return

#         import asyncio
#         loop = asyncio.get_running_loop()

#         def guarded_close():
#             with _init_lock:
#                 self._sync_close()
#                 self._initialized = False
#                 self._init_error = None

#         try:
#             await loop.run_in_executor(_executor, guarded_close)
#             logger.info("🔒 Playwright browser closed")
#         except Exception:
#             logger.exception("❌ Failed while closing Playwright (non-fatal)")

#     def _sync_close(self) -> None:
#         """Sync close (runs in executor thread)."""
#         try:
#             if self.browser:
#                 self.browser.close()
#         finally:
#             self.browser = None
#             if self.playwright:
#                 self.playwright.stop()
#             self.playwright = None

#     # ---------------------------
#     # Public capture API
#     # ---------------------------
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

#         fmt = (format or "png").lower().strip()
        
#         # ✅ CRITICAL FIX: Validate format based on availability
#         if fmt not in SUPPORTED_FORMATS:
#             if fmt == "webp" and not PILLOW_AVAILABLE:
#                 raise ValueError(
#                     f"WebP format requires Pillow library. Install with: pip install Pillow. "
#                     f"Supported formats: {SUPPORTED_FORMATS}"
#                 )
#             raise ValueError(f"Unsupported format: {fmt}. Must be one of: {SUPPORTED_FORMATS}")

#         if not self._initialized:
#             await self.initialize()

#         import asyncio
#         loop = asyncio.get_running_loop()

#         return await loop.run_in_executor(
#             _executor,
#             self._sync_capture_screenshot,
#             url,
#             width,
#             height,
#             fmt,
#             full_page,
#             dark_mode,
#             wait_until,
#             timeout,
#         )

#     def _sync_capture_screenshot(
#         self,
#         url: str,
#         width: int,
#         height: int,
#         fmt: str,
#         full_page: bool,
#         dark_mode: bool,
#         wait_until: str,
#         timeout: int,
#     ) -> Dict[str, Any]:
#         """
#         Capture screenshot with intelligent retry logic for heavy websites.
        
#         ✅ CRITICAL FIX: Implements 3-tier fallback strategy
#         ✅ Works with heavy sites like Vogue, NYTimes, WSJ
#         ✅ FIXED: WebP support via PNG conversion
#         ✅ FIXED: Proper format handling for all types
        
#         Fallback Strategy:
#         1. Try with user-requested wait_until (default: networkidle)
#         2. If timeout, retry with 'domcontentloaded' (more lenient)
#         3. If still timeout, retry with 'load' (most lenient)
#         """

#         if not self.browser:
#             raise RuntimeError("Playwright browser is not initialized")

#         timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
#         random_id = secrets.token_hex(8)
        
#         # ✅ CRITICAL FIX: Handle WebP specially
#         if fmt == "webp":
#             # Capture as PNG first, then convert
#             temp_fmt = "png"
#             temp_filename = f"screenshot_{timestamp}_{random_id}.png"
#             final_filename = f"screenshot_{timestamp}_{random_id}.webp"
#             temp_filepath = SCREENSHOTS_DIR / temp_filename
#             filepath = SCREENSHOTS_DIR / final_filename
#         else:
#             filename = f"screenshot_{timestamp}_{random_id}.{fmt}"
#             filepath = SCREENSHOTS_DIR / filename
#             temp_filepath = None

#         context = self.browser.new_context(
#             viewport={"width": int(width), "height": int(height)},
#             color_scheme="dark" if dark_mode else "light",
#             user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
#         )
#         page = context.new_page()

#         try:
#             logger.info("📸 Capturing screenshot: %s (format: %s)", url, fmt)
            
#             # ✅ CRITICAL FIX: Multi-tier retry strategy
#             page_loaded = False
#             last_error = None
            
#             # Strategy 1: Try with user-requested wait_until
#             try:
#                 logger.debug(f"Attempt 1: Using wait_until='{wait_until}', timeout={timeout}ms")
#                 page.goto(url, wait_until=wait_until, timeout=int(timeout))
#                 page.wait_for_load_state(wait_until, timeout=int(timeout))
#                 page_loaded = True
#                 logger.info(f"✅ Page loaded successfully with '{wait_until}' strategy")
                
#             except PlaywrightError as e:
#                 last_error = e
#                 error_str = str(e)
                
#                 if "Timeout" in error_str and wait_until == "networkidle":
#                     # Strategy 2: Retry with 'domcontentloaded' (more lenient)
#                     logger.warning(f"⏱️ Timeout with '{wait_until}', retrying with 'domcontentloaded'")
#                     try:
#                         page.goto(url, wait_until="domcontentloaded", timeout=FALLBACK_TIMEOUT)
#                         page.wait_for_load_state("domcontentloaded", timeout=FALLBACK_TIMEOUT)
#                         page_loaded = True
#                         logger.info("✅ Page loaded successfully with 'domcontentloaded' strategy")
#                     except PlaywrightError as e2:
#                         last_error = e2
#                         logger.warning(f"⏱️ Timeout with 'domcontentloaded', trying final fallback")
                        
#                         # Strategy 3: Final fallback with 'load' (most lenient)
#                         try:
#                             page.goto(url, wait_until="load", timeout=FALLBACK_TIMEOUT)
#                             page.wait_for_load_state("load", timeout=FALLBACK_TIMEOUT)
#                             page_loaded = True
#                             logger.info("✅ Page loaded successfully with 'load' strategy (fallback)")
#                         except PlaywrightError as e3:
#                             last_error = e3
#                             logger.error(f"❌ All retry strategies failed")
#                             # Let it raise below
                
#                 elif "Timeout" in error_str:
#                     # For non-networkidle timeouts, try 'load' directly
#                     logger.warning(f"⏱️ Timeout with '{wait_until}', retrying with 'load'")
#                     try:
#                         page.goto(url, wait_until="load", timeout=FALLBACK_TIMEOUT)
#                         page.wait_for_load_state("load", timeout=FALLBACK_TIMEOUT)
#                         page_loaded = True
#                         logger.info("✅ Page loaded successfully with 'load' strategy")
#                     except PlaywrightError as e2:
#                         last_error = e2
#                         # Let it raise below
#                 else:
#                     # Non-timeout error, re-raise immediately
#                     raise
            
#             # If page still didn't load after all retries, raise the last error
#             if not page_loaded and last_error:
#                 raise last_error

#             # ✅ Give page extra time to settle before screenshot
#             # This helps with dynamic content that loads after page.goto()
#             try:
#                 page.wait_for_timeout(2000)  # Wait 2 seconds for JS to settle
#                 logger.debug("✅ Extra settling time completed")
#             except Exception:
#                 pass  # Non-critical if this fails

#             # ✅ CRITICAL FIX: Take the screenshot with proper format handling
#             if fmt == "pdf":
#                 page.pdf(path=str(filepath), format="A4", print_background=True)
#                 logger.info("✅ PDF captured successfully")
                
#             elif fmt == "webp" and PILLOW_AVAILABLE:
#                 # Capture as PNG first
#                 logger.debug("📸 Capturing as PNG for WebP conversion")
#                 options: Dict[str, Any] = {
#                     "path": str(temp_filepath),
#                     "full_page": bool(full_page),
#                     "type": "png"  # Force PNG
#                 }
#                 page.screenshot(**options)
#                 logger.debug("✅ PNG captured, converting to WebP")
                
#                 # Convert PNG to WebP using Pillow
#                 img = Image.open(str(temp_filepath))
#                 img.save(str(filepath), "WEBP", quality=90, method=6)
#                 logger.info("✅ Converted PNG to WebP successfully")
                
#                 # Clean up temp PNG file
#                 try:
#                     temp_filepath.unlink()
#                 except Exception as e:
#                     logger.warning(f"Failed to delete temp PNG: {e}")
                    
#             else:
#                 # Standard PNG/JPEG capture
#                 options: Dict[str, Any] = {
#                     "path": str(filepath),
#                     "full_page": bool(full_page)
#                 }
                
#                 # ✅ CRITICAL FIX: Playwright only supports "png" and "jpeg" types
#                 if fmt in ("jpeg", "jpg"):
#                     options["type"] = "jpeg"
#                     options["quality"] = 90
#                 else:  # png
#                     options["type"] = "png"
                
#                 page.screenshot(**options)
#                 logger.info(f"✅ {fmt.upper()} screenshot captured successfully")

#             file_size = filepath.stat().st_size
#             if file_size > MAX_FILE_SIZE:
#                 try:
#                     filepath.unlink(missing_ok=True)  # py3.8+ on windows ok
#                 except Exception:
#                     pass
#                 raise ValueError(f"Screenshot too large: {file_size} bytes (max: {MAX_FILE_SIZE})")

#             logger.info("✅ Screenshot saved: %s (%d bytes)", filepath.name, file_size)

#             return {
#                 "filename": filepath.name,
#                 "filepath": str(filepath),
#                 "url": url,
#                 "width": int(width),
#                 "height": int(height),
#                 "format": fmt,
#                 "full_page": bool(full_page),
#                 "dark_mode": bool(dark_mode),
#                 "file_size": int(file_size),
#                 "created_at": datetime.utcnow(),
#             }

#         except PlaywrightError as e:
#             logger.error("❌ Playwright error: %s", e)
            
#             # ✅ IMPROVED ERROR MESSAGES
#             error_msg = str(e)
#             if "Timeout" in error_msg:
#                 # Extract URL from error for better debugging
#                 url_hint = url[:50] + "..." if len(url) > 50 else url
#                 raise ValueError(
#                     f"Failed to capture screenshot: Page timeout after multiple retry attempts. "
#                     f"The website ({url_hint}) may be too slow or have continuous network activity. "
#                     f"Try again with a simpler page or contact support."
#                 ) from e
#             else:
#                 raise ValueError(f"Failed to capture screenshot: {error_msg}") from e

#         finally:
#             try:
#                 page.close()
#             except Exception:
#                 pass
#             try:
#                 context.close()
#             except Exception:
#                 pass

#     async def delete_screenshot(self, filename: str) -> bool:
#         filepath = SCREENSHOTS_DIR / filename
#         try:
#             if filepath.exists():
#                 filepath.unlink()
#                 logger.info("🗑️ Deleted screenshot: %s", filename)
#                 return True
#             return False
#         except Exception as e:
#             logger.error("❌ Failed to delete %s: %s", filename, e)
#             return False


# def _friendly_playwright_init_error(e: Exception) -> str:
#     s = str(e) or e.__class__.__name__

#     # Most common issue after installing playwright python package:
#     # browsers not installed: "Executable doesn't exist" or similar
#     lower = s.lower()
#     if "executable doesn't exist" in lower or "browser_type.launch" in lower or "playwright" in lower and "install" in lower:
#         return (
#             f"{s}\n\n"
#             "Playwright browsers may be missing.\n"
#             "Run:\n"
#             "  python -m playwright install --with-deps chromium\n"
#         )

#     # Windows policy issues (just in case someone reverts run.py)
#     if "notimplementederror" in lower and "subprocess" in lower:
#         return (
#             f"{s}\n\n"
#             "Windows event loop policy does not support subprocesses.\n"
#             "Use WindowsProactorEventLoopPolicy() in run.py/main.py.\n"
#         )

#     return s


# # Singleton
# screenshot_service = ScreenshotService()


# # Convenience helpers (kept for compatibility)
# async def capture_screenshot_async(
#     url: str,
#     width: int = 1920,
#     height: int = 1080,
#     format: str = "png",
#     full_page: bool = False,
#     dark_mode: bool = False,
# ) -> Dict[str, Any]:
#     return await screenshot_service.capture_screenshot(
#         url=url,
#         width=width,
#         height=height,
#         format=format,
#         full_page=full_page,
#         dark_mode=dark_mode,
#     )


# def get_screenshot_url(filename: str, base_url: str = "") -> str:
#     if not base_url:
#         base_url = os.getenv("BACKEND_URL", "http://localhost:8000")
#     return f"{base_url.rstrip('/')}/screenshots/{filename}"


# # Usage tracking (kept)
# def increment_user_usage(user, db):
#     user.usage_screenshots = (user.usage_screenshots or 0) + 1
#     user.usage_api_calls = (user.usage_api_calls or 0) + 1
#     db.commit()
#     db.refresh(user)


# def check_usage_limit(user, tier_limits) -> bool:
#     limit = tier_limits.get("screenshots")
#     if limit == "unlimited":
#         return True
#     current_usage = user.usage_screenshots or 0
#     return current_usage < limit

# # ============================================================================
# # END OF screenshot_service.py
# # ============================================================================





# ============================================================================
# END OF screenshot_service.py
# ============================================================================

# # ============================================================================
# # SCREENSHOT SERVICE - PixelPerfect API (PRODUCTION READY)
# # ============================================================================
# # File: backend/screenshot_service.py
# # Author: OneTechly
# # Updated: February 2026
# # ============================================================================
# # ✅ PRODUCTION READY
# # ✅ Python 3.12 safe: uses get_running_loop()
# # ✅ Prevents double init under reload/concurrency (thread lock)
# # ✅ Clean close sequence
# # ✅ Better error messages for missing Playwright browsers
# # ✅ FIXED: Timeout retry logic with 3-tier fallback strategy
# # ✅ FIXED: Works with heavy websites (Vogue, NYTimes, WSJ, etc.)
# # ============================================================================

# import os
# import secrets
# from pathlib import Path
# from datetime import datetime
# from typing import Optional, Dict, Any
# import logging
# import threading
# from concurrent.futures import ThreadPoolExecutor

# from playwright.sync_api import sync_playwright, Browser, Error as PlaywrightError

# logger = logging.getLogger("pixelperfect")

# # ----------------------------------------------------------------------------
# # CONFIG
# # ----------------------------------------------------------------------------
# SCREENSHOTS_DIR = Path(__file__).resolve().parent / "screenshots"
# SCREENSHOTS_DIR.mkdir(exist_ok=True)

# # ✅ UPDATED: Increased default timeout for heavy sites
# DEFAULT_TIMEOUT = 45_000  # 45 seconds (was 30s)
# FALLBACK_TIMEOUT = 60_000  # 60 seconds for retry
# MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
# SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "webp", "pdf"]

# # Thread pool for sync Playwright (keep small)
# _executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="playwright")

# # Guard against double-init / double-close (reload / concurrent requests)
# _init_lock = threading.Lock()


# class ScreenshotService:
#     def __init__(self):
#         self.browser: Optional[Browser] = None
#         self.playwright = None
#         self._initialized = False
#         self._init_error: Optional[str] = None

#     # ---------------------------
#     # Init / Close
#     # ---------------------------
#     async def initialize(self) -> None:
#         """Initialize Playwright browser (safe to call multiple times)."""
#         if self._initialized:
#             return

#         # If we already tried and failed, keep failing fast with same message
#         if self._init_error:
#             raise RuntimeError(self._init_error)

#         import asyncio
#         loop = asyncio.get_running_loop()

#         # Ensure only one init happens at a time
#         def guarded_init():
#             with _init_lock:
#                 if self._initialized:
#                     return
#                 self._sync_initialize()
#                 self._initialized = True

#         try:
#             await loop.run_in_executor(_executor, guarded_init)
#             logger.info("✅ Playwright browser initialized (sync mode)")
#         except Exception as e:
#             msg = _friendly_playwright_init_error(e)
#             self._init_error = msg
#             logger.error("❌ Failed to initialize Playwright: %s", msg)
#             raise RuntimeError(msg) from e

#     def _sync_initialize(self) -> None:
#         """Sync initialization (runs in executor thread)."""
#         self.playwright = sync_playwright().start()

#         # NOTE: Keep args minimal and stable across Windows + Linux
#         self.browser = self.playwright.chromium.launch(
#             headless=True,
#             args=[
#                 "--no-sandbox",
#                 "--disable-setuid-sandbox",
#                 "--disable-dev-shm-usage",
#                 "--disable-gpu",
#             ],
#         )

#     async def close(self) -> None:
#         """Close browser safely (called on shutdown)."""
#         if not self._initialized and not self.browser and not self.playwright:
#             return

#         import asyncio
#         loop = asyncio.get_running_loop()

#         def guarded_close():
#             with _init_lock:
#                 self._sync_close()
#                 self._initialized = False
#                 self._init_error = None

#         try:
#             await loop.run_in_executor(_executor, guarded_close)
#             logger.info("🔒 Playwright browser closed")
#         except Exception:
#             logger.exception("❌ Failed while closing Playwright (non-fatal)")

#     def _sync_close(self) -> None:
#         """Sync close (runs in executor thread)."""
#         try:
#             if self.browser:
#                 self.browser.close()
#         finally:
#             self.browser = None
#             if self.playwright:
#                 self.playwright.stop()
#             self.playwright = None

#     # ---------------------------
#     # Public capture API
#     # ---------------------------
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

#         fmt = (format or "png").lower().strip()
#         if fmt not in SUPPORTED_FORMATS:
#             raise ValueError(f"Unsupported format: {fmt}. Must be one of: {SUPPORTED_FORMATS}")

#         if not self._initialized:
#             await self.initialize()

#         import asyncio
#         loop = asyncio.get_running_loop()

#         return await loop.run_in_executor(
#             _executor,
#             self._sync_capture_screenshot,
#             url,
#             width,
#             height,
#             fmt,
#             full_page,
#             dark_mode,
#             wait_until,
#             timeout,
#         )

#     def _sync_capture_screenshot(
#         self,
#         url: str,
#         width: int,
#         height: int,
#         fmt: str,
#         full_page: bool,
#         dark_mode: bool,
#         wait_until: str,
#         timeout: int,
#     ) -> Dict[str, Any]:
#         """
#         Capture screenshot with intelligent retry logic for heavy websites.
        
#         ✅ CRITICAL FIX: Implements 3-tier fallback strategy
#         ✅ Works with heavy sites like Vogue, NYTimes, WSJ
        
#         Fallback Strategy:
#         1. Try with user-requested wait_until (default: networkidle)
#         2. If timeout, retry with 'domcontentloaded' (more lenient)
#         3. If still timeout, retry with 'load' (most lenient)
#         """

#         if not self.browser:
#             raise RuntimeError("Playwright browser is not initialized")

#         timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
#         random_id = secrets.token_hex(8)
#         filename = f"screenshot_{timestamp}_{random_id}.{fmt}"
#         filepath = SCREENSHOTS_DIR / filename

#         context = self.browser.new_context(
#             viewport={"width": int(width), "height": int(height)},
#             color_scheme="dark" if dark_mode else "light",
#             user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
#         )
#         page = context.new_page()

#         try:
#             logger.info("📸 Capturing screenshot: %s", url)
            
#             # ✅ CRITICAL FIX: Multi-tier retry strategy
#             page_loaded = False
#             last_error = None
            
#             # Strategy 1: Try with user-requested wait_until
#             try:
#                 logger.debug(f"Attempt 1: Using wait_until='{wait_until}', timeout={timeout}ms")
#                 page.goto(url, wait_until=wait_until, timeout=int(timeout))
#                 page.wait_for_load_state(wait_until, timeout=int(timeout))
#                 page_loaded = True
#                 logger.info(f"✅ Page loaded successfully with '{wait_until}' strategy")
                
#             except PlaywrightError as e:
#                 last_error = e
#                 error_str = str(e)
                
#                 if "Timeout" in error_str and wait_until == "networkidle":
#                     # Strategy 2: Retry with 'domcontentloaded' (more lenient)
#                     logger.warning(f"⏱️ Timeout with '{wait_until}', retrying with 'domcontentloaded'")
#                     try:
#                         page.goto(url, wait_until="domcontentloaded", timeout=FALLBACK_TIMEOUT)
#                         page.wait_for_load_state("domcontentloaded", timeout=FALLBACK_TIMEOUT)
#                         page_loaded = True
#                         logger.info("✅ Page loaded successfully with 'domcontentloaded' strategy")
#                     except PlaywrightError as e2:
#                         last_error = e2
#                         logger.warning(f"⏱️ Timeout with 'domcontentloaded', trying final fallback")
                        
#                         # Strategy 3: Final fallback with 'load' (most lenient)
#                         try:
#                             page.goto(url, wait_until="load", timeout=FALLBACK_TIMEOUT)
#                             page.wait_for_load_state("load", timeout=FALLBACK_TIMEOUT)
#                             page_loaded = True
#                             logger.info("✅ Page loaded successfully with 'load' strategy (fallback)")
#                         except PlaywrightError as e3:
#                             last_error = e3
#                             logger.error(f"❌ All retry strategies failed")
#                             # Let it raise below
                
#                 elif "Timeout" in error_str:
#                     # For non-networkidle timeouts, try 'load' directly
#                     logger.warning(f"⏱️ Timeout with '{wait_until}', retrying with 'load'")
#                     try:
#                         page.goto(url, wait_until="load", timeout=FALLBACK_TIMEOUT)
#                         page.wait_for_load_state("load", timeout=FALLBACK_TIMEOUT)
#                         page_loaded = True
#                         logger.info("✅ Page loaded successfully with 'load' strategy")
#                     except PlaywrightError as e2:
#                         last_error = e2
#                         # Let it raise below
#                 else:
#                     # Non-timeout error, re-raise immediately
#                     raise
            
#             # If page still didn't load after all retries, raise the last error
#             if not page_loaded and last_error:
#                 raise last_error

#             # ✅ Give page extra time to settle before screenshot
#             # This helps with dynamic content that loads after page.goto()
#             try:
#                 page.wait_for_timeout(2000)  # Wait 2 seconds for JS to settle
#                 logger.debug("✅ Extra settling time completed")
#             except Exception:
#                 pass  # Non-critical if this fails

#             # ✅ Take the screenshot
#             if fmt == "pdf":
#                 page.pdf(path=str(filepath), format="A4", print_background=True)
#             else:
#                 options: Dict[str, Any] = {"path": str(filepath), "full_page": bool(full_page)}
#                 if fmt in ("jpeg", "jpg"):
#                     options["type"] = "jpeg"
#                     options["quality"] = 90
#                 elif fmt == "png":
#                     options["type"] = "png"
#                 elif fmt == "webp":
#                     options["type"] = "webp"
#                 page.screenshot(**options)

#             file_size = filepath.stat().st_size
#             if file_size > MAX_FILE_SIZE:
#                 try:
#                     filepath.unlink(missing_ok=True)  # py3.8+ on windows ok
#                 except Exception:
#                     pass
#                 raise ValueError(f"Screenshot too large: {file_size} bytes (max: {MAX_FILE_SIZE})")

#             logger.info("✅ Screenshot saved: %s (%d bytes)", filename, file_size)

#             return {
#                 "filename": filename,
#                 "filepath": str(filepath),
#                 "url": url,
#                 "width": int(width),
#                 "height": int(height),
#                 "format": fmt,
#                 "full_page": bool(full_page),
#                 "dark_mode": bool(dark_mode),
#                 "file_size": int(file_size),
#                 "created_at": datetime.utcnow(),
#             }

#         except PlaywrightError as e:
#             logger.error("❌ Playwright error: %s", e)
            
#             # ✅ IMPROVED ERROR MESSAGES
#             error_msg = str(e)
#             if "Timeout" in error_msg:
#                 # Extract URL from error for better debugging
#                 url_hint = url[:50] + "..." if len(url) > 50 else url
#                 raise ValueError(
#                     f"Failed to capture screenshot: Page timeout after multiple retry attempts. "
#                     f"The website ({url_hint}) may be too slow or have continuous network activity. "
#                     f"Try again with a simpler page or contact support."
#                 ) from e
#             else:
#                 raise ValueError(f"Failed to capture screenshot: {error_msg}") from e

#         finally:
#             try:
#                 page.close()
#             except Exception:
#                 pass
#             try:
#                 context.close()
#             except Exception:
#                 pass

#     async def delete_screenshot(self, filename: str) -> bool:
#         filepath = SCREENSHOTS_DIR / filename
#         try:
#             if filepath.exists():
#                 filepath.unlink()
#                 logger.info("🗑️ Deleted screenshot: %s", filename)
#                 return True
#             return False
#         except Exception as e:
#             logger.error("❌ Failed to delete %s: %s", filename, e)
#             return False


# def _friendly_playwright_init_error(e: Exception) -> str:
#     s = str(e) or e.__class__.__name__

#     # Most common issue after installing playwright python package:
#     # browsers not installed: "Executable doesn't exist" or similar
#     lower = s.lower()
#     if "executable doesn't exist" in lower or "browser_type.launch" in lower or "playwright" in lower and "install" in lower:
#         return (
#             f"{s}\n\n"
#             "Playwright browsers may be missing.\n"
#             "Run:\n"
#             "  python -m playwright install chromium\n"
#         )

#     # Windows policy issues (just in case someone reverts run.py)
#     if "notimplementederror" in lower and "subprocess" in lower:
#         return (
#             f"{s}\n\n"
#             "Windows event loop policy does not support subprocesses.\n"
#             "Use WindowsProactorEventLoopPolicy() in run.py/main.py.\n"
#         )

#     return s


# # Singleton
# screenshot_service = ScreenshotService()


# # Convenience helpers (kept for compatibility)
# async def capture_screenshot_async(
#     url: str,
#     width: int = 1920,
#     height: int = 1080,
#     format: str = "png",
#     full_page: bool = False,
#     dark_mode: bool = False,
# ) -> Dict[str, Any]:
#     return await screenshot_service.capture_screenshot(
#         url=url,
#         width=width,
#         height=height,
#         format=format,
#         full_page=full_page,
#         dark_mode=dark_mode,
#     )


# def get_screenshot_url(filename: str, base_url: str = "") -> str:
#     if not base_url:
#         base_url = os.getenv("BACKEND_URL", "http://localhost:8000")
#     return f"{base_url.rstrip('/')}/screenshots/{filename}"


# # Usage tracking (kept)
# def increment_user_usage(user, db):
#     user.usage_screenshots = (user.usage_screenshots or 0) + 1
#     user.usage_api_calls = (user.usage_api_calls or 0) + 1
#     db.commit()
#     db.refresh(user)


# def check_usage_limit(user, tier_limits) -> bool:
#     limit = tier_limits.get("screenshots")
#     if limit == "unlimited":
#         return True
#     current_usage = user.usage_screenshots or 0
#     return current_usage < limit

# # ============================================================================
# # END OF screenshot_service.py
# # ============================================================================


