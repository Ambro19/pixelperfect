# ⚠️⚠️  THERE ARE TWO MODULES NAMED screenshot_service IN THIS CODEBASE  ⚠️⚠️
#
#     backend/screenshot_service.py            <-- THIS FILE
#         Imported by: screenshot_endpoints.py, batch.py
#         Serves:      legacy single-capture path + batch processing
#
#     backend/services/screenshot_service.py   <-- the other one
#         Imported by: routers/screenshot.py
#         Serves:      ALL production single captures
#         That is the hot path. Patch there FIRST.
#
# A fix applied to one is NOT applied to the other. RULE: patch BOTH, or
# neither. Never one.

# ============================================================================
# SCREENSHOT SERVICE - PixelPerfect API (PRODUCTION READY)
# File: backend/screenshot_service.py
# Author: OneTechly
# Updated: August 2026
# ============================================================================
# Fixes in this version:
# ✅ is_ready() checks browser availability
# ✅ No db.commit() — caller controls transaction
# ✅ PLAYWRIGHT_BROWSERS_PATH-aware guidance
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
# ✅ NEW (May 2026 — Phase 2): Element Selection (Business+) implemented.
#
# ✅ REWRITTEN (Aug 2026 — heavy-site navigation):
#    PATCH A — new navigation constants, tracker block list, lazy-scroll script.
#    PATCH B — single-navigation block replacing the 3-tier re-navigation.
#              Trackers are blocked before goto(); networkidle is now an
#              optimisation rather than a requirement.
#    PATCH C — lazy-load scrolling before full-page and element captures.
#              Applies to full_page=True and to target_element (which always
#              captures full-page before cropping). Non-fatal and bounded by
#              LAZY_SCROLL_TIMEOUT_MS; the page default timeout is restored
#              afterwards so it cannot leak into page.screenshot().
#
# ✅ FIX (Aug 2026 — boot failure, NameError at import):
#    LAZY_SCROLL_TIMEOUT_MS was commented out when CAPTURE_TIMEOUT_MS was
#    added. The module-level startup banner references it, so the module
#    raised NameError at IMPORT time — the app died at boot rather than at
#    first capture, and Render crash-looped. Restored below.
#
# ✅ NEW (Aug 2026 — phase-aware timeout reporting):
#    Every Playwright Timeout used to produce the same user-facing message —
#    "the website did not respond in time" — whether the site was slow or OUR
#    renderer was. A full-page render of a very tall document exceeding the
#    capture budget was reported as the site's fault, sending people to check
#    something that had already answered fine. `phase` is set at the top of
#    each step and read by the exception handler, so the log and the message
#    now name the step that actually ran out of budget.
#
#    ⚠️ `wait_until` and `timeout` are now DEAD PARAMETERS on both
#       capture_screenshot() and _sync_capture_screenshot(). Navigation uses
#       NAV_TIMEOUT_MS / COMMIT_TIMEOUT_MS with fixed wait conditions. Callers
#       passing wait_until="networkidle" or a custom timeout are silently
#       ignored. See the note in capture_screenshot's docstring.
#
# ============================================================================
# Phase 2 additions (May 2026):
#
#   target_element: Optional[str]  — CSS selector (Business+)
#
#   Implementation strategy: Option A — bounding box crop.
#
#     1. Capture a full-page PNG of the entire document (Playwright).
#     2. Use Pillow to resolve the element's bounding box via
#        page.evaluate() (returns {x, y, width, height} in CSS pixels).
#     3. Scale the bounding box by deviceScaleFactor (DPR) to get
#        physical pixel coordinates that match the screenshot.
#     4. Crop with Image.crop() and save to the final output path.
#
#   Why Option A over Playwright's built-in element.screenshot():
#     - Works even when the element is partially off-screen or below the fold.
#     - No dependency on element handle validity after DOM mutations.
#     - Handles zero-size edge cases gracefully (raises ValueError with a
#       clear message rather than silently producing an empty file).
#     - Consistent output: the crop always comes from the same full render,
#       so relative positions (sticky headers, overlays) are preserved.
#
#   Error handling:
#     - Element not found  → ValueError (HTTP 400 via router)
#     - Zero-size element  → ValueError (HTTP 400 via router)
#     - Crop succeeds      → result dict includes element_selector key
#
#   Temp file lifecycle:
#     - Full-page PNG written to SCREENSHOTS_DIR as a temp file.
#     - Cropped output written to final filepath.
#     - Temp full-page PNG deleted in the finally block regardless of
#       success or failure — no orphan files left on disk.
#
#   Backward compatibility:
#     - target_element=None (default) → existing behaviour unchanged.
#     - result dict gains one new key: element_selector (None or str).
#       Callers that ignore it are unaffected.
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
#      2. PLAYWRIGHT_DEFAULT_TIMEOUT_MS / PLAYWRIGHT_FALLBACK_TIMEOUT_MS
#         env vars (defaults: 30000ms / 35000ms).
#         Combined worst-case: 30s + 35s + 35s = 100s (20s margin).
#
#      ⚠️ The 100s figure above counts navigation only. It omits the matching
#         wait_for_load_state() call at each tier and the user delay, which is
#         why real-world worst case reached ~210s. Superseded by the Aug 2026
#         rewrite — kept here as the historical record.
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

# ✅ REWRITTEN (Aug 2026 — heavy-site navigation)
#
# The old design called page.goto() up to three times, re-downloading and
# re-parsing the entire document at each "fallback" tier. Worst case was
# 210s against Render's 120s request timeout, so heavy pages were killed
# mid-flight and surfaced as a network error rather than a clean timeout.
#
# New design: navigate ONCE with a condition that reliably fires, then give
# the page a short bounded window to settle. Every budget below is additive
# and the total is well inside 120s.
DEFAULT_TIMEOUT  = int(os.getenv("PLAYWRIGHT_DEFAULT_TIMEOUT_MS",  "30000"))   # legacy, still read
FALLBACK_TIMEOUT = int(os.getenv("PLAYWRIGHT_FALLBACK_TIMEOUT_MS", "35000"))   # legacy, still read

# Primary navigation. domcontentloaded fires as soon as the HTML is parsed —
# it does not wait for images, fonts, or async scripts, so it is reliable
# even on pages that never go idle.
NAV_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_NAV_TIMEOUT_MS", "25000"))

# Last-resort navigation. "commit" resolves as soon as response headers
# arrive — before any parsing. Almost nothing fails this.
COMMIT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_COMMIT_TIMEOUT_MS", "15000"))

# Optional settle window. We ASK for networkidle and accept not getting it.
# This is the key change: idle is now an optimisation, not a requirement.
SETTLE_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_SETTLE_TIMEOUT_MS", "8000"))

# Budget for auto-scrolling a full-page capture to trigger lazy-loaded content.
# ⚠️ RESTORED (Aug 2026). This line was commented out when CAPTURE_TIMEOUT_MS
# was added below it. The module never imported again, because the startup
# banner references it at MODULE level — so the app died at boot with
# NameError rather than at first capture. Do not comment this out again.
LAZY_SCROLL_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_LAZY_SCROLL_TIMEOUT_MS", "6000"))

# ✅ NEW (Aug 2026): explicit budget for the capture itself.
#
# page.screenshot() and page.pdf() take no timeout argument in our calls, so
# they inherited the page default — which the lazy-scroll finally block sets
# to NAV_TIMEOUT_MS (25s). That is a navigation number, not a rendering one.
# A full-page capture of a very tall document (gnu.org's GPL text renders
# ~25,000px at 1920 wide) exceeds 25s on a 1-CPU instance, and the resulting
# Playwright Timeout was reported to the user as "the website did not respond
# in time" — blaming a site that had already responded perfectly.
#
# 60s keeps the worst case inside Render's window:
#   nav 25 + commit 15 + settle 8 + scroll 6 + delay 10 + capture 60 = 124s
# ...which is why CAPTURE is only reached when the earlier phases were fast.
# In practice a page that navigates in 2s and captures in 40s totals ~50s.
CAPTURE_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_CAPTURE_TIMEOUT_MS", "60000"))

# Third-party hosts that keep the network busy indefinitely and almost never
# affect the visual result. Blocking these is the single biggest reason a
# heavy page can reach networkidle at all.
# Set PLAYWRIGHT_BLOCK_TRACKERS=0 to disable if a customer needs ads captured.
BLOCK_TRACKERS = os.getenv("PLAYWRIGHT_BLOCK_TRACKERS", "1") != "0"

_TRACKER_PATTERNS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "googlesyndication.com", "google-adsense", "adservice.google",
    "facebook.net", "connect.facebook", "hotjar.com", "segment.io",
    "segment.com", "mixpanel.com", "amplitude.com", "fullstory.com",
    "intercom.io", "clarity.ms", "newrelic.com", "nr-data.net",
    "sentry.io", "bugsnag.com", "optimizely.com", "criteo.",
    "taboola.com", "outbrain.com", "scorecardresearch.com",
    "quantserve.com", "adsrvr.org", "pubmatic.com", "rubiconproject.com",
)

# Auto-scroll script for lazy-loaded content. Steps down the page in viewport
# increments, then returns to the top so the capture starts from the origin.
_LAZY_SCROLL_JS = """
async () => {
  await new Promise((resolve) => {
    let total = 0;
    const step = Math.max(200, Math.floor(window.innerHeight * 0.85));
    const timer = setInterval(() => {
      const height = document.body.scrollHeight;
      window.scrollBy(0, step);
      total += step;
      // Stop at the bottom, or after 50 steps as a hard guard against
      // infinite-scroll pages that grow faster than we scroll.
      if (total >= height || total > step * 50) {
        clearInterval(timer);
        window.scrollTo(0, 0);
        resolve();
      }
    }, 90);
  });
}
"""

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

MAX_DELAY_SECONDS            = 10
MAX_REMOVE_ELEMENTS_COUNT    = 20
MAX_REMOVE_ELEMENT_SELECTOR  = 200

# ── Phase 1: Device preset registry ─────────────────────────────────────────
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

