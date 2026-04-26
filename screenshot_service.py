# ============================================================================
# SCREENSHOT SERVICE - PixelPerfect API (PRODUCTION READY)
# File: backend/screenshot_service.py
# Author: OneTechly
# Updated: April 2026
# ============================================================================
# Fixes in this version:
# ✅ is_ready() checks browser availability
# ✅ No db.commit() — caller controls transaction
# ✅ PLAYWRIGHT_BROWSERS_PATH-aware guidance
# ✅ 3-tier timeout fallback strategy (networkidle → domcontentloaded → load)
# ✅ WebP support via Pillow (PNG → WebP)
# ✅ Safer cleanup for temp files
# ✅ FIX (Mar 2026 v1): get_screenshot_url prefers CUSTOM_API_DOMAIN in prod
# ✅ FIX (Mar 2026 v2): get_screenshot_url is environment-aware
# ✅ FIX (Apr 2026): Playwright timeouts configurable via env vars.
# ✅ NEW (Apr 2026): `delay` and `remove_elements` parameters now honored.
#
#    Previously the frontend sent `delay` (seconds) and `remove_elements`
#    (CSS selector array) but the backend dropped them silently. This meant
#    users could set "wait 3 seconds" or "hide the cookie banner" and see
#    no effect in the resulting screenshot — a quiet bug.
#
#    Now:
#      - delay (0–10s, clamped)  → applied after page load, before capture
#      - remove_elements (≤20 selectors, each ≤200 chars)
#                                → applied after load, before delay, so
#                                   hidden elements stay hidden during any
#                                   wait time the user requested
#      - bad selectors never crash the capture — they're logged and skipped
#      - both parameters are optional and backward compatible
#
#    Note on timeout budget:
#      Render's Request Timeout = 120s. Playwright worst-case = 100s.
#      User delay (≤10s) eats into the 20s margin. Keep PLAYWRIGHT_*_TIMEOUT
#      env vars aligned if you ever raise the max delay above 10.
#
# ============================================================================
#
#    Root cause of production "Failed to fetch" on single screenshot capture:
#      Render's load balancer enforces a 30-second HTTP response timeout on
#      incoming connections. Heavy news sites (CNN, etc.) trigger Playwright's
#      networkidle fallback chain: 45s → 60s → 60s — far exceeding that limit.
#      Render closes the TCP connection; the browser receives a connection
#      reset and reports "Failed to fetch" (a network error, NOT an HTTP error).
#      Local dev is unaffected because uvicorn has no such connection timeout.
#
#    Two-part fix:
#      1. Render Dashboard → service → Settings → Request Timeout = 120s
#         (this is the primary fix — do this in the dashboard)
#
#      2. Playwright timeouts are now configurable via environment variables
#         so they can be tuned without code changes:
#
#         PLAYWRIGHT_DEFAULT_TIMEOUT_MS  (default: 30000 — 30 seconds)
#         PLAYWRIGHT_FALLBACK_TIMEOUT_MS (default: 35000 — 35 seconds)
#
#         Combined worst-case time with all three fallback tiers:
#           30s (tier 1) + 35s (tier 2) + 35s (tier 3) = 100s
#
#         With Render timeout set to 120s, this leaves a 20s safety margin.
#
#         OLD values: DEFAULT=45s, FALLBACK=60s → worst case 165s (always
#         exceeded Render's 30s default timeout → permanent "Failed to fetch")
#
#         To tune further without redeploying, update in Render env vars:
#           PLAYWRIGHT_DEFAULT_TIMEOUT_MS=25000
#           PLAYWRIGHT_FALLBACK_TIMEOUT_MS=30000
# ============================================================================

import os
import secrets
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List
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

# ✅ FIX: Configurable via env vars so production timeouts can be tuned
# without code changes or redeployment.
#
# Render's load balancer closes connections after its "Request Timeout" setting
# (default 30s on free, configurable on paid plans — set to 120s in dashboard).
# Keep these values low enough that the worst-case capture (all 3 tiers) still
# completes before the Render timeout fires.
#
# Worst case = DEFAULT + FALLBACK + FALLBACK = 30 + 35 + 35 = 100s
# With Render timeout = 120s → 20s safety margin ✓
DEFAULT_TIMEOUT  = int(os.getenv("PLAYWRIGHT_DEFAULT_TIMEOUT_MS",  "30000"))
FALLBACK_TIMEOUT = int(os.getenv("PLAYWRIGHT_FALLBACK_TIMEOUT_MS", "35000"))

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# ✅ NEW (Apr 2026): Hard limits for user-provided capture options.
#
# These exist to protect the server from abuse and to keep total capture
# time within the Render timeout budget. They match the ScreenshotRequest
# validation in screenshot_endpoints.py exactly — keep them in sync.
MAX_DELAY_SECONDS            = 10    # user delay clamped to 0–10s
MAX_REMOVE_ELEMENTS_COUNT    = 20    # selector array length
MAX_REMOVE_ELEMENT_SELECTOR  = 200   # chars per individual selector

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

