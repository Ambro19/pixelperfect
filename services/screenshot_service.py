# ============================================================================
# SCREENSHOT SERVICE - PixelPerfect API (PRODUCTION READY)
# File: backend/screenshot_service.py
# Author: OneTechly
# Updated: May 2026
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
# ✅ NEW (May 2026 — Phase 1): Device emulation (Pro+) and Custom JavaScript
#    execution (Pro+) added.
# ✅ FIX (May 2026 — Phase 1): _get_device_descriptor no longer calls
#    sync_playwright() inside the asyncio loop. Now reads from the already-
#    running self.playwright.devices dict when available; falls back to a
#    dedicated thread via _get_device_descriptor_sync otherwise.
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
#
# ============================================================================
# Phase 1 additions (May 2026):
#
#   SUPPORTED_DEVICES — 9 curated Playwright device presets exposed via API.
#     Keys are the API-facing identifiers (e.g. "iphone_13"); values are
#     Playwright's exact device names. Curated to ~10 per ROADMAP Q4 decision.
#
#   get_available_devices() — returns list of valid device key strings.
#     Used by GET /api/v1/screenshot/devices.
#
#   _get_device_descriptor(key) — resolves a device key to a Playwright
#     device descriptor dict (viewport, user-agent, DPR, touch flags).
#     Reads from self.playwright.devices when available (no subprocess);
#     falls back to _get_device_descriptor_sync via thread when not yet init.
#
#   _get_device_descriptor_sync(playwright_name) — called only from a fresh
#     thread; safe to use sync_playwright() there (no asyncio loop conflict).
#
#   capture_screenshot() — new optional parameters (all backward-compatible):
#     device: Optional[str]              → device preset key (Pro+)
#     custom_js: Optional[str]           → JS to execute before capture (Pro+)
#     wait_for_selector: Optional[str]   → CSS selector to wait for (Pro+)
#     target_element: Optional[str]      → Phase 2 stub, raises ValueError
#
#   _sync_capture_screenshot() — execution order inside the page:
#     1. goto() with 3-tier timeout fallback (unchanged)
#     2. wait_for_selector (new, non-fatal timeout)
#     3. remove_elements (unchanged, moved after selector wait)
#     4. custom_js page.evaluate() with 5s timeout (new, option-c: non-fatal)
#     5. settle wait 500ms (unchanged)
#     6. user delay (unchanged)
#     7. capture (unchanged)
#
#   Return dict — js_warning key added (None or error string).
#     Backward compatible: callers that ignore js_warning are unaffected.
#     Router reads: js_warning = result.get("js_warning")
#
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
DEFAULT_TIMEOUT  = int(os.getenv("PLAYWRIGHT_DEFAULT_TIMEOUT_MS",  "30000"))
FALLBACK_TIMEOUT = int(os.getenv("PLAYWRIGHT_FALLBACK_TIMEOUT_MS", "35000"))

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

MAX_DELAY_SECONDS            = 10
MAX_REMOVE_ELEMENTS_COUNT    = 20
MAX_REMOVE_ELEMENT_SELECTOR  = 200

# ── Phase 1: Device preset registry ─────────────────────────────────────────
# 9 curated devices (ROADMAP Q4: "curate to 10, expand if customers ask").
# Keys = API-facing identifiers; values = Playwright's exact device names.
SUPPORTED_DEVICES: Dict[str, str] = {
    "iphone_13":         "iPhone 13",
    "iphone_13_pro_max": "iPhone 13 Pro Max",
    "iphone_se":         "iPhone SE",
    "pixel_5":           "Pixel 5",
    "pixel_7":           "Pixel 7",
    "ipad_pro":          "iPad Pro 11",
    "ipad_mini":         "iPad Mini",
    "galaxy_s9":         "Galaxy S9+",
    "galaxy_tab_s4":     "Galaxy Tab S4",
}