_JS_TIMEOUT_MS      = 5_000
_SELECTOR_TIMEOUT_MS = 10_000

# Pillow — required for WebP (Phase 1) and element crop (Phase 2)
try:
    from PIL import Image  # type: ignore
    PILLOW_AVAILABLE = True
    SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "webp", "pdf"]
    logger.info("✅ Pillow available - WebP format enabled")
except Exception:
    PILLOW_AVAILABLE = False
    SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "pdf"]
    logger.warning("⚠️ Pillow not available - WebP format disabled")

_executor  = ThreadPoolExecutor(max_workers=3, thread_name_prefix="playwright")
_init_lock = threading.Lock()

# ✅ UPDATED (Aug 2026): the old banner advertised a "worst-case 3-tier"
# figure derived from DEFAULT_TIMEOUT + FALLBACK_TIMEOUT * 2. Once Patch B
# lands that number describes code that no longer exists, so the banner now
# reports the new additive budget instead of a stale one.
logger.info(
    "📸 Playwright navigation budget: nav=%dms commit=%dms settle=%dms "
    "lazy_scroll=%dms capture=%dms (worst-case=%dms) | block_trackers=%s | "
    "legacy DEFAULT=%dms FALLBACK=%dms",
    NAV_TIMEOUT_MS, COMMIT_TIMEOUT_MS, SETTLE_TIMEOUT_MS, LAZY_SCROLL_TIMEOUT_MS,
    CAPTURE_TIMEOUT_MS,
    NAV_TIMEOUT_MS + COMMIT_TIMEOUT_MS + SETTLE_TIMEOUT_MS
    + LAZY_SCROLL_TIMEOUT_MS + CAPTURE_TIMEOUT_MS,
    BLOCK_TRACKERS,
    DEFAULT_TIMEOUT, FALLBACK_TIMEOUT,
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
    try:
        value = int(delay) if delay is not None else 0
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, MAX_DELAY_SECONDS))


def _sanitize_remove_elements(selectors: Any) -> List[str]:
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

# ── Phase 2: Bounding box resolution script ──────────────────────────────────
# Returns null when the selector matches nothing, or an object with
# {x, y, width, height} in CSS pixels (not physical pixels).
# We multiply by deviceScaleFactor when cropping.
_ELEMENT_BBOX_JS = """
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return null;
  const rect = el.getBoundingClientRect();
  return {
    x:      rect.left + window.scrollX,
    y:      rect.top  + window.scrollY,
    width:  rect.width,
    height: rect.height
  };
}
"""


