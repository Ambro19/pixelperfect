# ============================================================================
# SCREENSHOT SERVICE - PixelPerfect API (PRODUCTION READY)
# File: backend/screenshot_service.py
# Author: OneTechly
# Updated: March 2026
# ============================================================================
# Fixes in this version:
# ✅ Adds "is_ready()" that checks browser availability
# ✅ Prevents double-commit (no db.commit here)
# ✅ Better init error messages + preserves last error
# ✅ Uses PLAYWRIGHT_BROWSERS_PATH-aware guidance
# ✅ Keeps your 3-tier timeout fallback strategy
# ✅ WebP support via Pillow (PNG -> WebP)
# ✅ Safer cleanup for temp files
# ✅ FIX (Mar 2026 v1): get_screenshot_url prefers CUSTOM_API_DOMAIN over
#    BACKEND_URL in production, fixing mobile 404s on batch View links.
# ✅ FIX (Mar 2026 v2): get_screenshot_url is now ENVIRONMENT-AWARE.
#
#    Bug introduced by v1:
#      The v1 fix made get_screenshot_url ALWAYS prefer CUSTOM_API_DOMAIN,
#      even in local development. In local dev the backend saves screenshots
#      to the local backend/screenshots/ folder, but the stored URL pointed
#      to api.pixelperfectapi.net — where those files don't exist → 404.
#
#    Correct behaviour:
#      ENVIRONMENT=production  → use CUSTOM_API_DOMAIN (api.pixelperfectapi.net)
#                                 Files on Render are served from there. ✓
#      ENVIRONMENT=development → use BACKEND_URL (http://localhost:8000)
#                                 Files are local; the frontend's
#                                 resolveScreenshotUrl() already rewrites
#                                 localhost → LAN IP for mobile. ✓
#
#    Priority per environment:
#      production:   CUSTOM_API_DOMAIN > BACKEND_URL > localhost fallback
#      development:  BACKEND_URL > localhost fallback
#                    (CUSTOM_API_DOMAIN intentionally skipped in dev)
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

DEFAULT_TIMEOUT = 45_000
FALLBACK_TIMEOUT = 60_000
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Pillow for WebP conversion (optional dependency)
try:
    from PIL import Image  # type: ignore
    PILLOW_AVAILABLE = True
    SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "webp", "pdf"]
    logger.info("✅ Pillow available - WebP format enabled")
except Exception:
    PILLOW_AVAILABLE = False
    SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "pdf"]
    logger.warning("⚠️ Pillow not available - WebP format disabled")

_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="playwright")
_init_lock = threading.Lock()


def _playwright_install_hint() -> str:
    return (
        "Playwright browsers may be missing.\n"
        "If using Render (non-Docker): add a Build Command:\n"
        "  python -m playwright install chromium\n"
        "If using Docker: ensure your Dockerfile runs:\n"
        "  python -m playwright install --with-deps chromium\n"
        "Then redeploy."
    )


def _friendly_playwright_init_error(e: Exception) -> str:
    s = str(e) or e.__class__.__name__
    lower = s.lower()

    if "executable doesn't exist" in lower or "looks like playwright was just installed" in lower:
        return f"{s}\n\n{_playwright_install_hint()}"

    if "notimplementederror" in lower and "subprocess" in lower:
        return (
            f"{s}\n\n"
            "Windows event loop policy does not support subprocesses.\n"
            "Use WindowsProactorEventLoopPolicy() in run.py/main.py.\n"
        )

    return s