logger.info(
    "📸 Playwright timeouts configured: DEFAULT=%dms FALLBACK=%dms "
    "(worst-case 3-tier=%dms) — set PLAYWRIGHT_DEFAULT_TIMEOUT_MS / "
    "PLAYWRIGHT_FALLBACK_TIMEOUT_MS env vars to tune without redeploying",
    DEFAULT_TIMEOUT, FALLBACK_TIMEOUT,
    DEFAULT_TIMEOUT + FALLBACK_TIMEOUT * 2,
)


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


# ✅ NEW (Apr 2026): Helpers for the new user-provided capture options.
# Kept as module-level helpers so they're easy to unit-test.

def _sanitize_delay(delay: Any) -> int:
    """
    Clamp user delay to [0, MAX_DELAY_SECONDS]. Non-numeric → 0.
    Never raises — bad input just becomes a no-op.
    """
    try:
        value = int(delay) if delay is not None else 0
    except (TypeError, ValueError):
        return 0
    if value < 0:
        return 0
    if value > MAX_DELAY_SECONDS:
        return MAX_DELAY_SECONDS
    return value


def _sanitize_remove_elements(selectors: Any) -> List[str]:
    """
    Normalize remove_elements into a clean, bounded list of CSS selector strings.

    Rules:
      - None or empty → []
      - Non-list → []
      - Non-string entries dropped
      - Whitespace stripped
      - Empty strings dropped
      - Each selector capped at MAX_REMOVE_ELEMENT_SELECTOR chars
      - Array capped at MAX_REMOVE_ELEMENTS_COUNT entries
    Never raises.
    """
    if not selectors or not isinstance(selectors, list):
        return []

    cleaned: List[str] = []
    for item in selectors:
        if not isinstance(item, str):
            continue
        stripped = item.strip()
        if not stripped:
            continue
        if len(stripped) > MAX_REMOVE_ELEMENT_SELECTOR:
            stripped = stripped[:MAX_REMOVE_ELEMENT_SELECTOR]
        cleaned.append(stripped)
        if len(cleaned) >= MAX_REMOVE_ELEMENTS_COUNT:
            break
    return cleaned