class ScreenshotService:
    def __init__(self):
        self.browser: Optional[Browser] = None
        self.playwright = None
        self._initialized = False
        self._init_error: Optional[str] = None

    def is_ready(self) -> bool:
        return bool(self._initialized and self.browser and not self._init_error)

    def last_error(self) -> Optional[str]:
        return self._init_error

    # ── Phase 1: Device helpers ──────────────────────────────────────────────

    def get_available_devices(self) -> List[str]:
        return list(SUPPORTED_DEVICES.keys())

    def _get_device_descriptor(self, device_key: str) -> Optional[Dict[str, Any]]:
        """
        Resolve device key to Playwright descriptor dict.
        Fast path: reads self.playwright.devices (already running, no subprocess).
        Slow path: spins a fresh thread so sync_playwright() is safe outside
        the asyncio loop.
        """
        playwright_name = SUPPORTED_DEVICES.get(device_key)
        if not playwright_name:
            return None
        if self.playwright is not None:
            descriptor = self.playwright.devices.get(playwright_name)
            return dict(descriptor) if descriptor else None
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._get_device_descriptor_sync, playwright_name)
            return future.result()

    def _get_device_descriptor_sync(self, playwright_name: str) -> Optional[Dict[str, Any]]:
        with sync_playwright() as pw:
            descriptor = pw.devices.get(playwright_name)
            return dict(descriptor) if descriptor else None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
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
        # Phase 1
        device: Optional[str] = None,
        custom_js: Optional[str] = None,
        wait_for_selector: Optional[str] = None,
        # Phase 2
        target_element: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Capture a screenshot and return a result dict.

        Phase 2 addition:
          target_element: Optional[str]
            CSS selector for the element to crop to (Business+).
            The full page is captured first, then Pillow crops to the
            element's bounding box.  Raises ValueError when:
              - Pillow is not installed
              - The selector matches no element
              - The matched element has zero width or height

        Returns the same dict as Phase 1, plus:
          element_selector: None | str   ← selector used, or None if not provided
        """
        fmt = (format or "png").lower().strip()

        if fmt not in SUPPORTED_FORMATS:
            if fmt == "webp" and not PILLOW_AVAILABLE:
                raise ValueError(
                    f"WebP format requires Pillow. Install with: pip install Pillow. "
                    f"Supported formats: {SUPPORTED_FORMATS}"
                )
            raise ValueError(f"Unsupported format: {fmt}. Must be one of: {SUPPORTED_FORMATS}")

        # Phase 2 gate — Pillow is required for the bounding-box crop
        if target_element and not PILLOW_AVAILABLE:
            raise ValueError(
                "Element selection requires Pillow. Install with: pip install Pillow."
            )

        if not self.is_ready():
            await self.initialize()

        safe_delay           = _sanitize_delay(delay)
        safe_remove_elements = _sanitize_remove_elements(remove_elements)

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
            target_element,       # Phase 2
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
        # Phase 2
        target_element: Optional[str],
    ) -> Dict[str, Any]:
        """
        All Playwright calls happen here. Runs in a thread executor.

        Execution order inside the page:
          1. Build browser context (device descriptor overrides viewport/UA/DPR)
          2. block trackers, navigate ONCE (domcontentloaded → commit),
             then bounded networkidle settle (non-fatal)
          3. wait_for_selector (non-fatal)
          4. remove_elements JS (non-fatal per-selector)
          5. custom_js page.evaluate() (option-c: non-fatal)
          6. 500ms settle wait
          7. user delay
         7b. lazy-load scroll pass (full_page or target_element, non-fatal)
          8. [Phase 2] resolve target_element bounding box via page.evaluate()
          9. capture full-page PNG (always full-page when target_element is set)
         10. [Phase 2] Pillow crop to bounding box → save to final filepath
         11. WebP re-encode or PDF (unchanged for non-element captures)
         12. Temp file cleanup

        Returns the Phase 1 dict plus:
          element_selector: None | str
        """
        if not self.browser:
            raise RuntimeError("Playwright browser is not initialized")

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        random_id = secrets.token_hex(8)
        js_warning: Optional[str] = None

        # ── File path planning ────────────────────────────────────────────────
        # Three possible pipelines:
        #   A) target_element → full_temp.png → crop → final.(fmt)
        #   B) webp           → temp.png      → webp-encode → final.webp
        #   C) normal         → final.(fmt)   directly

        temp_filepath: Optional[Path] = None   # always cleaned up in finally

        if target_element:
            # Pipeline A: always capture PNG full-page, then crop with Pillow.
            # The final output can still be any supported format.
            temp_filename  = f"screenshot_{timestamp}_{random_id}_full.png"
            temp_filepath  = SCREENSHOTS_DIR / temp_filename
            if fmt == "webp":
                final_filename = f"screenshot_{timestamp}_{random_id}.webp"
            elif fmt in ("jpeg", "jpg"):
                final_filename = f"screenshot_{timestamp}_{random_id}.jpg"
            elif fmt == "pdf":
                # PDF from a cropped region is not well-defined; fall back to PNG.
                logger.warning(
                    "⚠️ target_element + pdf is not supported — falling back to PNG"
                )
                fmt = "png"
                final_filename = f"screenshot_{timestamp}_{random_id}.png"
            else:
                final_filename = f"screenshot_{timestamp}_{random_id}.{fmt}"
            filepath = SCREENSHOTS_DIR / final_filename
            filename = final_filename

        elif fmt == "webp":
            # Pipeline B: existing WebP flow
            temp_filename  = f"screenshot_{timestamp}_{random_id}.png"
            final_filename = f"screenshot_{timestamp}_{random_id}.webp"
            temp_filepath  = SCREENSHOTS_DIR / temp_filename
            filepath       = SCREENSHOTS_DIR / final_filename
            filename       = final_filename

        else:
            # Pipeline C: direct capture
            filename = f"screenshot_{timestamp}_{random_id}.{fmt}"
            filepath = SCREENSHOTS_DIR / filename

        # ── 1. Build browser context ──────────────────────────────────────────
        # ✅ FIX (Aug 2026): record the viewport ACTUALLY applied. A device
        # descriptor carries its own viewport, so the width/height arguments
        # never reach the browser — but they were still what got returned.
        actual_viewport_width   = int(width)
        actual_viewport_height  = int(height)
        device_scale_factor_ctx = 1.0

        if device_descriptor:
            _ALLOWED_CONTEXT_KEYS = {
                "viewport", "user_agent", "device_scale_factor",
                "is_mobile", "has_touch", "color_scheme", "locale",
                "timezone_id", "screen",
            }
            context_kwargs: Dict[str, Any] = {
                k: v for k, v in device_descriptor.items()
                if k in _ALLOWED_CONTEXT_KEYS
            }
            vp = device_descriptor.get("viewport") or {}
            if vp.get("width") and vp.get("height"):
                actual_viewport_width  = int(vp["width"])
                actual_viewport_height = int(vp["height"])
            device_scale_factor_ctx = float(
                device_descriptor.get("device_scale_factor", 1) or 1
            )
            if dark_mode:
                context_kwargs["color_scheme"] = "dark"
            logger.info(
                "📱 Device preset applied: viewport=%dx%d dpr=%.2f UA=%s",
                actual_viewport_width, actual_viewport_height,
                device_scale_factor_ctx,
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
        page    = context.new_page()

        # ✅ NEW (Aug 2026 — Patch B): which step are we in?
        #
        # The except handler below reports timeouts to the user. Without this,
        # every Playwright Timeout produced the same message — "the website did
        # not respond in time" — regardless of whether the site was slow or OUR
        # renderer was. That sent people to check a site that had answered fine.
        #
        # Set at the top of each phase; read by the handler.
        phase = "setup"

        try:
            # ✅ UPDATED (Aug 2026 — Patch B): logged DEFAULT_TIMEOUT and
            # FALLBACK_TIMEOUT, neither of which the navigation block reads any
            # more. Now reports the budgets actually in force for this capture.
            logger.info(
                "📸 Capturing screenshot: %s (format=%s nav=%dms commit=%dms "
                "settle=%dms capture=%dms delay=%ds remove=%d device=%s "
                "custom_js=%s target_element=%s)",
                url, fmt, NAV_TIMEOUT_MS, COMMIT_TIMEOUT_MS, SETTLE_TIMEOUT_MS,
                CAPTURE_TIMEOUT_MS, delay,
                len(remove_elements),
                "yes" if device_descriptor else "no",
                "yes" if custom_js else "no",
                repr(target_element) if target_element else "no",
            )

            # ── 2. Navigate ONCE, then settle ─────────────────────────────────
            # ✅ REWRITTEN (Aug 2026). The previous implementation called
            # page.goto() up to three times, re-downloading the document at
            # each "fallback" tier — 210s worst case against a 120s platform
            # timeout. Heavy pages (gnu.org's GPL text, news sites) were
            # killed mid-reload and surfaced as "Failed to fetch".
            #
            # Now: one navigation with a condition that reliably fires, then
            # an optional bounded settle. networkidle is requested, not
            # required.
            #
            # Block trackers BEFORE navigating. Ad and analytics requests are
            # the main reason networkidle never fires, and they almost never
            # change what the page looks like.
            if BLOCK_TRACKERS:
                def _route_filter(route):
                    try:
                        req_url = route.request.url
                        if any(pat in req_url for pat in _TRACKER_PATTERNS):
                            return route.abort()
                        return route.continue_()
                    except Exception:
                        # Never let routing kill a capture.
                        try:
                            return route.continue_()
                        except Exception:
                            return None

                try:
                    page.route("**/*", _route_filter)
                except Exception as route_err:
                    logger.warning("⚠️ Tracker blocking unavailable (non-fatal): %s", route_err)

            phase = "navigation"          # ✅ Patch B
            page_loaded = False
            last_error: Optional[Exception] = None

            # Attempt 1 — domcontentloaded. Fires once the HTML is parsed;
            # does not wait on images, fonts, or async scripts.
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                page_loaded = True
            except PlaywrightError as e:
                last_error = e
                if "Timeout" not in str(e):
                    # A real navigation error (DNS, refused, cert) — surface it
                    # immediately rather than burning the retry budget.
                    raise
                logger.info(
                    "⏱ domcontentloaded timed out after %dms — retrying with commit",
                    NAV_TIMEOUT_MS,
                )

            # Attempt 2 — commit. Resolves as soon as response headers arrive,
            # before parsing. This is the floor: if this fails, the site is
            # genuinely unreachable.
            if not page_loaded:
                try:
                    page.goto(url, wait_until="commit", timeout=COMMIT_TIMEOUT_MS)
                    page_loaded = True
                    logger.info("✅ Navigated with commit (page still loading)")
                except PlaywrightError as e2:
                    last_error = e2

            if not page_loaded:
                raise last_error or PlaywrightError("Navigation failed")

            # Settle — ask for quiet, accept not getting it. On a page that
            # never idles this costs SETTLE_TIMEOUT_MS and then continues,
            # instead of failing the whole capture.
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
                logger.info("✅ Page reached networkidle")
            except PlaywrightError:
                logger.info(
                    "⏱ Page never went idle within %dms — capturing anyway "
                    "(expected on ad-heavy and live-updating sites)",
                    SETTLE_TIMEOUT_MS,
                )

            # ── 3. Wait for selector (Phase 1, non-fatal) ─────────────────────
            if wait_for_selector:
                try:
                    page.wait_for_selector(
                        wait_for_selector, state="visible", timeout=_SELECTOR_TIMEOUT_MS
                    )
                    logger.info("✅ wait_for_selector: '%s' found", wait_for_selector)
                except PlaywrightError as sel_err:
                    logger.warning(
                        "⚠️ wait_for_selector timed out for '%s' (capture continues): %s",
                        wait_for_selector, sel_err,
                    )

            # ── 4. Remove elements (Phase 1, non-fatal) ───────────────────────
            if remove_elements:
                try:
                    summary     = page.evaluate(_REMOVE_ELEMENTS_JS, remove_elements)
                    total_hidden = sum(
                        s.get("hidden", 0) for s in (summary or []) if s.get("ok")
                    )
                    failed = [s for s in (summary or []) if not s.get("ok")]
                    logger.info(
                        "🙈 remove_elements: hid %d element(s) across %d selector(s); %d failed",
                        total_hidden, len(remove_elements), len(failed),
                    )
                    for f in failed:
                        logger.warning("   bad selector %r: %s", f.get("selector"), f.get("error"))
                except Exception as hide_err:
                    logger.warning("⚠️ remove_elements failed silently: %s", hide_err)

            # ── 5. Custom JavaScript (Phase 1, option-c: non-fatal) ───────────
            if custom_js:
                try:
                    page.evaluate(custom_js)
                    logger.info("✅ Custom JavaScript executed successfully")
                except PlaywrightError as js_err:
                    js_warning = str(js_err)
                    logger.warning("⚠️ Custom JavaScript failed (capture continues): %s", js_err)
                except Exception as js_err:
                    js_warning = str(js_err)
                    logger.warning(
                        "⚠️ Custom JavaScript raised unexpected error (capture continues): %s",
                        js_err,
                    )

            # ── 6. 500ms settle ───────────────────────────────────────────────
            try:
                page.wait_for_timeout(500)
            except Exception:
                pass

            # ── 7. User delay ─────────────────────────────────────────────────
            if delay > 0:
                try:
                    page.wait_for_timeout(delay * 1000)
                except Exception as delay_err:
                    logger.warning("⚠️ delay wait failed silently: %s", delay_err)

            # ── 7b. Lazy-load pass (full-page captures only) ──────────────────
            # ✅ NEW (Aug 2026). Because we no longer wait for networkidle,
            # images that load on scroll may not have started. Stepping down
            # the page triggers them, then we return to the top so the capture
            # begins at the origin. Bounded and non-fatal.
            #
            # ⚠️ set_default_timeout() mutates the page for EVERY subsequent
            # call, and Playwright exposes no getter to read the old value.
            # Left unrestored, the 6s scroll budget would silently become the
            # timeout for page.screenshot() further down. Step 9 now passes an
            # explicit timeout=CAPTURE_TIMEOUT_MS, so this is belt and braces —
            # but the restore stays, because page.pdf() takes no timeout
            # argument and would still inherit whatever is left here.
            if full_page or target_element:
                phase = "lazy_scroll"     # ✅ Patch B
                try:
                    page.set_default_timeout(LAZY_SCROLL_TIMEOUT_MS)
                    page.evaluate(_LAZY_SCROLL_JS)
                    # Brief pause for triggered images to actually arrive.
                    page.wait_for_timeout(600)
                    logger.info("📜 Lazy-load scroll pass complete")
                except Exception as scroll_err:
                    logger.warning(
                        "⚠️ Lazy-load scroll failed (capture continues): %s", scroll_err
                    )
                finally:
                    try:
                        page.set_default_timeout(CAPTURE_TIMEOUT_MS)
                    except Exception:
                        pass

            # ── 8. Phase 2: resolve element bounding box ──────────────────────
            # Done after all page manipulation so the box reflects the final
            # DOM state (post-JS, post-element-removal, post-delay).
            element_bbox: Optional[Dict[str, float]] = None
            device_scale_factor: float = 1.0

            if target_element:
                phase = "element_lookup"   # ✅ Patch B
                bbox_result = page.evaluate(_ELEMENT_BBOX_JS, target_element)

                if bbox_result is None:
                    raise ValueError(
                        f"Element not found: no element matched the selector "
                        f"'{target_element}'. Check that the selector is correct "
                        f"and the element exists on the page at capture time."
                    )

                el_w = float(bbox_result.get("width",  0))
                el_h = float(bbox_result.get("height", 0))

                if el_w <= 0 or el_h <= 0:
                    raise ValueError(
                        f"Element '{target_element}' was found but has zero size "
                        f"({el_w}×{el_h}px). The element may be hidden or collapsed. "
                        f"Use remove_elements or custom_js to ensure it is visible "
                        f"before capturing."
                    )

                element_bbox = bbox_result

                # Get the device pixel ratio so we can scale CSS → physical pixels.
                # devicePixelRatio is always 1 for desktop captures; devices set it
                # to their DPR (e.g. 3 for iPhone 13).
                try:
                    device_scale_factor = float(
                        page.evaluate("() => window.devicePixelRatio || 1")
                    )
                except Exception:
                    device_scale_factor = 1.0

                logger.info(
                    "🎯 target_element '%s': bbox=(%s,%s,%s,%s) dpr=%.2f",
                    target_element,
                    bbox_result["x"], bbox_result["y"],
                    bbox_result["width"], bbox_result["height"],
                    device_scale_factor,
                )

            # ── 9. Capture ────────────────────────────────────────────────────
            # ✅ Patch B: everything below is rendering, not loading. A timeout
            # here is OUR renderer running out of budget on a very tall page,
            # not the website failing to respond.
            phase = "capture"

            if target_element:
                # Always capture full-page PNG first, then crop.
                page.screenshot(
                    path=str(temp_filepath), full_page=True, type="png",
                    timeout=CAPTURE_TIMEOUT_MS,
                )

            elif fmt == "pdf":
                # page.pdf() accepts no timeout argument in the sync API. It
                # inherits the page default, which the lazy-scroll finally
                # block now restores to CAPTURE_TIMEOUT_MS rather than
                # NAV_TIMEOUT_MS.
                page.pdf(
                    path=str(filepath), format="A4", print_background=True,
                )

            elif fmt == "webp":
                page.screenshot(
                    path=str(temp_filepath), full_page=bool(full_page), type="png",
                    timeout=CAPTURE_TIMEOUT_MS,
                )

            else:
                options: Dict[str, Any] = {
                    "path": str(filepath),
                    "full_page": bool(full_page),
                    "timeout": CAPTURE_TIMEOUT_MS,
                }
                if fmt in ("jpeg", "jpg"):
                    options["type"] = "jpeg"
                    options["quality"] = 90
                else:
                    options["type"] = "png"
                page.screenshot(**options)

            # ── 10. Phase 2: Pillow crop ──────────────────────────────────────
            if target_element and element_bbox is not None:
                phase = "crop"            # ✅ Patch B
                dpr = device_scale_factor
                x      = int(element_bbox["x"]      * dpr)
                y      = int(element_bbox["y"]      * dpr)
                x2     = int((element_bbox["x"] + element_bbox["width"])  * dpr)
                y2     = int((element_bbox["y"] + element_bbox["height"]) * dpr)

                full_img = Image.open(str(temp_filepath))
                img_w, img_h = full_img.size

                # Clamp to image bounds — handles elements partially off-screen
                x  = max(0, min(x,  img_w))
                y  = max(0, min(y,  img_h))
                x2 = max(0, min(x2, img_w))
                y2 = max(0, min(y2, img_h))

                if x2 <= x or y2 <= y:
                    raise ValueError(
                        f"Element '{target_element}' bounding box is entirely "
                        f"outside the captured image bounds. "
                        f"Try using full_page=true or scrolling to the element "
                        f"via custom_js before capturing."
                    )

                cropped = full_img.crop((x, y, x2, y2))

                if fmt == "webp":
                    cropped.save(str(filepath), "WEBP", quality=90, method=6)
                elif fmt in ("jpeg", "jpg"):
                    cropped = cropped.convert("RGB")   # strip alpha for JPEG
                    cropped.save(str(filepath), "JPEG", quality=90)
                else:
                    cropped.save(str(filepath), "PNG")

                logger.info(
                    "✂️ Element crop: (%d,%d)→(%d,%d) = %d×%d physical px",
                    x, y, x2, y2, x2 - x, y2 - y,
                )

            # ── 11. WebP re-encode (non-element pipeline) ─────────────────────
            elif fmt == "webp" and temp_filepath and temp_filepath.exists():
                phase = "encode"          # ✅ Patch B
                img = Image.open(str(temp_filepath))
                img.save(str(filepath), "WEBP", quality=90, method=6)

            # ── 12. Validate output file size ─────────────────────────────────
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
                "✅ Screenshot captured: %s format=%s size=%d bytes "
                "js_warning=%s element=%s",
                filename, fmt, file_size,
                bool(js_warning),
                repr(target_element) if target_element else "no",
            )

            return {
                "filename":         filename,
                "filepath":         str(filepath),
                "url":              url,
                "width":            actual_viewport_width,
                "height":           actual_viewport_height,
                "viewport_width":   actual_viewport_width,
                "viewport_height":  actual_viewport_height,
                "requested_width":  int(width),
                "requested_height": int(height),
                "device_scale_factor": device_scale_factor_ctx,
                "format":           fmt,
                "full_page":        bool(full_page),
                "dark_mode":        bool(dark_mode),
                "file_size":        int(file_size),
                "created_at":       datetime.utcnow(),
                "js_warning":       js_warning,         # Phase 1
                "element_selector": target_element,     # Phase 2 (None if unused)
            }

        except PlaywrightError as e:
            error_msg = str(e)
            # ✅ NEW (Aug 2026 — Patch B): name the step that failed.
            # Previously every Playwright Timeout produced the same message,
            # so a slow full-page RENDER was reported as the website failing
            # to respond — which sent people to check a site that had already
            # answered fine.
            logger.error(
                "❌ Playwright error during %s for %s: %s", phase, url, error_msg
            )
            if "Timeout" in error_msg:
                url_hint = url[:50] + "..." if len(url) > 50 else url

                if phase == "capture":
                    raise ValueError(
                        f"The page loaded, but rendering the screenshot took longer "
                        f"than {CAPTURE_TIMEOUT_MS // 1000}s. This usually means the "
                        f"page is very tall — try unchecking 'Capture full page', or "
                        f"capture a more specific section of the site."
                    ) from e

                raise ValueError(
                    f"The website ({url_hint}) did not respond in time. It may be "
                    f"very slow, blocking automated access, or continuously loading "
                    f"content. Try again, or capture a more specific page."
                ) from e

            raise ValueError(f"Failed to capture screenshot: {error_msg}") from e

        finally:
            # Always clean up the temp full-page PNG regardless of success/failure
            if temp_filepath and temp_filepath.exists():
                try:
                    temp_filepath.unlink(missing_ok=True)
                except Exception:
                    pass
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


def increment_user_usage(user) -> None:
    user.usage_screenshots = (user.usage_screenshots or 0) + 1
    user.usage_api_calls   = (user.usage_api_calls   or 0) + 1


def check_usage_limit(user, tier_limits, db=None) -> bool:
    """
    ✅ FIX (Aug 2026): prefer the period-scoped count from usage_accounting.

    user.usage_screenshots is a running counter that does not reset at the
    period boundary and does not account for tombstoned deletions, so it
    drifts from what /subscription_status shows the user. When a db session
    is available, use the authoritative source.
    """
    limit = tier_limits.get("screenshots")
    if limit == "unlimited" or limit is None:
        return True

    try:
        limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        # Unrecognised limit value — fail open rather than blocking a paying
        # customer on a config typo, but make it loud.
        # OverflowError covers float("inf"), which some tier maps use as the
        # unlimited sentinel instead of the string; int(inf) raises it, and
        # it is NOT a subclass of ValueError.
        logger.error("check_usage_limit: unparseable limit %r — allowing", limit)
        return True

    if db is not None:
        try:
            from usage_accounting import screenshots_used_this_period
            # Pass the User, not the id — the period boundary depends on the
            # Stripe billing anchor.
            return screenshots_used_this_period(db, user) < limit
        except Exception as e:
            logger.warning(
                "check_usage_limit: usage_accounting unavailable, "
                "falling back to user.usage_screenshots (%s)", e
            )
    else:
        # Only warn about a missing session when one really was missing —
        # the fallback above already logged its own reason.
        logger.warning(
            "check_usage_limit called without db — using non-authoritative counter"
        )

    return int(user.usage_screenshots or 0) < limit

# ===== END OF backend\screenshot_service.py ========




# # ⚠️⚠️  THERE ARE TWO MODULES NAMED screenshot_service IN THIS CODEBASE  ⚠️⚠️
# #
# #     backend/screenshot_service.py            <-- THIS FILE
# #         Imported by: screenshot_endpoints.py, batch.py
# #         Serves:      legacy single-capture path + batch processing
# #
# #     backend/services/screenshot_service.py   <-- the other one
# #         Imported by: routers/screenshot.py
# #         Serves:      ALL production single captures
# #         That is the hot path. Patch there FIRST.
# #
# # A fix applied to one is NOT applied to the other. RULE: patch BOTH, or
# # neither. Never one.

# # ============================================================================
# # SCREENSHOT SERVICE - PixelPerfect API (PRODUCTION READY)
# # File: backend/screenshot_service.py
# # Author: OneTechly
# # Updated: August 2026
# # ============================================================================
# # Fixes in this version:
# # ✅ is_ready() checks browser availability
# # ✅ No db.commit() — caller controls transaction
# # ✅ PLAYWRIGHT_BROWSERS_PATH-aware guidance
# # ✅ WebP support via Pillow (PNG → WebP)
# # ✅ Safer cleanup for temp files
# # ✅ FIX (Mar 2026 v1): get_screenshot_url prefers CUSTOM_API_DOMAIN in prod
# # ✅ FIX (Mar 2026 v2): get_screenshot_url is environment-aware
# # ✅ FIX (Apr 2026): Playwright timeouts configurable via env vars.
# # ✅ NEW (Apr 2026): `delay` and `remove_elements` parameters now honored.
# # ✅ NEW (May 2026 — Phase 1): Device emulation (Pro+) and Custom JavaScript
# #    execution (Pro+) added.
# # ✅ FIX (May 2026 — Phase 1): _get_device_descriptor no longer calls
# #    sync_playwright() inside the asyncio loop. Now reads from the already-
# #    running self.playwright.devices dict when available; falls back to a
# #    dedicated thread via _get_device_descriptor_sync otherwise.
# # ✅ NEW (May 2026 — Phase 2): Element Selection (Business+) implemented.
# #
# # ✅ REWRITTEN (Aug 2026 — heavy-site navigation):
# #    PATCH A — new navigation constants, tracker block list, lazy-scroll script.
# #    PATCH B — single-navigation block replacing the 3-tier re-navigation.
# #              Trackers are blocked before goto(); networkidle is now an
# #              optimisation rather than a requirement.
# #    PATCH C — lazy-load scrolling before full-page and element captures.
# #              Applies to full_page=True and to target_element (which always
# #              captures full-page before cropping). Non-fatal and bounded by
# #              LAZY_SCROLL_TIMEOUT_MS; the page default timeout is restored
# #              afterwards so it cannot leak into page.screenshot().
# #
# #    ⚠️ `wait_until` and `timeout` are now DEAD PARAMETERS on both
# #       capture_screenshot() and _sync_capture_screenshot(). Navigation uses
# #       NAV_TIMEOUT_MS / COMMIT_TIMEOUT_MS with fixed wait conditions. Callers
# #       passing wait_until="networkidle" or a custom timeout are silently
# #       ignored. See the note in capture_screenshot's docstring.
# #
# # ============================================================================
# # Phase 2 additions (May 2026):
# #
# #   target_element: Optional[str]  — CSS selector (Business+)
# #
# #   Implementation strategy: Option A — bounding box crop.
# #
# #     1. Capture a full-page PNG of the entire document (Playwright).
# #     2. Use Pillow to resolve the element's bounding box via
# #        page.evaluate() (returns {x, y, width, height} in CSS pixels).
# #     3. Scale the bounding box by deviceScaleFactor (DPR) to get
# #        physical pixel coordinates that match the screenshot.
# #     4. Crop with Image.crop() and save to the final output path.
# #
# #   Why Option A over Playwright's built-in element.screenshot():
# #     - Works even when the element is partially off-screen or below the fold.
# #     - No dependency on element handle validity after DOM mutations.
# #     - Handles zero-size edge cases gracefully (raises ValueError with a
# #       clear message rather than silently producing an empty file).
# #     - Consistent output: the crop always comes from the same full render,
# #       so relative positions (sticky headers, overlays) are preserved.
# #
# #   Error handling:
# #     - Element not found  → ValueError (HTTP 400 via router)
# #     - Zero-size element  → ValueError (HTTP 400 via router)
# #     - Crop succeeds      → result dict includes element_selector key
# #
# #   Temp file lifecycle:
# #     - Full-page PNG written to SCREENSHOTS_DIR as a temp file.
# #     - Cropped output written to final filepath.
# #     - Temp full-page PNG deleted in the finally block regardless of
# #       success or failure — no orphan files left on disk.
# #
# #   Backward compatibility:
# #     - target_element=None (default) → existing behaviour unchanged.
# #     - result dict gains one new key: element_selector (None or str).
# #       Callers that ignore it are unaffected.
# #
# # ============================================================================
# #
# #    Root cause of production "Failed to fetch" on single screenshot capture:
# #      Render's load balancer enforces a 30-second HTTP response timeout on
# #      incoming connections. Heavy news sites (CNN, etc.) trigger Playwright's
# #      networkidle fallback chain: 45s → 60s → 60s — far exceeding that limit.
# #      Render closes the TCP connection; the browser receives a connection
# #      reset and reports "Failed to fetch" (a network error, NOT an HTTP error).
# #      Local dev is unaffected because uvicorn has no such connection timeout.
# #
# #    Two-part fix:
# #      1. Render Dashboard → service → Settings → Request Timeout = 120s
# #      2. PLAYWRIGHT_DEFAULT_TIMEOUT_MS / PLAYWRIGHT_FALLBACK_TIMEOUT_MS
# #         env vars (defaults: 30000ms / 35000ms).
# #         Combined worst-case: 30s + 35s + 35s = 100s (20s margin).
# #
# #      ⚠️ The 100s figure above counts navigation only. It omits the matching
# #         wait_for_load_state() call at each tier and the user delay, which is
# #         why real-world worst case reached ~210s. Superseded by the Aug 2026
# #         rewrite — kept here as the historical record.
# #
# # ============================================================================

# import os
# import secrets
# from pathlib import Path
# from datetime import datetime
# from typing import Optional, Dict, Any, List
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

# # ✅ REWRITTEN (Aug 2026 — heavy-site navigation)
# #
# # The old design called page.goto() up to three times, re-downloading and
# # re-parsing the entire document at each "fallback" tier. Worst case was
# # 210s against Render's 120s request timeout, so heavy pages were killed
# # mid-flight and surfaced as a network error rather than a clean timeout.
# #
# # New design: navigate ONCE with a condition that reliably fires, then give
# # the page a short bounded window to settle. Every budget below is additive
# # and the total is well inside 120s.
# DEFAULT_TIMEOUT  = int(os.getenv("PLAYWRIGHT_DEFAULT_TIMEOUT_MS",  "30000"))   # legacy, still read
# FALLBACK_TIMEOUT = int(os.getenv("PLAYWRIGHT_FALLBACK_TIMEOUT_MS", "35000"))   # legacy, still read

# # Primary navigation. domcontentloaded fires as soon as the HTML is parsed —
# # it does not wait for images, fonts, or async scripts, so it is reliable
# # even on pages that never go idle.
# NAV_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_NAV_TIMEOUT_MS", "25000"))

# # Last-resort navigation. "commit" resolves as soon as response headers
# # arrive — before any parsing. Almost nothing fails this.
# COMMIT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_COMMIT_TIMEOUT_MS", "15000"))

# # Optional settle window. We ASK for networkidle and accept not getting it.
# # This is the key change: idle is now an optimisation, not a requirement.
# SETTLE_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_SETTLE_TIMEOUT_MS", "8000"))

# # Budget for auto-scrolling a full-page capture to trigger lazy-loaded content.
# # LAZY_SCROLL_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_LAZY_SCROLL_TIMEOUT_MS", "6000"))

# # ✅ NEW (Aug 2026): explicit budget for the capture itself.
# #
# # page.screenshot() and page.pdf() take no timeout argument in our calls, so
# # they inherited the page default — which the lazy-scroll finally block sets
# # to NAV_TIMEOUT_MS (25s). That is a navigation number, not a rendering one.
# # A full-page capture of a very tall document (gnu.org's GPL text renders
# # ~25,000px at 1920 wide) exceeds 25s on a 1-CPU instance, and the resulting
# # Playwright Timeout was reported to the user as "the website did not respond
# # in time" — blaming a site that had already responded perfectly.
# #
# # 60s keeps the worst case inside Render's window:
# #   nav 25 + commit 15 + settle 8 + scroll 6 + delay 10 + capture 60 = 124s
# # ...which is why CAPTURE is only reached when the earlier phases were fast.
# # In practice a page that navigates in 2s and captures in 40s totals ~50s.
# CAPTURE_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_CAPTURE_TIMEOUT_MS", "60000"))

# # Third-party hosts that keep the network busy indefinitely and almost never
# # affect the visual result. Blocking these is the single biggest reason a
# # heavy page can reach networkidle at all.
# # Set PLAYWRIGHT_BLOCK_TRACKERS=0 to disable if a customer needs ads captured.
# BLOCK_TRACKERS = os.getenv("PLAYWRIGHT_BLOCK_TRACKERS", "1") != "0"

# _TRACKER_PATTERNS = (
#     "google-analytics.com", "googletagmanager.com", "doubleclick.net",
#     "googlesyndication.com", "google-adsense", "adservice.google",
#     "facebook.net", "connect.facebook", "hotjar.com", "segment.io",
#     "segment.com", "mixpanel.com", "amplitude.com", "fullstory.com",
#     "intercom.io", "clarity.ms", "newrelic.com", "nr-data.net",
#     "sentry.io", "bugsnag.com", "optimizely.com", "criteo.",
#     "taboola.com", "outbrain.com", "scorecardresearch.com",
#     "quantserve.com", "adsrvr.org", "pubmatic.com", "rubiconproject.com",
# )

# # Auto-scroll script for lazy-loaded content. Steps down the page in viewport
# # increments, then returns to the top so the capture starts from the origin.
# _LAZY_SCROLL_JS = """
# async () => {
#   await new Promise((resolve) => {
#     let total = 0;
#     const step = Math.max(200, Math.floor(window.innerHeight * 0.85));
#     const timer = setInterval(() => {
#       const height = document.body.scrollHeight;
#       window.scrollBy(0, step);
#       total += step;
#       // Stop at the bottom, or after 50 steps as a hard guard against
#       // infinite-scroll pages that grow faster than we scroll.
#       if (total >= height || total > step * 50) {
#         clearInterval(timer);
#         window.scrollTo(0, 0);
#         resolve();
#       }
#     }, 90);
#   });
# }
# """

# MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# MAX_DELAY_SECONDS            = 10
# MAX_REMOVE_ELEMENTS_COUNT    = 20
# MAX_REMOVE_ELEMENT_SELECTOR  = 200

# # ── Phase 1: Device preset registry ─────────────────────────────────────────
# SUPPORTED_DEVICES: Dict[str, str] = {
#     "iphone_13":         "iPhone 13",
#     "iphone_13_pro_max": "iPhone 13 Pro Max",
#     "iphone_se":         "iPhone SE",
#     "pixel_5":           "Pixel 5",
#     "pixel_7":           "Pixel 7",
#     "ipad_pro":          "iPad Pro 11",
#     "ipad_mini":         "iPad Mini",
#     "galaxy_s9":         "Galaxy S9+",
#     "galaxy_tab_s4":     "Galaxy Tab S4",
# }

# _JS_TIMEOUT_MS      = 5_000
# _SELECTOR_TIMEOUT_MS = 10_000

# # Pillow — required for WebP (Phase 1) and element crop (Phase 2)
# try:
#     from PIL import Image  # type: ignore
#     PILLOW_AVAILABLE = True
#     SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "webp", "pdf"]
#     logger.info("✅ Pillow available - WebP format enabled")
# except Exception:
#     PILLOW_AVAILABLE = False
#     SUPPORTED_FORMATS = ["png", "jpeg", "jpg", "pdf"]
#     logger.warning("⚠️ Pillow not available - WebP format disabled")