class ScreenshotService:
    def __init__(self):
        self.browser: Optional[Browser] = None
        self.playwright = None
        self._initialized = False
        self._init_error: Optional[str] = None

    def is_ready(self) -> bool:
        """True only when Playwright + browser are ready."""
        return bool(self._initialized and self.browser and not self._init_error)

    def last_error(self) -> Optional[str]:
        return self._init_error

    async def initialize(self) -> None:
        """Initialize Playwright browser (safe to call multiple times)."""
        if self.is_ready():
            return

        if self._init_error:
            raise RuntimeError(self._init_error)

        import asyncio
        loop = asyncio.get_running_loop()

        def guarded_init():
            with _init_lock:
                if self.is_ready():
                    return
                self._sync_initialize()
                self._initialized = True
                self._init_error = None

        try:
            await loop.run_in_executor(_executor, guarded_init)
            logger.info("✅ Playwright browser initialized (sync mode)")
        except Exception as e:
            msg = _friendly_playwright_init_error(e)
            self._init_error = msg
            self._initialized = False
            logger.error("❌ Failed to initialize Playwright: %s", msg)
            raise RuntimeError(msg) from e

    def _sync_initialize(self) -> None:
        self.playwright = sync_playwright().start()
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
        try:
            if self.browser:
                self.browser.close()
        finally:
            self.browser = None
            if self.playwright:
                self.playwright.stop()
            self.playwright = None

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
            if fmt == "webp" and not PILLOW_AVAILABLE:
                raise ValueError(
                    f"WebP format requires Pillow. Install with: pip install Pillow. "
                    f"Supported formats: {SUPPORTED_FORMATS}"
                )
            raise ValueError(f"Unsupported format: {fmt}. Must be one of: {SUPPORTED_FORMATS}")

        if not self.is_ready():
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

        temp_filepath: Optional[Path] = None

        if fmt == "webp":
            if not PILLOW_AVAILABLE:
                raise ValueError("WebP requested but Pillow is not installed.")
            temp_filename = f"screenshot_{timestamp}_{random_id}.png"
            final_filename = f"screenshot_{timestamp}_{random_id}.webp"
            temp_filepath = SCREENSHOTS_DIR / temp_filename
            filepath = SCREENSHOTS_DIR / final_filename
            filename = final_filename
        else:
            filename = f"screenshot_{timestamp}_{random_id}.{fmt}"
            filepath = SCREENSHOTS_DIR / filename

        context = self.browser.new_context(
            viewport={"width": int(width), "height": int(height)},
            color_scheme="dark" if dark_mode else "light",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        page = context.new_page()

        try:
            logger.info("📸 Capturing screenshot: %s (format: %s)", url, fmt)

            page_loaded = False
            last_error: Optional[Exception] = None

            try:
                page.goto(url, wait_until=wait_until, timeout=int(timeout))
                page.wait_for_load_state(wait_until, timeout=int(timeout))
                page_loaded = True
            except PlaywrightError as e:
                last_error = e
                error_str = str(e)

                if "Timeout" in error_str and wait_until == "networkidle":
                    # fallback 1
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=FALLBACK_TIMEOUT)
                        page.wait_for_load_state("domcontentloaded", timeout=FALLBACK_TIMEOUT)
                        page_loaded = True
                    except PlaywrightError as e2:
                        last_error = e2
                        # fallback 2
                        try:
                            page.goto(url, wait_until="load", timeout=FALLBACK_TIMEOUT)
                            page.wait_for_load_state("load", timeout=FALLBACK_TIMEOUT)
                            page_loaded = True
                        except PlaywrightError as e3:
                            last_error = e3

                elif "Timeout" in error_str:
                    try:
                        page.goto(url, wait_until="load", timeout=FALLBACK_TIMEOUT)
                        page.wait_for_load_state("load", timeout=FALLBACK_TIMEOUT)
                        page_loaded = True
                    except PlaywrightError as e2:
                        last_error = e2
                else:
                    raise

            if not page_loaded and last_error:
                raise last_error

            try:
                page.wait_for_timeout(2000)
            except Exception:
                pass

            if fmt == "pdf":
                page.pdf(path=str(filepath), format="A4", print_background=True)

            elif fmt == "webp":
                # Capture PNG then convert to WebP via Pillow
                page.screenshot(path=str(temp_filepath), full_page=bool(full_page), type="png")
                img = Image.open(str(temp_filepath))
                img.save(str(filepath), "WEBP", quality=90, method=6)

            else:
                options: Dict[str, Any] = {"path": str(filepath), "full_page": bool(full_page)}
                if fmt in ("jpeg", "jpg"):
                    options["type"] = "jpeg"
                    options["quality"] = 90
                else:
                    options["type"] = "png"
                page.screenshot(**options)

            # Cleanup temp PNG used during WebP conversion
            if temp_filepath:
                try:
                    temp_filepath.unlink(missing_ok=True)
                except Exception:
                    pass

            file_size = filepath.stat().st_size
            if file_size > MAX_FILE_SIZE:
                try:
                    filepath.unlink(missing_ok=True)
                except Exception:
                    pass
                raise ValueError(f"Screenshot too large: {file_size} bytes (max: {MAX_FILE_SIZE})")

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
            error_msg = str(e)
            logger.error("❌ Playwright error: %s", error_msg)

            if "Timeout" in error_msg:
                url_hint = url[:50] + "..." if len(url) > 50 else url
                raise ValueError(
                    "Failed to capture screenshot: Page timeout after multiple retry attempts. "
                    f"The website ({url_hint}) may be too slow or have continuous network activity."
                ) from e

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