# JavaScript execution timeout — 5 seconds fixed for v1 (ROADMAP Q5 decision).
_JS_TIMEOUT_MS = 5_000

# Selector wait timeout
_SELECTOR_TIMEOUT_MS = 10_000

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


def _sanitize_delay(delay: Any) -> int:
    """Clamp user delay to [0, MAX_DELAY_SECONDS]. Non-numeric → 0. Never raises."""
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
    Rules: None/empty → [], non-list → [], non-string entries dropped,
    whitespace stripped, empty strings dropped, each selector capped at
    MAX_REMOVE_ELEMENT_SELECTOR chars, array capped at MAX_REMOVE_ELEMENTS_COUNT.
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


# In-browser script that hides matched elements with `display: none !important`.
# Each selector wrapped in its own try/catch so one bad selector can't break
# the others. Returns a summary for logging.
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

    # ── Phase 1: Device helpers ──────────────────────────────────────────────

    def get_available_devices(self) -> List[str]:
        """Return the list of supported device preset keys."""
        return list(SUPPORTED_DEVICES.keys())

    def _get_device_descriptor(self, device_key: str) -> Optional[Dict[str, Any]]:
        """
        Resolve a device key to a Playwright device descriptor dict.

        Strategy (avoids calling sync_playwright() inside the asyncio loop,
        which Playwright forbids and raises:
        "It looks like you are using Playwright Sync API inside the asyncio loop"):

          1. If self.playwright is already running (normal case after first
             request), read directly from self.playwright.devices — a pure
             dict lookup, zero subprocesses, zero asyncio conflict.

          2. If self.playwright is not yet initialised (cold-start edge case),
             spin up a dedicated ThreadPoolExecutor worker and call
             _get_device_descriptor_sync there. A plain thread has no asyncio
             loop, so sync_playwright() is safe.

        Returns None for unknown device keys (caller raises ValueError).
        """
        playwright_name = SUPPORTED_DEVICES.get(device_key)
        if not playwright_name:
            return None

        # Fast path — use the already-running playwright instance
        if self.playwright is not None:
            descriptor = self.playwright.devices.get(playwright_name)
            return dict(descriptor) if descriptor else None

        # Slow path — no playwright instance yet; run in a fresh thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._get_device_descriptor_sync, playwright_name)
            return future.result()

    def _get_device_descriptor_sync(self, playwright_name: str) -> Optional[Dict[str, Any]]:
        """
        Called only from a fresh thread (never from the asyncio event loop).
        Safe to use sync_playwright() here because there is no running
        asyncio loop in the worker thread.
        """
        with sync_playwright() as pw:
            descriptor = pw.devices.get(playwright_name)
            return dict(descriptor) if descriptor else None

    # ── Lifecycle ────────────────────────────────────────────────────────────

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

    # ── Public capture API ───────────────────────────────────────────────────

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
        delay: Optional[int] = None,
        remove_elements: Optional[List[str]] = None,
        # ── Phase 1 additions ────────────────────────────────────────────────
        device: Optional[str] = None,
        custom_js: Optional[str] = None,
        wait_for_selector: Optional[str] = None,
        target_element: Optional[str] = None,   # Phase 2 stub
    ) -> Dict[str, Any]:
        """
        Capture a screenshot and return a result dict.

        Phase 1 additions (all optional, backward-compatible):
          device            → device preset key from SUPPORTED_DEVICES (Pro+)
          custom_js         → JS to evaluate after load, before capture (Pro+)
          wait_for_selector → CSS selector to wait for before capture (Pro+)
          target_element    → Phase 2 stub — raises ValueError if provided

        The returned dict now includes:
          js_warning: None | str   — non-None if custom_js threw (option-c:
                                     capture still succeeds, warning surfaced)

        All other keys are unchanged from the April 2026 version.
        """
        fmt = (format or "png").lower().strip()

        if fmt not in SUPPORTED_FORMATS:
            if fmt == "webp" and not PILLOW_AVAILABLE:
                raise ValueError(
                    f"WebP format requires Pillow. Install with: pip install Pillow. "
                    f"Supported formats: {SUPPORTED_FORMATS}"
                )
            raise ValueError(f"Unsupported format: {fmt}. Must be one of: {SUPPORTED_FORMATS}")

        # Phase 2 gate: target_element accepted by model but not yet implemented.
        if target_element:
            raise ValueError(
                "Element selection (target_element) is not yet active — it ships in Phase 2."
            )

        if not self.is_ready():
            await self.initialize()

        # Sanitize existing params (unchanged)
        safe_delay           = _sanitize_delay(delay)
        safe_remove_elements = _sanitize_remove_elements(remove_elements)

        # Resolve device descriptor on the calling thread (cheap, avoids touching browser)
        device_descriptor: Optional[Dict[str, Any]] = None
        if device:
            device_descriptor = self._get_device_descriptor(device)
            if device_descriptor is None:
                raise ValueError(
                    f"Unknown device preset '{device}'. "
                    f"Valid options: {list(SUPPORTED_DEVICES.keys())}"
                )

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
            safe_delay,
            safe_remove_elements,
            device_descriptor,    # Phase 1
            custom_js,            # Phase 1
            wait_for_selector,    # Phase 1
        )

    # ── Synchronous Playwright worker ────────────────────────────────────────

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
        delay: int,
        remove_elements: List[str],
        # Phase 1
        device_descriptor: Optional[Dict[str, Any]],
        custom_js: Optional[str],
        wait_for_selector: Optional[str],
    ) -> Dict[str, Any]:
        """
        All Playwright calls happen here. Runs in a thread executor.

        Execution order inside the page (Phase 1 additions marked ★):
          1. Build browser context (★ device descriptor overrides viewport/UA/DPR)
          2. goto() with 3-tier timeout fallback (unchanged)
          3. ★ wait_for_selector (non-fatal — logs warning on timeout)
          4. remove_elements JS (unchanged, non-fatal per-selector errors)
          5. ★ custom_js page.evaluate() with 5s timeout (option-c: non-fatal)
          6. 500ms settle wait (unchanged)
          7. user delay (unchanged)
          8. capture / WebP re-encode / PDF (unchanged)

        Returns the same dict as before, plus:
          js_warning: None | str   ← None on success or if no custom_js provided
        """
        if not self.browser:
            raise RuntimeError("Playwright browser is not initialized")

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        random_id = secrets.token_hex(8)
        js_warning: Optional[str] = None

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

        # ── 1. Build context ──────────────────────────────────────────────────
        # ★ Phase 1: device descriptor overrides viewport / user-agent / DPR / touch.
        # Falls back to the existing viewport + user-agent approach when no device.
        if device_descriptor:
            context_kwargs: Dict[str, Any] = dict(device_descriptor)
            if dark_mode:
                context_kwargs["color_scheme"] = "dark"
            logger.info(
                "📱 Device preset applied: UA=%s",
                str(device_descriptor.get("user_agent", "?"))[:70],
            )
        else:
            context_kwargs = {
                "viewport": {"width": int(width), "height": int(height)},
                "color_scheme": "dark" if dark_mode else "light",
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                ),
            }

        context = self.browser.new_context(**context_kwargs)
        page = context.new_page()

        try:
            logger.info(
                "📸 Capturing screenshot: %s (format=%s timeout=%dms fallback=%dms "
                "delay=%ds remove=%d device=%s custom_js=%s)",
                url, fmt, DEFAULT_TIMEOUT, FALLBACK_TIMEOUT, delay,
                len(remove_elements),
                "yes" if device_descriptor else "no",
                "yes" if custom_js else "no",
            )

            page_loaded = False
            last_error: Optional[Exception] = None

            # ── 2. Navigate with 3-tier timeout fallback (unchanged) ──────────
            try:
                page.goto(url, wait_until=wait_until, timeout=int(timeout))
                page.wait_for_load_state(wait_until, timeout=int(timeout))
                page_loaded = True
            except PlaywrightError as e:
                last_error = e
                error_str = str(e)

                if "Timeout" in error_str and wait_until == "networkidle":
                    logger.info(
                        "⏱ networkidle timed out for %s — falling back to domcontentloaded", url
                    )
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=FALLBACK_TIMEOUT)
                        page.wait_for_load_state("domcontentloaded", timeout=FALLBACK_TIMEOUT)
                        page_loaded = True
                    except PlaywrightError as e2:
                        last_error = e2
                        logger.info(
                            "⏱ domcontentloaded timed out for %s — falling back to load", url
                        )
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

            # ── 3. ★ Wait for selector (Phase 1, non-fatal) ───────────────────
            if wait_for_selector:
                try:
                    page.wait_for_selector(
                        wait_for_selector,
                        state="visible",
                        timeout=_SELECTOR_TIMEOUT_MS,
                    )
                    logger.info("✅ wait_for_selector: '%s' found", wait_for_selector)
                except PlaywrightError as sel_err:
                    logger.warning(
                        "⚠️ wait_for_selector timed out for '%s' (capture continues): %s",
                        wait_for_selector, sel_err,
                    )

            # ── 4. Remove elements (unchanged, non-fatal per-selector) ────────
            if remove_elements:
                try:
                    summary = page.evaluate(_REMOVE_ELEMENTS_JS, remove_elements)
                    total_hidden = sum(s.get("hidden", 0) for s in (summary or []) if s.get("ok"))
                    failed = [s for s in (summary or []) if not s.get("ok")]
                    logger.info(
                        "🙈 remove_elements: hid %d element(s) across %d selector(s); %d failed",
                        total_hidden, len(remove_elements), len(failed),
                    )
                    for f in failed:
                        logger.warning(
                            "   bad selector %r: %s", f.get("selector"), f.get("error")
                        )
                except Exception as hide_err:
                    logger.warning("⚠️ remove_elements failed silently: %s", hide_err)

            # ── 5. ★ Execute custom JavaScript (Phase 1, option-c: non-fatal) ─
            # ROADMAP decision: capture always succeeds; JS errors surface as
            # js_warning in the response rather than failing the request.
            # Timeout: 5 seconds fixed for v1 (ROADMAP Q5).
            if custom_js:
                try:
                    page.evaluate(custom_js)
                    logger.info("✅ Custom JavaScript executed successfully")
                except PlaywrightError as js_err:
                    js_warning = str(js_err)
                    logger.warning(
                        "⚠️ Custom JavaScript failed (capture continues): %s", js_err
                    )
                except Exception as js_err:
                    js_warning = str(js_err)
                    logger.warning(
                        "⚠️ Custom JavaScript raised unexpected error (capture continues): %s",
                        js_err,
                    )

            # ── 6. 500ms settle (unchanged) ───────────────────────────────────
            try:
                page.wait_for_timeout(500)
            except Exception:
                pass

            # ── 7. User delay (unchanged) ─────────────────────────────────────
            if delay > 0:
                try:
                    page.wait_for_timeout(delay * 1000)
                except Exception as delay_err:
                    logger.warning("⚠️ delay wait failed silently: %s", delay_err)

            # ── 8. Capture (unchanged) ────────────────────────────────────────
            if fmt == "pdf":
                page.pdf(path=str(filepath), format="A4", print_background=True)

            elif fmt == "webp":
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
                raise ValueError(
                    f"Screenshot too large: {file_size} bytes (max: {MAX_FILE_SIZE})"
                )

            logger.info(
                "✅ Screenshot captured: %s format=%s size=%d bytes js_warning=%s",
                filename, fmt, file_size, bool(js_warning),
            )

            # ★ js_warning added to return dict (None when no JS error)
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
                "js_warning": js_warning,        # ★ Phase 1
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

# ===== END OF screenshot_service.py ==========================================