# In-browser script that hides matched elements with `display: none`.
# - Uses !important to override inline styles and site CSS specificity.
# - Each selector wrapped in its own try/catch so one bad selector can't
#   break the others.
# - Returns a summary so we can log how many elements were hidden per
#   selector (helps future debugging; cost is negligible).
_REMOVE_ELEMENTS_JS = """
(selectors) => {
  const summary = [];
  for (const selector of selectors) {
    try {
      const nodes = document.querySelectorAll(selector);
      let count = 0;
      nodes.forEach(el => {
        try {
          el.style.setProperty('display', 'none', 'important');
          count += 1;
        } catch (e) { /* ignore per-element errors */ }
      });
      summary.push({ selector, hidden: count, ok: true });
    } catch (e) {
      summary.push({ selector, hidden: 0, ok: false, error: String(e && e.message || e) });
    }
  }
  return summary;
}
"""


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
        delay: Optional[int] = None,                      # ✅ NEW (Apr 2026)
        remove_elements: Optional[List[str]] = None,      # ✅ NEW (Apr 2026)
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

        # ✅ Sanitize user-provided options up front. These never raise —
        # invalid input becomes a no-op, not a failed screenshot.
        safe_delay            = _sanitize_delay(delay)
        safe_remove_elements  = _sanitize_remove_elements(remove_elements)

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
            safe_delay,            # ✅ NEW
            safe_remove_elements,  # ✅ NEW
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
        delay: int,                         # ✅ NEW (pre-sanitized)
        remove_elements: List[str],         # ✅ NEW (pre-sanitized)
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
            logger.info(
                "📸 Capturing screenshot: %s (format=%s timeout=%dms fallback=%dms delay=%ds remove=%d)",
                url, fmt, DEFAULT_TIMEOUT, FALLBACK_TIMEOUT, delay, len(remove_elements),
            )

            page_loaded = False
            last_error: Optional[Exception] = None

            # ── Tier 1: networkidle (most accurate, slowest) ──────────────
            try:
                page.goto(url, wait_until=wait_until, timeout=int(timeout))
                page.wait_for_load_state(wait_until, timeout=int(timeout))
                page_loaded = True
            except PlaywrightError as e:
                last_error = e
                error_str = str(e)

                if "Timeout" in error_str and wait_until == "networkidle":
                    # ── Tier 2: domcontentloaded ──────────────────────────
                    logger.info("⏱ networkidle timed out for %s — falling back to domcontentloaded", url)
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=FALLBACK_TIMEOUT)
                        page.wait_for_load_state("domcontentloaded", timeout=FALLBACK_TIMEOUT)
                        page_loaded = True
                    except PlaywrightError as e2:
                        last_error = e2
                        # ── Tier 3: load ──────────────────────────────────
                        logger.info("⏱ domcontentloaded timed out for %s — falling back to load", url)
                        try:
                            page.goto(url, wait_until="load", timeout=FALLBACK_TIMEOUT)
                            page.wait_for_load_state("load", timeout=FALLBACK_TIMEOUT)
                            page_loaded = True
                        except PlaywrightError as e3:
                            last_error = e3

                elif "Timeout" in error_str:
                    logger.info("⏱ Timeout for %s — falling back to load", url)
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

            # ✅ NEW (Apr 2026): Hide user-specified elements BEFORE the delay.
            # This way, if the user is waiting 3 seconds for an animation to
            # settle, the banner they wanted hidden is already gone during
            # that wait — not flickering on screen at capture time.
            if remove_elements:
                try:
                    summary = page.evaluate(_REMOVE_ELEMENTS_JS, remove_elements)
                    total_hidden = sum(s.get("hidden", 0) for s in (summary or []) if s.get("ok"))
                    failed = [s for s in (summary or []) if not s.get("ok")]
                    logger.info(
                        "🙈 remove_elements: hid %d element(s) across %d selector(s); %d selector(s) failed",
                        total_hidden, len(remove_elements), len(failed),
                    )
                    if failed:
                        for f in failed:
                            logger.warning(
                                "   bad selector %r: %s", f.get("selector"), f.get("error")
                            )
                except Exception as hide_err:
                    # Never fail the screenshot because of a hide-elements issue
                    logger.warning("⚠️ remove_elements failed silently: %s", hide_err)

            # Brief settle time — kept short to avoid wasting Render budget
            try:
                page.wait_for_timeout(500)
            except Exception:
                pass

            # ✅ NEW (Apr 2026): User-requested delay BEFORE capture.
            # Already clamped to 0–10 seconds in _sanitize_delay().
            # Additive to the 500ms settle above.
            if delay > 0:
                try:
                    page.wait_for_timeout(delay * 1000)
                except Exception as delay_err:
                    logger.warning("⚠️ delay wait failed silently: %s", delay_err)

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

            logger.info(
                "✅ Screenshot captured: %s format=%s size=%d bytes",
                filename, fmt, file_size,
            )

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
            logger.error("❌ Playwright error capturing %s: %s", url, error_msg)

            if "Timeout" in error_msg:
                url_hint = url[:50] + "..." if len(url) > 50 else url
                raise ValueError(
                    "Screenshot timed out after all retry attempts. "
                    f"The website ({url_hint}) may be too slow or have continuous network activity. "
                    "Try increasing the delay or using a simpler URL."
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
# PRODUCTION  (ENVIRONMENT=production):
#   Use CUSTOM_API_DOMAIN (https://api.pixelperfectapi.net) — the public domain
#   where /screenshots/ is mounted and reachable by all clients.
#   Priority: CUSTOM_API_DOMAIN > BACKEND_URL > localhost fallback
#
# DEVELOPMENT (ENVIRONMENT=development, default):
#   Use BACKEND_URL (http://localhost:8000) — file is on local disk.
#   The frontend's resolveScreenshotUrl() rewrites localhost → LAN IP for mobile.
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


# # ======= END OF sreenshot_endpoints.py =====