# Singleton
screenshot_service = ScreenshotService()


# ✅ FIX (Mar 2026 v2): Environment-aware screenshot URL resolution.
#
# The full call chain that must be correct:
#   capture (backend) → get_screenshot_url() → stored in DB + JOBS dict
#       ↓ (polled by BatchJobs.js)
#   resolveScreenshotUrl(item.screenshot_url) in BatchJobs.js
#       ↓
#   Browser opens the URL → the file must exist on THAT host
#
# PRODUCTION  (ENVIRONMENT=production):
#   Screenshots live on Render's disk, served via CustomStaticFiles at
#   api.pixelperfectapi.net/screenshots/. Must use CUSTOM_API_DOMAIN.
#   Priority: CUSTOM_API_DOMAIN > BACKEND_URL > localhost fallback
#
# DEVELOPMENT (ENVIRONMENT=development, the default):
#   Screenshots live on the LOCAL backend's disk (backend/screenshots/).
#   CUSTOM_API_DOMAIN must NOT be used — the production server has no
#   copy of the locally-captured file → 404.
#   Use BACKEND_URL (typically http://localhost:8000).
#   The frontend's resolveScreenshotUrl() already handles the
#   localhost → LAN-IP rewrite for mobile browsers, so no change needed
#   in BatchJobs.js for this case.
#   Priority: BACKEND_URL > localhost fallback
#             (CUSTOM_API_DOMAIN intentionally skipped in dev)
#
def get_screenshot_url(filename: str, base_url: str = "") -> str:
    if not base_url:
        environment = os.getenv("ENVIRONMENT", "development").lower()
        is_prod = environment == "production"

        if is_prod:
            base_url = (
                os.getenv("CUSTOM_API_DOMAIN") or
                os.getenv("BACKEND_URL") or
                "http://localhost:8000"
            ).strip().rstrip("/")
        else:
            # Development: use local server. CUSTOM_API_DOMAIN skipped intentionally.
            base_url = (
                os.getenv("BACKEND_URL") or
                "http://localhost:8000"
            ).strip().rstrip("/")

    return f"{base_url.rstrip('/')}/screenshots/{filename}"


# IMPORTANT: NO db.commit() here. Caller controls the transaction.
def increment_user_usage(user) -> None:
    user.usage_screenshots = (user.usage_screenshots or 0) + 1
    user.usage_api_calls = (user.usage_api_calls or 0) + 1


def check_usage_limit(user, tier_limits) -> bool:
    limit = tier_limits.get("screenshots")
    if limit == "unlimited":
        return True
    current_usage = user.usage_screenshots or 0
    return current_usage < limit