# _executor  = ThreadPoolExecutor(max_workers=3, thread_name_prefix="playwright")
# _init_lock = threading.Lock()

# # ✅ UPDATED (Aug 2026): the old banner advertised a "worst-case 3-tier"
# # figure derived from DEFAULT_TIMEOUT + FALLBACK_TIMEOUT * 2. Once Patch B
# # lands that number describes code that no longer exists, so the banner now
# # reports the new additive budget instead of a stale one.
# logger.info(
#     "📸 Playwright navigation budget: nav=%dms commit=%dms settle=%dms "
#     "lazy_scroll=%dms (worst-case pre-capture=%dms) | block_trackers=%s | "
#     "legacy DEFAULT=%dms FALLBACK=%dms",
#     NAV_TIMEOUT_MS, COMMIT_TIMEOUT_MS, SETTLE_TIMEOUT_MS, LAZY_SCROLL_TIMEOUT_MS,
#     NAV_TIMEOUT_MS + COMMIT_TIMEOUT_MS + SETTLE_TIMEOUT_MS + LAZY_SCROLL_TIMEOUT_MS,
#     BLOCK_TRACKERS,
#     DEFAULT_TIMEOUT, FALLBACK_TIMEOUT,
# )


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


# def _sanitize_delay(delay: Any) -> int:
#     try:
#         value = int(delay) if delay is not None else 0
#     except (TypeError, ValueError):
#         return 0
#     return max(0, min(value, MAX_DELAY_SECONDS))