# # ===============================================
# # ============================================================================
# # SCREENSHOT SERVICE - PixelPerfect API (PRODUCTION READY)
# # File: backend/screenshot_service.py
# # Author: OneTechly
# # Updated: March 2026
# # ============================================================================
# # Fixes in this version:
# # ✅ Adds "is_ready()" that checks browser availability
# # ✅ Prevents double-commit (no db.commit here)
# # ✅ Better init error messages + preserves last error
# # ✅ Uses PLAYWRIGHT_BROWSERS_PATH-aware guidance
# # ✅ Keeps your 3-tier timeout fallback strategy
# # ✅ WebP support via Pillow (PNG -> WebP)
# # ✅ Safer cleanup for temp files
# # ✅ FIX (Mar 2026): get_screenshot_url now prefers CUSTOM_API_DOMAIN over
# #    BACKEND_URL so batch job screenshot links always resolve to the correct
# #    public-facing URL (https://api.pixelperfectapi.net/screenshots/...).
# #
# #    Root cause of the mobile 404:
# #      BACKEND_URL is typically the internal Render deploy URL
# #      (e.g. https://pixelperfect-backend-l5dn.onrender.com).
# #      That URL starts with "https://" so BatchJobs.js → resolveScreenshotUrl
# #      returned it unchanged.  The browser then hit the Render URL directly,
# #      which either routed incorrectly through Cloudflare or the file didn't
# #      exist on that host → {"detail":"Not Found"}.
# #
# #    Fix: prioritise CUSTOM_API_DOMAIN (https://api.pixelperfectapi.net) which
# #    is the domain where /screenshots/ is actually mounted and served.
# #    BACKEND_URL is kept as a fallback; localhost:8000 is the last resort.
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

# DEFAULT_TIMEOUT = 45_000
# FALLBACK_TIMEOUT = 60_000
# MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# # Pillow for WebP conversion (optional dependency)
# try:
#     from PIL import Image  # type: ignore
#     PILLOW_AVAILABLE = True
#     SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "webp", "pdf"]
#     logger.info("✅ Pillow available - WebP format enabled")
# except Exception:
#     PILLOW_AVAILABLE = False
#     SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "pdf"]
#     logger.warning("⚠️ Pillow not available - WebP format disabled")

# _executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="playwright")
# _init_lock = threading.Lock()


# def _playwright_install_hint() -> str:
#     return (
#         "Playwright browsers may be missing.\n"
#         "If using Render (non-Docker): add a Build Command:\n"
#         "  python -m playwright install chromium\n"
#         "If using Docker: ensure your Dockerfile runs:\n"
#         "  python -m playwright install --with-deps chromium\n"
#         "Then redeploy."
#     )


# def _friendly_playwright_init_error(e: Exception) -> str:
#     s = str(e) or e.__class__.__name__
#     lower = s.lower()

#     if "executable doesn't exist" in lower or "looks like playwright was just installed" in lower:
#         return f"{s}\n\n{_playwright_install_hint()}"

#     if "notimplementederror" in lower and "subprocess" in lower:
#         return (
#             f"{s}\n\n"
#             "Windows event loop policy does not support subprocesses.\n"
#             "Use WindowsProactorEventLoopPolicy() in run.py/main.py.\n"
#         )

#     return s


# class ScreenshotService:
#     def __init__(self):
#         self.browser: Optional[Browser] = None
#         self.playwright = None
#         self._initialized = False
#         self._init_error: Optional[str] = None

#     def is_ready(self) -> bool:
#         """
#         True only when Playwright + browser are ready.
#         This is the check endpoints should use.
#         """
#         return bool(self._initialized and self.browser and not self._init_error)

#     def last_error(self) -> Optional[str]:
#         return self._init_error

#     async def initialize(self) -> None:
#         """
#         Initialize Playwright browser (safe to call multiple times).
#         If it fails, stores error and raises RuntimeError.
#         """
#         if self.is_ready():
#             return

#         if self._init_error:
#             raise RuntimeError(self._init_error)

#         import asyncio
#         loop = asyncio.get_running_loop()

#         def guarded_init():
#             with _init_lock:
#                 if self.is_ready():
#                     return
#                 self._sync_initialize()
#                 self._initialized = True
#                 self._init_error = None

#         try:
#             await loop.run_in_executor(_executor, guarded_init)
#             logger.info("✅ Playwright browser initialized (sync mode)")
#         except Exception as e:
#             msg = _friendly_playwright_init_error(e)
#             self._init_error = msg
#             self._initialized = False
#             logger.error("❌ Failed to initialize Playwright: %s", msg)
#             raise RuntimeError(msg) from e

#     def _sync_initialize(self) -> None:
#         self.playwright = sync_playwright().start()
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
#         try:
#             if self.browser:
#                 self.browser.close()
#         finally:
#             self.browser = None
#             if self.playwright:
#                 self.playwright.stop()
#             self.playwright = None

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
#             if fmt == "webp" and not PILLOW_AVAILABLE:
#                 raise ValueError(
#                     f"WebP format requires Pillow. Install with: pip install Pillow. "
#                     f"Supported formats: {SUPPORTED_FORMATS}"
#                 )
#             raise ValueError(f"Unsupported format: {fmt}. Must be one of: {SUPPORTED_FORMATS}")

#         if not self.is_ready():
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

#         if not self.browser:
#             raise RuntimeError("Playwright browser is not initialized")

#         timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
#         random_id = secrets.token_hex(8)

#         temp_filepath: Optional[Path] = None

#         if fmt == "webp":
#             if not PILLOW_AVAILABLE:
#                 raise ValueError("WebP requested but Pillow is not installed.")
#             temp_filename = f"screenshot_{timestamp}_{random_id}.png"
#             final_filename = f"screenshot_{timestamp}_{random_id}.webp"
#             temp_filepath = SCREENSHOTS_DIR / temp_filename
#             filepath = SCREENSHOTS_DIR / final_filename
#             filename = final_filename
#         else:
#             filename = f"screenshot_{timestamp}_{random_id}.{fmt}"
#             filepath = SCREENSHOTS_DIR / filename

#         context = self.browser.new_context(
#             viewport={"width": int(width), "height": int(height)},
#             color_scheme="dark" if dark_mode else "light",
#             user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
#         )
#         page = context.new_page()

#         try:
#             logger.info("📸 Capturing screenshot: %s (format: %s)", url, fmt)

#             page_loaded = False
#             last_error: Optional[Exception] = None

#             try:
#                 page.goto(url, wait_until=wait_until, timeout=int(timeout))
#                 page.wait_for_load_state(wait_until, timeout=int(timeout))
#                 page_loaded = True
#             except PlaywrightError as e:
#                 last_error = e
#                 error_str = str(e)

#                 if "Timeout" in error_str and wait_until == "networkidle":
#                     # fallback 1
#                     try:
#                         page.goto(url, wait_until="domcontentloaded", timeout=FALLBACK_TIMEOUT)
#                         page.wait_for_load_state("domcontentloaded", timeout=FALLBACK_TIMEOUT)
#                         page_loaded = True
#                     except PlaywrightError as e2:
#                         last_error = e2
#                         # fallback 2
#                         try:
#                             page.goto(url, wait_until="load", timeout=FALLBACK_TIMEOUT)
#                             page.wait_for_load_state("load", timeout=FALLBACK_TIMEOUT)
#                             page_loaded = True
#                         except PlaywrightError as e3:
#                             last_error = e3