# def _sanitize_remove_elements(selectors: Any) -> List[str]:
#     if not selectors or not isinstance(selectors, list):
#         return []
#     cleaned: List[str] = []
#     for item in selectors:
#         if not isinstance(item, str):
#             continue
#         stripped = item.strip()
#         if not stripped:
#             continue
#         if len(stripped) > MAX_REMOVE_ELEMENT_SELECTOR:
#             stripped = stripped[:MAX_REMOVE_ELEMENT_SELECTOR]
#         cleaned.append(stripped)
#         if len(cleaned) >= MAX_REMOVE_ELEMENTS_COUNT:
#             break
#     return cleaned


# _REMOVE_ELEMENTS_JS = """
# (selectors) => {
#   const summary = [];
#   for (const selector of selectors) {
#     try {
#       const nodes = document.querySelectorAll(selector);
#       let count = 0;
#       nodes.forEach(el => {
#         try {
#           el.style.setProperty('display', 'none', 'important');
#           count += 1;
#         } catch (e) { /* ignore per-element errors */ }
#       });
#       summary.push({ selector, hidden: count, ok: true });
#     } catch (e) {
#       summary.push({ selector, hidden: 0, ok: false, error: String(e && e.message || e) });
#     }
#   }
#   return summary;
# }
# """

# # ── Phase 2: Bounding box resolution script ──────────────────────────────────
# # Returns null when the selector matches nothing, or an object with
# # {x, y, width, height} in CSS pixels (not physical pixels).
# # We multiply by deviceScaleFactor when cropping.
# _ELEMENT_BBOX_JS = """
# (selector) => {
#   const el = document.querySelector(selector);
#   if (!el) return null;
#   const rect = el.getBoundingClientRect();
#   return {
#     x:      rect.left + window.scrollX,
#     y:      rect.top  + window.scrollY,
#     width:  rect.width,
#     height: rect.height
#   };
# }
# """


# class ScreenshotService:
#     def __init__(self):
#         self.browser: Optional[Browser] = None
#         self.playwright = None
#         self._initialized = False
#         self._init_error: Optional[str] = None

#     def is_ready(self) -> bool:
#         return bool(self._initialized and self.browser and not self._init_error)

#     def last_error(self) -> Optional[str]:
#         return self._init_error

#     # ── Phase 1: Device helpers ──────────────────────────────────────────────

#     def get_available_devices(self) -> List[str]:
#         return list(SUPPORTED_DEVICES.keys())

#     def _get_device_descriptor(self, device_key: str) -> Optional[Dict[str, Any]]:
#         """
#         Resolve device key to Playwright descriptor dict.
#         Fast path: reads self.playwright.devices (already running, no subprocess).
#         Slow path: spins a fresh thread so sync_playwright() is safe outside
#         the asyncio loop.
#         """
#         playwright_name = SUPPORTED_DEVICES.get(device_key)
#         if not playwright_name:
#             return None
#         if self.playwright is not None:
#             descriptor = self.playwright.devices.get(playwright_name)
#             return dict(descriptor) if descriptor else None
#         import concurrent.futures
#         with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
#             future = pool.submit(self._get_device_descriptor_sync, playwright_name)
#             return future.result()

#     def _get_device_descriptor_sync(self, playwright_name: str) -> Optional[Dict[str, Any]]:
#         with sync_playwright() as pw:
#             descriptor = pw.devices.get(playwright_name)
#             return dict(descriptor) if descriptor else None

#     # ── Lifecycle ────────────────────────────────────────────────────────────

#     async def initialize(self) -> None:
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

#     # ── Public capture API ───────────────────────────────────────────────────

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
#         delay: Optional[int] = None,
#         remove_elements: Optional[List[str]] = None,
#         # Phase 1
#         device: Optional[str] = None,
#         custom_js: Optional[str] = None,
#         wait_for_selector: Optional[str] = None,
#         # Phase 2
#         target_element: Optional[str] = None,
#     ) -> Dict[str, Any]:
#         """
#         Capture a screenshot and return a result dict.

#         Phase 2 addition:
#           target_element: Optional[str]
#             CSS selector for the element to crop to (Business+).
#             The full page is captured first, then Pillow crops to the
#             element's bounding box.  Raises ValueError when:
#               - Pillow is not installed
#               - The selector matches no element
#               - The matched element has zero width or height

#         Returns the same dict as Phase 1, plus:
#           element_selector: None | str   ← selector used, or None if not provided
#         """
#         fmt = (format or "png").lower().strip()

#         if fmt not in SUPPORTED_FORMATS:
#             if fmt == "webp" and not PILLOW_AVAILABLE:
#                 raise ValueError(
#                     f"WebP format requires Pillow. Install with: pip install Pillow. "
#                     f"Supported formats: {SUPPORTED_FORMATS}"
#                 )
#             raise ValueError(f"Unsupported format: {fmt}. Must be one of: {SUPPORTED_FORMATS}")

#         # Phase 2 gate — Pillow is required for the bounding-box crop
#         if target_element and not PILLOW_AVAILABLE:
#             raise ValueError(
#                 "Element selection requires Pillow. Install with: pip install Pillow."
#             )

#         if not self.is_ready():
#             await self.initialize()

#         safe_delay           = _sanitize_delay(delay)
#         safe_remove_elements = _sanitize_remove_elements(remove_elements)

#         device_descriptor: Optional[Dict[str, Any]] = None
#         if device:
#             device_descriptor = self._get_device_descriptor(device)
#             if device_descriptor is None:
#                 raise ValueError(
#                     f"Unknown device preset '{device}'. "
#                     f"Valid options: {list(SUPPORTED_DEVICES.keys())}"
#                 )

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
#             safe_delay,
#             safe_remove_elements,
#             device_descriptor,    # Phase 1
#             custom_js,            # Phase 1
#             wait_for_selector,    # Phase 1
#             target_element,       # Phase 2
#         )

#     # ── Synchronous Playwright worker ────────────────────────────────────────

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
#         delay: int,
#         remove_elements: List[str],
#         # Phase 1
#         device_descriptor: Optional[Dict[str, Any]],
#         custom_js: Optional[str],
#         wait_for_selector: Optional[str],
#         # Phase 2
#         target_element: Optional[str],
#     ) -> Dict[str, Any]:
#         """
#         All Playwright calls happen here. Runs in a thread executor.

#         Execution order inside the page:
#           1. Build browser context (device descriptor overrides viewport/UA/DPR)
#           2. block trackers, navigate ONCE (domcontentloaded → commit),
#              then bounded networkidle settle (non-fatal)
#           3. wait_for_selector (non-fatal)
#           4. remove_elements JS (non-fatal per-selector)
#           5. custom_js page.evaluate() (option-c: non-fatal)
#           6. 500ms settle wait
#           7. user delay
#          7b. lazy-load scroll pass (full_page or target_element, non-fatal)
#           8. [Phase 2] resolve target_element bounding box via page.evaluate()
#           9. capture full-page PNG (always full-page when target_element is set)
#          10. [Phase 2] Pillow crop to bounding box → save to final filepath
#          11. WebP re-encode or PDF (unchanged for non-element captures)
#          12. Temp file cleanup

#         Returns the Phase 1 dict plus:
#           element_selector: None | str
#         """
#         if not self.browser:
#             raise RuntimeError("Playwright browser is not initialized")

#         timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
#         random_id = secrets.token_hex(8)
#         js_warning: Optional[str] = None

#         # ── File path planning ────────────────────────────────────────────────
#         # Three possible pipelines:
#         #   A) target_element → full_temp.png → crop → final.(fmt)
#         #   B) webp           → temp.png      → webp-encode → final.webp
#         #   C) normal         → final.(fmt)   directly

#         temp_filepath: Optional[Path] = None   # always cleaned up in finally

#         if target_element:
#             # Pipeline A: always capture PNG full-page, then crop with Pillow.
#             # The final output can still be any supported format.
#             temp_filename  = f"screenshot_{timestamp}_{random_id}_full.png"
#             temp_filepath  = SCREENSHOTS_DIR / temp_filename
#             if fmt == "webp":
#                 final_filename = f"screenshot_{timestamp}_{random_id}.webp"
#             elif fmt in ("jpeg", "jpg"):
#                 final_filename = f"screenshot_{timestamp}_{random_id}.jpg"
#             elif fmt == "pdf":
#                 # PDF from a cropped region is not well-defined; fall back to PNG.
#                 logger.warning(
#                     "⚠️ target_element + pdf is not supported — falling back to PNG"
#                 )
#                 fmt = "png"
#                 final_filename = f"screenshot_{timestamp}_{random_id}.png"
#             else:
#                 final_filename = f"screenshot_{timestamp}_{random_id}.{fmt}"
#             filepath = SCREENSHOTS_DIR / final_filename
#             filename = final_filename

#         elif fmt == "webp":
#             # Pipeline B: existing WebP flow
#             temp_filename  = f"screenshot_{timestamp}_{random_id}.png"
#             final_filename = f"screenshot_{timestamp}_{random_id}.webp"
#             temp_filepath  = SCREENSHOTS_DIR / temp_filename
#             filepath       = SCREENSHOTS_DIR / final_filename
#             filename       = final_filename

#         else:
#             # Pipeline C: direct capture
#             filename = f"screenshot_{timestamp}_{random_id}.{fmt}"
#             filepath = SCREENSHOTS_DIR / filename

#         # ── 1. Build browser context ──────────────────────────────────────────
#         # ✅ FIX (Aug 2026): record the viewport ACTUALLY applied. A device
#         # descriptor carries its own viewport, so the width/height arguments
#         # never reach the browser — but they were still what got returned.
#         actual_viewport_width   = int(width)
#         actual_viewport_height  = int(height)
#         device_scale_factor_ctx = 1.0