#                 elif "Timeout" in error_str:
#                     try:
#                         page.goto(url, wait_until="load", timeout=FALLBACK_TIMEOUT)
#                         page.wait_for_load_state("load", timeout=FALLBACK_TIMEOUT)
#                         page_loaded = True
#                     except PlaywrightError as e2:
#                         last_error = e2
#                 else:
#                     raise

#             if not page_loaded and last_error:
#                 raise last_error

#             try:
#                 page.wait_for_timeout(2000)
#             except Exception:
#                 pass

#             if fmt == "pdf":
#                 page.pdf(path=str(filepath), format="A4", print_background=True)

#             elif fmt == "webp":
#                 # Capture PNG then convert to WebP via Pillow
#                 page.screenshot(path=str(temp_filepath), full_page=bool(full_page), type="png")
#                 img = Image.open(str(temp_filepath))
#                 img.save(str(filepath), "WEBP", quality=90, method=6)

#             else:
#                 options: Dict[str, Any] = {"path": str(filepath), "full_page": bool(full_page)}
#                 if fmt in ("jpeg", "jpg"):
#                     options["type"] = "jpeg"
#                     options["quality"] = 90
#                 else:
#                     options["type"] = "png"
#                 page.screenshot(**options)

#             # Cleanup temp PNG used during WebP conversion
#             if temp_filepath:
#                 try:
#                     temp_filepath.unlink(missing_ok=True)
#                 except Exception:
#                     pass

#             file_size = filepath.stat().st_size
#             if file_size > MAX_FILE_SIZE:
#                 try:
#                     filepath.unlink(missing_ok=True)
#                 except Exception:
#                     pass
#                 raise ValueError(f"Screenshot too large: {file_size} bytes (max: {MAX_FILE_SIZE})")

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
#             error_msg = str(e)
#             logger.error("❌ Playwright error: %s", error_msg)

#             if "Timeout" in error_msg:
#                 url_hint = url[:50] + "..." if len(url) > 50 else url
#                 raise ValueError(
#                     "Failed to capture screenshot: Page timeout after multiple retry attempts. "
#                     f"The website ({url_hint}) may be too slow or have continuous network activity."
#                 ) from e

#             raise ValueError(f"Failed to capture screenshot: {error_msg}") from e

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


# # Singleton
# screenshot_service = ScreenshotService()


# # ✅ FIX (Mar 2026): Prefer CUSTOM_API_DOMAIN over BACKEND_URL.
# #
# # Why this matters:
# #   BACKEND_URL is typically the internal Render deploy URL
# #   (e.g. https://pixelperfect-backend-l5dn.onrender.com).
# #   Since it starts with "https://", the frontend's resolveScreenshotUrl()
# #   passes it through unchanged, so the browser ends up requesting a URL
# #   on the wrong host → {"detail": "Not Found"}.
# #
# #   CUSTOM_API_DOMAIN=https://api.pixelperfectapi.net is the public domain
# #   where /screenshots/ is actually mounted and reachable by all clients,
# #   including mobile browsers — so it must be used here.
# #
# # Priority: CUSTOM_API_DOMAIN > BACKEND_URL > localhost fallback
# def get_screenshot_url(filename: str, base_url: str = "") -> str:
#     if not base_url:
#         base_url = (
#             os.getenv("CUSTOM_API_DOMAIN") or
#             os.getenv("BACKEND_URL") or
#             "http://localhost:8000"
#         ).strip().rstrip("/")
#     return f"{base_url.rstrip('/')}/screenshots/{filename}"


# # IMPORTANT: NO db.commit() here. Caller controls the transaction.
# def increment_user_usage(user) -> None:
#     user.usage_screenshots = (user.usage_screenshots or 0) + 1
#     user.usage_api_calls = (user.usage_api_calls or 0) + 1


# def check_usage_limit(user, tier_limits) -> bool:
#     limit = tier_limits.get("screenshots")
#     if limit == "unlimited":
#         return True
#     current_usage = user.usage_screenshots or 0
#     return current_usage < limit