#         if device_descriptor:
#             _ALLOWED_CONTEXT_KEYS = {
#                 "viewport", "user_agent", "device_scale_factor",
#                 "is_mobile", "has_touch", "color_scheme", "locale",
#                 "timezone_id", "screen",
#             }
#             context_kwargs: Dict[str, Any] = {
#                 k: v for k, v in device_descriptor.items()
#                 if k in _ALLOWED_CONTEXT_KEYS
#             }
#             vp = device_descriptor.get("viewport") or {}
#             if vp.get("width") and vp.get("height"):
#                 actual_viewport_width  = int(vp["width"])
#                 actual_viewport_height = int(vp["height"])
#             device_scale_factor_ctx = float(
#                 device_descriptor.get("device_scale_factor", 1) or 1
#             )
#             if dark_mode:
#                 context_kwargs["color_scheme"] = "dark"
#             logger.info(
#                 "📱 Device preset applied: viewport=%dx%d dpr=%.2f UA=%s",
#                 actual_viewport_width, actual_viewport_height,
#                 device_scale_factor_ctx,
#                 str(device_descriptor.get("user_agent", "?"))[:70],
#             )
#         else:
#             context_kwargs = {
#                 "viewport": {"width": int(width), "height": int(height)},
#                 "color_scheme": "dark" if dark_mode else "light",
#                 "user_agent": (
#                     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
#                 ),
#             }

#         context = self.browser.new_context(**context_kwargs)
#         page    = context.new_page()

#         try:
#             # ✅ UPDATED (Aug 2026 — Patch B): logged DEFAULT_TIMEOUT and
#             # FALLBACK_TIMEOUT, neither of which the navigation block reads any
#             # more. Now reports the budgets actually in force for this capture.
#             logger.info(
#                 "📸 Capturing screenshot: %s (format=%s nav=%dms commit=%dms "
#                 "settle=%dms delay=%ds remove=%d device=%s custom_js=%s "
#                 "target_element=%s)",
#                 url, fmt, NAV_TIMEOUT_MS, COMMIT_TIMEOUT_MS, SETTLE_TIMEOUT_MS, delay,
#                 len(remove_elements),
#                 "yes" if device_descriptor else "no",
#                 "yes" if custom_js else "no",
#                 repr(target_element) if target_element else "no",
#             )

#             # ── 2. Navigate ONCE, then settle ─────────────────────────────────
#             # ✅ REWRITTEN (Aug 2026). The previous implementation called
#             # page.goto() up to three times, re-downloading the document at
#             # each "fallback" tier — 210s worst case against a 120s platform
#             # timeout. Heavy pages (gnu.org's GPL text, news sites) were
#             # killed mid-reload and surfaced as "Failed to fetch".
#             #
#             # Now: one navigation with a condition that reliably fires, then
#             # an optional bounded settle. networkidle is requested, not
#             # required.
#             #
#             # Block trackers BEFORE navigating. Ad and analytics requests are
#             # the main reason networkidle never fires, and they almost never
#             # change what the page looks like.
#             if BLOCK_TRACKERS:
#                 def _route_filter(route):
#                     try:
#                         req_url = route.request.url
#                         if any(pat in req_url for pat in _TRACKER_PATTERNS):
#                             return route.abort()
#                         return route.continue_()
#                     except Exception:
#                         # Never let routing kill a capture.
#                         try:
#                             return route.continue_()
#                         except Exception:
#                             return None

#                 try:
#                     page.route("**/*", _route_filter)
#                 except Exception as route_err:
#                     logger.warning("⚠️ Tracker blocking unavailable (non-fatal): %s", route_err)

#             page_loaded = False
#             last_error: Optional[Exception] = None

#             # Attempt 1 — domcontentloaded. Fires once the HTML is parsed;
#             # does not wait on images, fonts, or async scripts.
#             try:
#                 page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
#                 page_loaded = True
#             except PlaywrightError as e:
#                 last_error = e
#                 if "Timeout" not in str(e):
#                     # A real navigation error (DNS, refused, cert) — surface it
#                     # immediately rather than burning the retry budget.
#                     raise
#                 logger.info(
#                     "⏱ domcontentloaded timed out after %dms — retrying with commit",
#                     NAV_TIMEOUT_MS,
#                 )

#             # Attempt 2 — commit. Resolves as soon as response headers arrive,
#             # before parsing. This is the floor: if this fails, the site is
#             # genuinely unreachable.
#             if not page_loaded:
#                 try:
#                     page.goto(url, wait_until="commit", timeout=COMMIT_TIMEOUT_MS)
#                     page_loaded = True
#                     logger.info("✅ Navigated with commit (page still loading)")
#                 except PlaywrightError as e2:
#                     last_error = e2

#             if not page_loaded:
#                 raise last_error or PlaywrightError("Navigation failed")

#             # Settle — ask for quiet, accept not getting it. On a page that
#             # never idles this costs SETTLE_TIMEOUT_MS and then continues,
#             # instead of failing the whole capture.
#             try:
#                 page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
#                 logger.info("✅ Page reached networkidle")
#             except PlaywrightError:
#                 logger.info(
#                     "⏱ Page never went idle within %dms — capturing anyway "
#                     "(expected on ad-heavy and live-updating sites)",
#                     SETTLE_TIMEOUT_MS,
#                 )

#             # ── 3. Wait for selector (Phase 1, non-fatal) ─────────────────────
#             if wait_for_selector:
#                 try:
#                     page.wait_for_selector(
#                         wait_for_selector, state="visible", timeout=_SELECTOR_TIMEOUT_MS
#                     )
#                     logger.info("✅ wait_for_selector: '%s' found", wait_for_selector)
#                 except PlaywrightError as sel_err:
#                     logger.warning(
#                         "⚠️ wait_for_selector timed out for '%s' (capture continues): %s",
#                         wait_for_selector, sel_err,
#                     )

#             # ── 4. Remove elements (Phase 1, non-fatal) ───────────────────────
#             if remove_elements:
#                 try:
#                     summary     = page.evaluate(_REMOVE_ELEMENTS_JS, remove_elements)
#                     total_hidden = sum(
#                         s.get("hidden", 0) for s in (summary or []) if s.get("ok")
#                     )
#                     failed = [s for s in (summary or []) if not s.get("ok")]
#                     logger.info(
#                         "🙈 remove_elements: hid %d element(s) across %d selector(s); %d failed",
#                         total_hidden, len(remove_elements), len(failed),
#                     )
#                     for f in failed:
#                         logger.warning("   bad selector %r: %s", f.get("selector"), f.get("error"))
#                 except Exception as hide_err:
#                     logger.warning("⚠️ remove_elements failed silently: %s", hide_err)

#             # ── 5. Custom JavaScript (Phase 1, option-c: non-fatal) ───────────
#             if custom_js:
#                 try:
#                     page.evaluate(custom_js)
#                     logger.info("✅ Custom JavaScript executed successfully")
#                 except PlaywrightError as js_err:
#                     js_warning = str(js_err)
#                     logger.warning("⚠️ Custom JavaScript failed (capture continues): %s", js_err)
#                 except Exception as js_err:
#                     js_warning = str(js_err)
#                     logger.warning(
#                         "⚠️ Custom JavaScript raised unexpected error (capture continues): %s",
#                         js_err,
#                     )

#             # ── 6. 500ms settle ───────────────────────────────────────────────
#             try:
#                 page.wait_for_timeout(500)
#             except Exception:
#                 pass

#             # ── 7. User delay ─────────────────────────────────────────────────
#             if delay > 0:
#                 try:
#                     page.wait_for_timeout(delay * 1000)
#                 except Exception as delay_err:
#                     logger.warning("⚠️ delay wait failed silently: %s", delay_err)

#             # ── 7b. Lazy-load pass (full-page captures only) ──────────────────
#             # ✅ NEW (Aug 2026). Because we no longer wait for networkidle,
#             # images that load on scroll may not have started. Stepping down
#             # the page triggers them, then we return to the top so the capture
#             # begins at the origin. Bounded and non-fatal.
#             #
#             # ⚠️ set_default_timeout() mutates the page for EVERY subsequent
#             # call, and Playwright exposes no getter to read the old value.
#             # Left unrestored, the 6s scroll budget would silently become the
#             # timeout for page.screenshot() further down — which takes no
#             # explicit timeout — and a full-page render of a heavy document
#             # routinely exceeds 6s. That would time out precisely the pages
#             # this rewrite exists to rescue. The finally block puts it back.
#             if full_page or target_element:
#                 try:
#                     page.set_default_timeout(LAZY_SCROLL_TIMEOUT_MS)
#                     page.evaluate(_LAZY_SCROLL_JS)
#                     # Brief pause for triggered images to actually arrive.
#                     page.wait_for_timeout(600)
#                     logger.info("📜 Lazy-load scroll pass complete")
#                 except Exception as scroll_err:
#                     logger.warning(
#                         "⚠️ Lazy-load scroll failed (capture continues): %s", scroll_err
#                     )
#                 finally:
#                     try:
#                         page.set_default_timeout(NAV_TIMEOUT_MS)
#                     except Exception:
#                         pass

#             # ── 8. Phase 2: resolve element bounding box ──────────────────────
#             # Done after all page manipulation so the box reflects the final
#             # DOM state (post-JS, post-element-removal, post-delay).
#             element_bbox: Optional[Dict[str, float]] = None
#             device_scale_factor: float = 1.0

#             if target_element:
#                 bbox_result = page.evaluate(_ELEMENT_BBOX_JS, target_element)

#                 if bbox_result is None:
#                     raise ValueError(
#                         f"Element not found: no element matched the selector "
#                         f"'{target_element}'. Check that the selector is correct "
#                         f"and the element exists on the page at capture time."
#                     )

#                 el_w = float(bbox_result.get("width",  0))
#                 el_h = float(bbox_result.get("height", 0))

#                 if el_w <= 0 or el_h <= 0:
#                     raise ValueError(
#                         f"Element '{target_element}' was found but has zero size "
#                         f"({el_w}×{el_h}px). The element may be hidden or collapsed. "
#                         f"Use remove_elements or custom_js to ensure it is visible "
#                         f"before capturing."
#                     )

#                 element_bbox = bbox_result

#                 # Get the device pixel ratio so we can scale CSS → physical pixels.
#                 # devicePixelRatio is always 1 for desktop captures; devices set it
#                 # to their DPR (e.g. 3 for iPhone 13).
#                 try:
#                     device_scale_factor = float(
#                         page.evaluate("() => window.devicePixelRatio || 1")
#                     )
#                 except Exception:
#                     device_scale_factor = 1.0

#                 logger.info(
#                     "🎯 target_element '%s': bbox=(%s,%s,%s,%s) dpr=%.2f",
#                     target_element,
#                     bbox_result["x"], bbox_result["y"],
#                     bbox_result["width"], bbox_result["height"],
#                     device_scale_factor,
#                 )

#             # ── 9. Capture ────────────────────────────────────────────────────
#             # ✅ NEW (Aug 2026): `phase` lets the error handler tell the user
#             # WHICH step timed out. Without it, a slow full-page render was
#             # reported as "the website did not respond in time" — false, and
#             # it sent people looking at the wrong thing.
#             phase = "capture"

#             if target_element:
#                 page.screenshot(
#                     path=str(temp_filepath), full_page=True, type="png",
#                     timeout=CAPTURE_TIMEOUT_MS,
#                 )

#             elif fmt == "pdf":
#                 page.pdf(
#                     path=str(filepath), format="A4", print_background=True,
#                 )

#             elif fmt == "webp":
#                 page.screenshot(
#                     path=str(temp_filepath), full_page=bool(full_page), type="png",
#                     timeout=CAPTURE_TIMEOUT_MS,
#                 )

#             else:
#                 options: Dict[str, Any] = {
#                     "path": str(filepath),
#                     "full_page": bool(full_page),
#                     "timeout": CAPTURE_TIMEOUT_MS,
#                 }
#                 if fmt in ("jpeg", "jpg"):
#                     options["type"] = "jpeg"
#                     options["quality"] = 90
#                 else:
#                     options["type"] = "png"
#                 page.screenshot(**options)
            
#             # if target_element:
#             #     # Always capture full-page PNG first, then crop.
#             #     page.screenshot(
#             #         path=str(temp_filepath), full_page=True, type="png"
#             #     )

#             # elif fmt == "pdf":
#             #     page.pdf(path=str(filepath), format="A4", print_background=True)

#             # elif fmt == "webp":
#             #     page.screenshot(
#             #         path=str(temp_filepath), full_page=bool(full_page), type="png"
#             #     )

#             # else:
#             #     options: Dict[str, Any] = {
#             #         "path": str(filepath),
#             #         "full_page": bool(full_page),
#             #     }
#             #     if fmt in ("jpeg", "jpg"):
#             #         options["type"] = "jpeg"
#             #         options["quality"] = 90
#             #     else:
#             #         options["type"] = "png"
#             #     page.screenshot(**options)

#             # ── 10. Phase 2: Pillow crop ──────────────────────────────────────
#             if target_element and element_bbox is not None:
#                 dpr = device_scale_factor
#                 x      = int(element_bbox["x"]      * dpr)
#                 y      = int(element_bbox["y"]      * dpr)
#                 x2     = int((element_bbox["x"] + element_bbox["width"])  * dpr)
#                 y2     = int((element_bbox["y"] + element_bbox["height"]) * dpr)

#                 full_img = Image.open(str(temp_filepath))
#                 img_w, img_h = full_img.size

#                 # Clamp to image bounds — handles elements partially off-screen
#                 x  = max(0, min(x,  img_w))
#                 y  = max(0, min(y,  img_h))
#                 x2 = max(0, min(x2, img_w))
#                 y2 = max(0, min(y2, img_h))

#                 if x2 <= x or y2 <= y:
#                     raise ValueError(
#                         f"Element '{target_element}' bounding box is entirely "
#                         f"outside the captured image bounds. "
#                         f"Try using full_page=true or scrolling to the element "
#                         f"via custom_js before capturing."
#                     )

#                 cropped = full_img.crop((x, y, x2, y2))

#                 if fmt == "webp":
#                     cropped.save(str(filepath), "WEBP", quality=90, method=6)
#                 elif fmt in ("jpeg", "jpg"):
#                     cropped = cropped.convert("RGB")   # strip alpha for JPEG
#                     cropped.save(str(filepath), "JPEG", quality=90)
#                 else:
#                     cropped.save(str(filepath), "PNG")

#                 logger.info(
#                     "✂️ Element crop: (%d,%d)→(%d,%d) = %d×%d physical px",
#                     x, y, x2, y2, x2 - x, y2 - y,
#                 )

#             # ── 11. WebP re-encode (non-element pipeline) ─────────────────────
#             elif fmt == "webp" and temp_filepath and temp_filepath.exists():
#                 img = Image.open(str(temp_filepath))
#                 img.save(str(filepath), "WEBP", quality=90, method=6)

#             # ── 12. Validate output file size ─────────────────────────────────
#             file_size = filepath.stat().st_size
#             if file_size > MAX_FILE_SIZE:
#                 try:
#                     filepath.unlink(missing_ok=True)
#                 except Exception:
#                     pass
#                 raise ValueError(
#                     f"Screenshot too large: {file_size} bytes (max: {MAX_FILE_SIZE})"
#                 )

#             logger.info(
#                 "✅ Screenshot captured: %s format=%s size=%d bytes "
#                 "js_warning=%s element=%s",
#                 filename, fmt, file_size,
#                 bool(js_warning),
#                 repr(target_element) if target_element else "no",
#             )

#             return {
#                 "filename":         filename,
#                 "filepath":         str(filepath),
#                 "url":              url,
#                 "width":            actual_viewport_width,
#                 "height":           actual_viewport_height,
#                 "viewport_width":   actual_viewport_width,
#                 "viewport_height":  actual_viewport_height,
#                 "requested_width":  int(width),
#                 "requested_height": int(height),
#                 "device_scale_factor": device_scale_factor_ctx,
#                 "format":           fmt,
#                 "full_page":        bool(full_page),
#                 "dark_mode":        bool(dark_mode),
#                 "file_size":        int(file_size),
#                 "created_at":       datetime.utcnow(),
#                 "js_warning":       js_warning,         # Phase 1
#                 "element_selector": target_element,     # Phase 2 (None if unused)
#             }

#         except PlaywrightError as e:
#             error_msg = str(e)
#             logger.error("❌ Playwright error capturing %s: %s", url, error_msg)
#             if "Timeout" in error_msg:
#                 url_hint = url[:50] + "..." if len(url) > 50 else url
#                 raise ValueError(
#                     "Screenshot timed out after all retry attempts. "
#                     f"The website ({url_hint}) may be too slow or have continuous "
#                     f"network activity. Try increasing the delay or using a simpler URL."
#                 ) from e
#             raise ValueError(f"Failed to capture screenshot: {error_msg}") from e

#         finally:
#             # Always clean up the temp full-page PNG regardless of success/failure
#             if temp_filepath and temp_filepath.exists():
#                 try:
#                     temp_filepath.unlink(missing_ok=True)
#                 except Exception:
#                     pass
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


# def get_screenshot_url(filename: str, base_url: str = "") -> str:
#     if not base_url:
#         environment = os.getenv("ENVIRONMENT", "development").lower()
#         is_prod = environment == "production"
#         if is_prod:
#             base_url = (
#                 os.getenv("CUSTOM_API_DOMAIN") or
#                 os.getenv("BACKEND_URL") or
#                 "http://localhost:8000"
#             ).strip().rstrip("/")
#         else:
#             base_url = (
#                 os.getenv("BACKEND_URL") or
#                 "http://localhost:8000"
#             ).strip().rstrip("/")
#     return f"{base_url.rstrip('/')}/screenshots/{filename}"


# def increment_user_usage(user) -> None:
#     user.usage_screenshots = (user.usage_screenshots or 0) + 1
#     user.usage_api_calls   = (user.usage_api_calls   or 0) + 1


# def check_usage_limit(user, tier_limits, db=None) -> bool:
#     """
#     ✅ FIX (Aug 2026): prefer the period-scoped count from usage_accounting.

#     user.usage_screenshots is a running counter that does not reset at the
#     period boundary and does not account for tombstoned deletions, so it
#     drifts from what /subscription_status shows the user. When a db session
#     is available, use the authoritative source.
#     """
#     limit = tier_limits.get("screenshots")
#     if limit == "unlimited" or limit is None:
#         return True

#     try:
#         limit = int(limit)
#     except (TypeError, ValueError, OverflowError):
#         # Unrecognised limit value — fail open rather than blocking a paying
#         # customer on a config typo, but make it loud.
#         # OverflowError covers float("inf"), which some tier maps use as the
#         # unlimited sentinel instead of the string; int(inf) raises it, and
#         # it is NOT a subclass of ValueError.
#         logger.error("check_usage_limit: unparseable limit %r — allowing", limit)
#         return True

#     if db is not None:
#         try:
#             # from usage_accounting import screenshots_used_this_period
#             # return screenshots_used_this_period(db, user.id) < limit
#             from usage_accounting import screenshots_used_this_period
#             # Pass the User, not the id — the period boundary depends on the
#             # Stripe billing anchor.
#             return screenshots_used_this_period(db, user) < limit
#         except Exception as e:
#             logger.warning(
#                 "check_usage_limit: usage_accounting unavailable, "
#                 "falling back to user.usage_screenshots (%s)", e
#             )
#     else:
#         # Only warn about a missing session when one really was missing —
#         # the fallback above already logged its own reason.
#         logger.warning(
#             "check_usage_limit called without db — using non-authoritative counter"
#         )

#     return int(user.usage_screenshots or 0) < limit

# # ===== END OF backend\screenshot_service.py ========

