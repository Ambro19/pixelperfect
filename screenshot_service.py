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
#
# ⚠️ THE SEP 2026 FIXES BELOW ARE NOT YET APPLIED TO services/screenshot_service.py.

# ============================================================================
# SCREENSHOT SERVICE - PixelPerfect API (PRODUCTION READY)
# File: backend/screenshot_service.py
# Author: OneTechly
# Updated: September 2026
# ============================================================================
#
# ✅ FIX (Sep 2026 — "Cannot switch to a different thread")  ← THE BATCH BUG
#
#   _executor was ThreadPoolExecutor(max_workers=3). Playwright's SYNC API is
#   greenlet-based and thread-affine: the Playwright object, the Browser, the
#   BrowserContext and the Page may only be touched from the thread that
#   called sync_playwright().start().
#
#   initialize() runs _sync_initialize() on ONE of those three worker threads,
#   so the Browser belongs to that thread. Captures were then dispatched to
#   whichever of the three threads was free, so roughly two out of every three
#   captures raised greenlet.error("Cannot switch to a different thread")
#   instantly — 0 bytes, 0.01s, "failed". That is exactly the pattern in the
#   batch job list.
#
#   Fix: max_workers=1. One worker thread means initialize(), every capture and
#   close() all run on the same thread, which is the only arrangement the sync
#   API supports. Captures were already serialised in practice (one shared
#   Browser, one 1-CPU instance), so this costs no real throughput.
#
# ✅ FIX (Sep 2026 — "Target page, context or browser has been closed")
#
#   When Chromium dies (an ultrawide full-page capture of a very tall document
#   is hundreds of MB of raw bitmap, and the OOM killer takes the browser
#   process), self.browser stayed set to the dead object and every later
#   capture failed on new_context(). _sync_ensure_browser() now checks
#   browser.is_connected() before each capture and relaunches on the SAME
#   worker thread, and _sync_capture_with_recovery() retries a capture once if
#   the browser dies mid-flight.
#
# ✅ NEW (Sep 2026 — full-page capture size budget)
#
#   The reason Chromium died in the first place. A full-page capture at 3440px
#   wide of a page that renders 20,000px tall is 3440 × 20000 × 4 bytes ≈ 275 MB
#   of raw bitmap before compression, plus compositor tiles. On a 2 GB Render
#   instance running two Chromium instances that is fatal.
#
#   _full_page_clip_height() caps the rasterised area (default 40M pixels, and
#   never taller than Chromium's 16,384px texture limit, both adjusted for the
#   device pixel ratio). Over-tall pages are now TRIMMED instead of killing the
#   browser, and the result dict carries truncated / document_height so callers
#   can say so. PDF is unaffected — it is not rasterised this way.
#
# ✅ FIX (Sep 2026 — tracker blocking intercepted every request)
#
#   page.route("**/*", …) makes Playwright intercept EVERY request and round-trip
#   it to Python before the browser may continue. On a page with several hundred
#   subresources that alone can push domcontentloaded past NAV_TIMEOUT_MS — the
#   timeout then gets reported as the website being slow. Routes are now
#   registered per tracker pattern, so Chromium only intercepts requests that
#   can actually match, and everything else goes straight through.
#
# ✅ REMOVED (Sep 2026 — second sync_playwright() entry point)
#
#   _get_device_descriptor_sync() started a SECOND Playwright instance in a
#   throwaway thread whenever self.playwright was None. It was dead code on the
#   happy path and a second thread-affinity hazard otherwise. _sync_initialize()
#   is now the only sync_playwright()/chromium.launch() call in this file.
#
# ----------------------------------------------------------------------------
# Earlier fixes (all retained):
# ✅ is_ready() checks browser availability
# ✅ No db.commit() — caller controls transaction
# ✅ PLAYWRIGHT_BROWSERS_PATH-aware guidance
# ✅ WebP support via Pillow (PNG → WebP)
# ✅ Safer cleanup for temp files
# ✅ FIX (Mar 2026 v1): get_screenshot_url prefers CUSTOM_API_DOMAIN in prod
# ✅ FIX (Mar 2026 v2): get_screenshot_url is environment-aware
# ✅ FIX (Apr 2026): Playwright timeouts configurable via env vars.
# ✅ NEW (Apr 2026): `delay` and `remove_elements` parameters now honored.
# ✅ NEW (May 2026 — Phase 1): Device emulation (Pro+) and Custom JavaScript.
# ✅ FIX (May 2026 — Phase 1): _get_device_descriptor no longer calls
#    sync_playwright() inside the asyncio loop.
# ✅ NEW (May 2026 — Phase 2): Element Selection (Business+) implemented.
# ✅ REWRITTEN (Aug 2026 — heavy-site navigation): PATCH A/B/C.
# ✅ FIX (Aug 2026 — boot failure): LAZY_SCROLL_TIMEOUT_MS restored.
# ✅ NEW (Aug 2026 — phase-aware timeout reporting).
#
#    ⚠️ `wait_until` and `timeout` are DEAD PARAMETERS on both
#       capture_screenshot() and _sync_capture_screenshot().
#
# ============================================================================
# Phase 2 notes (May 2026) — Option A bounding-box crop:
#   1. Capture a full-page PNG of the entire document (Playwright).
#   2. Resolve the element's bounding box via page.evaluate().
#   3. Scale by deviceScaleFactor to physical pixels.
#   4. Crop with Pillow and save to the final output path.
#   Errors: element not found / zero size → ValueError (HTTP 400 via router).
#   Temp full-page PNG always removed in the finally block.
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

DEFAULT_TIMEOUT  = int(os.getenv("PLAYWRIGHT_DEFAULT_TIMEOUT_MS",  "30000"))   # legacy, still read
FALLBACK_TIMEOUT = int(os.getenv("PLAYWRIGHT_FALLBACK_TIMEOUT_MS", "35000"))   # legacy, still read

# Primary navigation. domcontentloaded fires as soon as the HTML is parsed.
NAV_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_NAV_TIMEOUT_MS", "25000"))

# Last-resort navigation. "commit" resolves as soon as response headers arrive.
COMMIT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_COMMIT_TIMEOUT_MS", "15000"))

# Optional settle window. We ASK for networkidle and accept not getting it.
SETTLE_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_SETTLE_TIMEOUT_MS", "8000"))

# Budget for auto-scrolling a full-page capture to trigger lazy-loaded content.
# ⚠️ Do not comment this out — the startup banner reads it at MODULE level.
LAZY_SCROLL_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_LAZY_SCROLL_TIMEOUT_MS", "6000"))

# Explicit budget for the capture itself (see Aug 2026 note).
CAPTURE_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_CAPTURE_TIMEOUT_MS", "60000"))

# ✅ NEW (Sep 2026): full-page capture size budget.
#
# Chromium cannot reliably rasterise a texture taller than 16,384px, and a
# large bitmap is what kills the browser process on a 2 GB instance:
#   3440 × 20000 × 4 bytes ≈ 275 MB raw, before compositor overhead.
# 40M pixels ≈ 160 MB raw, which survives comfortably.
MAX_CAPTURE_HEIGHT_PX = int(os.getenv("PLAYWRIGHT_MAX_CAPTURE_HEIGHT_PX", "16384"))
MAX_CAPTURE_PIXELS    = int(os.getenv("PLAYWRIGHT_MAX_CAPTURE_PIXELS",    "40000000"))

# Chromium launch flags — one definition, used by initial launch AND relaunch.
_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]

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

# Auto-scroll script for lazy-loaded content.
_LAZY_SCROLL_JS = """
async () => {
  await new Promise((resolve) => {
    let total = 0;
    const step = Math.max(200, Math.floor(window.innerHeight * 0.85));
    const timer = setInterval(() => {
      const height = document.body.scrollHeight;
      window.scrollBy(0, step);
      total += step;
      if (total >= height || total > step * 50) {
        clearInterval(timer);
        window.scrollTo(0, 0);
        resolve();
      }
    }, 90);
  });
}
"""

# ✅ NEW (Sep 2026): document height probe for the capture budget.
_DOC_HEIGHT_JS = """
() => Math.max(
  document.documentElement ? document.documentElement.scrollHeight : 0,
  document.body ? document.body.scrollHeight : 0
)
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

# ✅ FIX (Sep 2026 — THE BATCH BUG): max_workers=3 → 1.
#
# Playwright's sync API is thread-affine. With three workers, the Browser was
# created on whichever thread ran initialize() and every capture that landed on
# one of the other two raised greenlet.error("Cannot switch to a different
# thread") instantly. One worker == one owning thread == the only arrangement
# the sync API supports. Do not raise this number. If you need real capture
# concurrency, run a second ScreenshotService with its OWN executor and its OWN
# browser, and size it against available RAM.
_executor  = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")
_init_lock = threading.Lock()

logger.info(
    "📸 Playwright navigation budget: nav=%dms commit=%dms settle=%dms "
    "lazy_scroll=%dms capture=%dms (worst-case=%dms) | block_trackers=%s | "
    "max_capture=%dpx/%dMpx | workers=1 | legacy DEFAULT=%dms FALLBACK=%dms",
    NAV_TIMEOUT_MS, COMMIT_TIMEOUT_MS, SETTLE_TIMEOUT_MS, LAZY_SCROLL_TIMEOUT_MS,
    CAPTURE_TIMEOUT_MS,
    NAV_TIMEOUT_MS + COMMIT_TIMEOUT_MS + SETTLE_TIMEOUT_MS
    + LAZY_SCROLL_TIMEOUT_MS + CAPTURE_TIMEOUT_MS,
    BLOCK_TRACKERS,
    MAX_CAPTURE_HEIGHT_PX, MAX_CAPTURE_PIXELS // 1_000_000,
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


# ✅ NEW (Sep 2026): recognise "the browser process is gone" so the caller can
# relaunch instead of failing every subsequent capture against a dead object.
_BROWSER_GONE_MARKERS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "target closed",
    "browser closed",
    "connection closed",
    "browser has disconnected",
)


def _is_browser_gone(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _BROWSER_GONE_MARKERS)


# ✅ NEW (Sep 2026): how tall a full-page capture may be before it endangers
# the browser process. Returns a height in CSS pixels.
def _full_page_clip_height(
    css_width: int, document_height: int, device_scale_factor: float = 1.0
) -> int:
    dpr = max(float(device_scale_factor or 1.0), 1.0)
    width = max(int(css_width or 1), 1)
    by_pixels  = int(MAX_CAPTURE_PIXELS / (width * dpr * dpr))
    by_texture = int(MAX_CAPTURE_HEIGHT_PX / dpr)
    return max(1, min(int(document_height), by_pixels, by_texture))


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
        Resolve a device key to a Playwright descriptor dict.

        ✅ CHANGED (Sep 2026): reads self.playwright.devices only. The old
        fallback started a SECOND sync_playwright() instance in a throwaway
        thread — a second thread-affinity hazard for a case that cannot happen
        (capture_screenshot() awaits initialize() before calling this).
        """
        playwright_name = SUPPORTED_DEVICES.get(device_key)
        if not playwright_name:
            return None
        if self.playwright is None:
            raise RuntimeError(
                "Playwright is not initialized — cannot resolve device presets."
            )
        descriptor = self.playwright.devices.get(playwright_name)
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
            logger.info("✅ Playwright browser initialized (sync mode, 1 worker thread)")
        except Exception as e:
            msg = _friendly_playwright_init_error(e)
            self._init_error = msg
            self._initialized = False
            logger.error("❌ Failed to initialize Playwright: %s", msg)
            raise RuntimeError(msg) from e

    def _sync_initialize(self) -> None:
        """The ONLY sync_playwright()/chromium.launch() call in this module.

        Runs on the single playwright worker thread, which then owns every
        Playwright object created from it.
        """
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=True,
            args=_CHROMIUM_ARGS,
        )
        logger.info(
            "🚀 Chromium launched on thread %s", threading.current_thread().name
        )

    # ✅ NEW (Sep 2026): browser liveness + relaunch.
    #
    # MUST be called from the playwright worker thread — it is, because its
    # only caller (_sync_capture_with_recovery) runs there.
    def _sync_ensure_browser(self) -> None:
        if self.playwright is None:
            self._sync_initialize()
            self._initialized = True
            self._init_error = None
            return

        if self.browser is not None and self.browser.is_connected():
            return

        logger.warning(
            "♻️ Chromium is not connected (process died or was closed) — relaunching"
        )
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:
            pass
        self.browser = self.playwright.chromium.launch(
            headless=True,
            args=_CHROMIUM_ARGS,
        )
        self._initialized = True
        self._init_error = None

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

        ⚠️ wait_until and timeout are DEAD PARAMETERS (Aug 2026). Navigation
        uses NAV_TIMEOUT_MS / COMMIT_TIMEOUT_MS with fixed wait conditions.

        Result dict gained two keys in Sep 2026:
          truncated:       bool  — the page was taller than the capture budget
          document_height: int   — full document height in CSS px (0 if unknown)
        """
        fmt = (format or "png").lower().strip()

        if fmt not in SUPPORTED_FORMATS:
            if fmt == "webp" and not PILLOW_AVAILABLE:
                raise ValueError(
                    f"WebP format requires Pillow. Install with: pip install Pillow. "
                    f"Supported formats: {SUPPORTED_FORMATS}"
                )
            raise ValueError(f"Unsupported format: {fmt}. Must be one of: {SUPPORTED_FORMATS}")

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

        # ✅ CHANGED (Sep 2026): dispatch to the recovery wrapper, not straight
        # to _sync_capture_screenshot.
        return await loop.run_in_executor(
            _executor,
            self._sync_capture_with_recovery,
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

    # ── Recovery wrapper (runs on the playwright worker thread) ──────────────

    # ✅ NEW (Sep 2026). Everything Playwright-facing now goes through here:
    #   1. make sure a live browser exists (relaunch if the process died),
    #   2. run the capture,
    #   3. if the browser died DURING the capture, relaunch and retry once.
    # A second failure is surfaced normally.
    def _sync_capture_with_recovery(self, *args: Any) -> Dict[str, Any]:
        last_exc: Optional[BaseException] = None
        for attempt in (1, 2):
            self._sync_ensure_browser()
            try:
                return self._sync_capture_screenshot(*args)
            except PlaywrightError as e:
                last_exc = e
                if attempt == 1 and _is_browser_gone(e):
                    logger.warning(
                        "♻️ Browser died mid-capture — relaunching and retrying once: %s", e
                    )
                    self.browser = None
                    continue
                raise
        raise last_exc or RuntimeError("Screenshot capture failed")

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
        All Playwright calls happen here. Runs on the single worker thread.

        Execution order inside the page:
          1. Build browser context (device descriptor overrides viewport/UA/DPR)
          2. block trackers (per-pattern routes), navigate ONCE
             (domcontentloaded → commit), then bounded networkidle settle
          3. wait_for_selector (non-fatal)
          4. remove_elements JS (non-fatal per-selector)
          5. custom_js page.evaluate() (option-c: non-fatal)
          6. 500ms settle wait
          7. user delay
         7b. lazy-load scroll pass (full_page or target_element, non-fatal)
          8. [Phase 2] resolve target_element bounding box
         8b. [Sep 2026] measure document height, compute the capture clip
          9. capture (clipped when the document exceeds the capture budget)
         10. [Phase 2] Pillow crop to bounding box
         11. WebP re-encode or PDF
         12. Temp file cleanup
        """
        if not self.browser:
            raise RuntimeError("Playwright browser is not initialized")

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        random_id = secrets.token_hex(8)
        js_warning: Optional[str] = None

        temp_filepath: Optional[Path] = None   # always cleaned up in finally

        if target_element:
            temp_filename  = f"screenshot_{timestamp}_{random_id}_full.png"
            temp_filepath  = SCREENSHOTS_DIR / temp_filename
            if fmt == "webp":
                final_filename = f"screenshot_{timestamp}_{random_id}.webp"
            elif fmt in ("jpeg", "jpg"):
                final_filename = f"screenshot_{timestamp}_{random_id}.jpg"
            elif fmt == "pdf":
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
            temp_filename  = f"screenshot_{timestamp}_{random_id}.png"
            final_filename = f"screenshot_{timestamp}_{random_id}.webp"
            temp_filepath  = SCREENSHOTS_DIR / temp_filename
            filepath       = SCREENSHOTS_DIR / final_filename
            filename       = final_filename

        else:
            filename = f"screenshot_{timestamp}_{random_id}.{fmt}"
            filepath = SCREENSHOTS_DIR / filename

        # ── 1. Build browser context ──────────────────────────────────────────
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
                "device_scale_factor": 1,
                "color_scheme": "dark" if dark_mode else "light",
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                ),
            }

        context = self.browser.new_context(**context_kwargs)
        page    = context.new_page()

        phase = "setup"
        capture_truncated = False
        document_height   = 0

        try:
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
            #
            # ✅ FIX (Sep 2026): tracker blocking no longer uses a catch-all
            # route. page.route("**/*", …) makes Chromium hand EVERY request to
            # Python and wait for a verdict; on a page with hundreds of
            # subresources that overhead alone can blow the navigation budget,
            # and the user then gets told the WEBSITE was slow. Registering one
            # route per tracker pattern lets Playwright push those patterns down
            # to the browser, so non-tracker requests are never intercepted.
            if BLOCK_TRACKERS:
                def _abort_tracker(route):
                    try:
                        return route.abort()
                    except Exception:
                        try:
                            return route.continue_()
                        except Exception:
                            return None

                blocked_patterns = 0
                for pattern in _TRACKER_PATTERNS:
                    try:
                        page.route(f"**{pattern}**", _abort_tracker)
                        blocked_patterns += 1
                    except Exception as route_err:
                        logger.warning(
                            "⚠️ Tracker route %r unavailable (non-fatal): %s",
                            pattern, route_err,
                        )
                logger.info("🛡 Tracker routes registered: %d", blocked_patterns)

            phase = "navigation"
            page_loaded = False
            last_error: Optional[Exception] = None

            # Attempt 1 — domcontentloaded.
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                page_loaded = True
            except PlaywrightError as e:
                last_error = e
                if "Timeout" not in str(e):
                    raise
                logger.info(
                    "⏱ domcontentloaded timed out after %dms — retrying with commit",
                    NAV_TIMEOUT_MS,
                )

            # Attempt 2 — commit.
            if not page_loaded:
                try:
                    page.goto(url, wait_until="commit", timeout=COMMIT_TIMEOUT_MS)
                    page_loaded = True
                    logger.info("✅ Navigated with commit (page still loading)")
                except PlaywrightError as e2:
                    last_error = e2

            if not page_loaded:
                raise last_error or PlaywrightError("Navigation failed")

            # Settle — ask for quiet, accept not getting it.
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
            if full_page or target_element:
                phase = "lazy_scroll"
                try:
                    page.set_default_timeout(LAZY_SCROLL_TIMEOUT_MS)
                    page.evaluate(_LAZY_SCROLL_JS)
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
            element_bbox: Optional[Dict[str, float]] = None
            device_scale_factor: float = 1.0

            if target_element:
                phase = "element_lookup"
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

            # ── 8b. Capture size budget ───────────────────────────────────────
            #
            # ✅ NEW (Sep 2026). THIS is what kept killing Chromium. A full-page
            # capture rasterises width × document_height × 4 bytes at once; at
            # 3440px wide on a tall listing page that is hundreds of MB and the
            # OOM killer takes the browser, which then takes every later capture
            # with it ("Target page, context or browser has been closed").
            #
            # Over-budget pages are trimmed instead. PDF is exempt — page.pdf()
            # is paginated vector output, not one giant bitmap.
            capture_clip: Optional[Dict[str, float]] = None

            if (full_page or target_element) and fmt != "pdf":
                try:
                    document_height = int(page.evaluate(_DOC_HEIGHT_JS) or 0)
                except Exception:
                    document_height = 0

                if document_height > 0:
                    safe_height = _full_page_clip_height(
                        actual_viewport_width, document_height, device_scale_factor_ctx
                    )

                    # Never clip away the element we were asked to crop to.
                    if element_bbox is not None:
                        needed = int(element_bbox["y"] + element_bbox["height"] + 8)
                        hard_cap = int(MAX_CAPTURE_HEIGHT_PX / max(device_scale_factor_ctx, 1.0))
                        safe_height = max(safe_height, min(needed, hard_cap))

                    if safe_height < document_height:
                        capture_truncated = True
                        capture_clip = {
                            "x": 0, "y": 0,
                            "width":  float(actual_viewport_width),
                            "height": float(safe_height),
                        }
                        logger.warning(
                            "✂️ Page is %dpx tall at %dpx wide (dpr=%.2f) — over the "
                            "capture budget. Trimming to %dpx to protect the browser "
                            "process.",
                            document_height, actual_viewport_width,
                            device_scale_factor_ctx, safe_height,
                        )

            # ── 9. Capture ────────────────────────────────────────────────────
            phase = "capture"

            if target_element:
                shot_opts: Dict[str, Any] = {
                    "path": str(temp_filepath),
                    "full_page": True,
                    "type": "png",
                    "timeout": CAPTURE_TIMEOUT_MS,
                }
                if capture_clip:
                    shot_opts["clip"] = capture_clip
                page.screenshot(**shot_opts)

            elif fmt == "pdf":
                page.pdf(
                    path=str(filepath), format="A4", print_background=True,
                )

            elif fmt == "webp":
                shot_opts = {
                    "path": str(temp_filepath),
                    "full_page": bool(full_page),
                    "type": "png",
                    "timeout": CAPTURE_TIMEOUT_MS,
                }
                if capture_clip:
                    shot_opts["clip"] = capture_clip
                page.screenshot(**shot_opts)

            else:
                options: Dict[str, Any] = {
                    "path": str(filepath),
                    "full_page": bool(full_page),
                    "timeout": CAPTURE_TIMEOUT_MS,
                }
                if capture_clip:
                    options["clip"] = capture_clip
                if fmt in ("jpeg", "jpg"):
                    options["type"] = "jpeg"
                    options["quality"] = 90
                else:
                    options["type"] = "png"
                page.screenshot(**options)

            # ── 10. Phase 2: Pillow crop ──────────────────────────────────────
            if target_element and element_bbox is not None:
                phase = "crop"
                dpr = device_scale_factor
                x      = int(element_bbox["x"]      * dpr)
                y      = int(element_bbox["y"]      * dpr)
                x2     = int((element_bbox["x"] + element_bbox["width"])  * dpr)
                y2     = int((element_bbox["y"] + element_bbox["height"]) * dpr)

                full_img = Image.open(str(temp_filepath))
                img_w, img_h = full_img.size

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
                phase = "encode"
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
                "js_warning=%s element=%s truncated=%s",
                filename, fmt, file_size,
                bool(js_warning),
                repr(target_element) if target_element else "no",
                capture_truncated,
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
                "js_warning":       js_warning,           # Phase 1
                "element_selector": target_element,       # Phase 2 (None if unused)
                "truncated":        bool(capture_truncated),   # ✅ Sep 2026
                "document_height":  int(document_height),      # ✅ Sep 2026
            }

        except PlaywrightError as e:
            error_msg = str(e)

            # ✅ NEW (Sep 2026): a dead browser is not a capture failure — it is
            # an infrastructure failure. Re-raise it unchanged so
            # _sync_capture_with_recovery() can relaunch and retry. Wrapping it
            # in ValueError here would hide it from the recovery path.
            if _is_browser_gone(e):
                logger.error(
                    "💥 Browser process is gone during %s for %s: %s", phase, url, error_msg
                )
                raise

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
    """
    limit = tier_limits.get("screenshots")
    if limit == "unlimited" or limit is None:
        return True

    try:
        limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        logger.error("check_usage_limit: unparseable limit %r — allowing", limit)
        return True

    if db is not None:
        try:
            from usage_accounting import screenshots_used_this_period
            return screenshots_used_this_period(db, user) < limit
        except Exception as e:
            logger.warning(
                "check_usage_limit: usage_accounting unavailable, "
                "falling back to user.usage_screenshots (%s)", e
            )
    else:
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
# #
# # ⚠️ THE SEP 2026 FIXES BELOW ARE NOT YET APPLIED TO services/screenshot_service.py.

# # ============================================================================
# # SCREENSHOT SERVICE - PixelPerfect API (PRODUCTION READY)
# # File: backend/screenshot_service.py
# # Author: OneTechly
# # Updated: September 2026
# # ============================================================================
# #
# # ✅ FIX (Sep 2026 — "Cannot switch to a different thread")  ← THE BATCH BUG
# #
# #   _executor was ThreadPoolExecutor(max_workers=3). Playwright's SYNC API is
# #   greenlet-based and thread-affine: the Playwright object, the Browser, the
# #   BrowserContext and the Page may only be touched from the thread that
# #   called sync_playwright().start().
# #
# #   initialize() runs _sync_initialize() on ONE of those three worker threads,
# #   so the Browser belongs to that thread. Captures were then dispatched to
# #   whichever of the three threads was free, so roughly two out of every three
# #   captures raised greenlet.error("Cannot switch to a different thread")
# #   instantly — 0 bytes, 0.01s, "failed". That is exactly the pattern in the
# #   batch job list.
# #
# #   Fix: max_workers=1. One worker thread means initialize(), every capture and
# #   close() all run on the same thread, which is the only arrangement the sync
# #   API supports. Captures were already serialised in practice (one shared
# #   Browser, one 1-CPU instance), so this costs no real throughput.
# #
# # ✅ FIX (Sep 2026 — "Target page, context or browser has been closed")
# #
# #   When Chromium dies (an ultrawide full-page capture of a very tall document
# #   is hundreds of MB of raw bitmap, and the OOM killer takes the browser
# #   process), self.browser stayed set to the dead object and every later
# #   capture failed on new_context(). _sync_ensure_browser() now checks
# #   browser.is_connected() before each capture and relaunches on the SAME
# #   worker thread, and _sync_capture_with_recovery() retries a capture once if
# #   the browser dies mid-flight.
# #
# # ✅ NEW (Sep 2026 — full-page capture size budget)
# #
# #   The reason Chromium died in the first place. A full-page capture at 3440px
# #   wide of a page that renders 20,000px tall is 3440 × 20000 × 4 bytes ≈ 275 MB
# #   of raw bitmap before compression, plus compositor tiles. On a 2 GB Render
# #   instance running two Chromium instances that is fatal.
# #
# #   _full_page_clip_height() caps the rasterised area (default 40M pixels, and
# #   never taller than Chromium's 16,384px texture limit, both adjusted for the
# #   device pixel ratio). Over-tall pages are now TRIMMED instead of killing the
# #   browser, and the result dict carries truncated / document_height so callers
# #   can say so. PDF is unaffected — it is not rasterised this way.
# #
# # ✅ FIX (Sep 2026 — tracker blocking intercepted every request)
# #
# #   page.route("**/*", …) makes Playwright intercept EVERY request and round-trip
# #   it to Python before the browser may continue. On a page with several hundred
# #   subresources that alone can push domcontentloaded past NAV_TIMEOUT_MS — the
# #   timeout then gets reported as the website being slow. Routes are now
# #   registered per tracker pattern, so Chromium only intercepts requests that
# #   can actually match, and everything else goes straight through.
# #
# # ✅ REMOVED (Sep 2026 — second sync_playwright() entry point)
# #
# #   _get_device_descriptor_sync() started a SECOND Playwright instance in a
# #   throwaway thread whenever self.playwright was None. It was dead code on the
# #   happy path and a second thread-affinity hazard otherwise. _sync_initialize()
# #   is now the only sync_playwright()/chromium.launch() call in this file.
# #
# # ----------------------------------------------------------------------------
# # Earlier fixes (all retained):
# # ✅ is_ready() checks browser availability
# # ✅ No db.commit() — caller controls transaction
# # ✅ PLAYWRIGHT_BROWSERS_PATH-aware guidance
# # ✅ WebP support via Pillow (PNG → WebP)
# # ✅ Safer cleanup for temp files
# # ✅ FIX (Mar 2026 v1): get_screenshot_url prefers CUSTOM_API_DOMAIN in prod
# # ✅ FIX (Mar 2026 v2): get_screenshot_url is environment-aware
# # ✅ FIX (Apr 2026): Playwright timeouts configurable via env vars.
# # ✅ NEW (Apr 2026): `delay` and `remove_elements` parameters now honored.
# # ✅ NEW (May 2026 — Phase 1): Device emulation (Pro+) and Custom JavaScript.
# # ✅ FIX (May 2026 — Phase 1): _get_device_descriptor no longer calls
# #    sync_playwright() inside the asyncio loop.
# # ✅ NEW (May 2026 — Phase 2): Element Selection (Business+) implemented.
# # ✅ REWRITTEN (Aug 2026 — heavy-site navigation): PATCH A/B/C.
# # ✅ FIX (Aug 2026 — boot failure): LAZY_SCROLL_TIMEOUT_MS restored.
# # ✅ NEW (Aug 2026 — phase-aware timeout reporting).
# #
# #    ⚠️ `wait_until` and `timeout` are DEAD PARAMETERS on both
# #       capture_screenshot() and _sync_capture_screenshot().
# #
# # ============================================================================
# # Phase 2 notes (May 2026) — Option A bounding-box crop:
# #   1. Capture a full-page PNG of the entire document (Playwright).
# #   2. Resolve the element's bounding box via page.evaluate().
# #   3. Scale by deviceScaleFactor to physical pixels.
# #   4. Crop with Pillow and save to the final output path.
# #   Errors: element not found / zero size → ValueError (HTTP 400 via router).
# #   Temp full-page PNG always removed in the finally block.
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

# DEFAULT_TIMEOUT  = int(os.getenv("PLAYWRIGHT_DEFAULT_TIMEOUT_MS",  "30000"))   # legacy, still read
# FALLBACK_TIMEOUT = int(os.getenv("PLAYWRIGHT_FALLBACK_TIMEOUT_MS", "35000"))   # legacy, still read

# # Primary navigation. domcontentloaded fires as soon as the HTML is parsed.
# NAV_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_NAV_TIMEOUT_MS", "25000"))

# # Last-resort navigation. "commit" resolves as soon as response headers arrive.
# COMMIT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_COMMIT_TIMEOUT_MS", "15000"))

# # Optional settle window. We ASK for networkidle and accept not getting it.
# SETTLE_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_SETTLE_TIMEOUT_MS", "8000"))

# # Budget for auto-scrolling a full-page capture to trigger lazy-loaded content.
# # ⚠️ Do not comment this out — the startup banner reads it at MODULE level.
# LAZY_SCROLL_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_LAZY_SCROLL_TIMEOUT_MS", "6000"))

# # Explicit budget for the capture itself (see Aug 2026 note).
# CAPTURE_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_CAPTURE_TIMEOUT_MS", "60000"))

# # ✅ NEW (Sep 2026): full-page capture size budget.
# #
# # Chromium cannot reliably rasterise a texture taller than 16,384px, and a
# # large bitmap is what kills the browser process on a 2 GB instance:
# #   3440 × 20000 × 4 bytes ≈ 275 MB raw, before compositor overhead.
# # 40M pixels ≈ 160 MB raw, which survives comfortably.
# MAX_CAPTURE_HEIGHT_PX = int(os.getenv("PLAYWRIGHT_MAX_CAPTURE_HEIGHT_PX", "16384"))
# MAX_CAPTURE_PIXELS    = int(os.getenv("PLAYWRIGHT_MAX_CAPTURE_PIXELS",    "40000000"))

# # Chromium launch flags — one definition, used by initial launch AND relaunch.
# _CHROMIUM_ARGS = [
#     "--no-sandbox",
#     "--disable-setuid-sandbox",
#     "--disable-dev-shm-usage",
#     "--disable-gpu",
# ]

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

# # Auto-scroll script for lazy-loaded content.
# _LAZY_SCROLL_JS = """
# async () => {
#   await new Promise((resolve) => {
#     let total = 0;
#     const step = Math.max(200, Math.floor(window.innerHeight * 0.85));
#     const timer = setInterval(() => {
#       const height = document.body.scrollHeight;
#       window.scrollBy(0, step);
#       total += step;
#       if (total >= height || total > step * 50) {
#         clearInterval(timer);
#         window.scrollTo(0, 0);
#         resolve();
#       }
#     }, 90);
#   });
# }
# """

# # ✅ NEW (Sep 2026): document height probe for the capture budget.
# _DOC_HEIGHT_JS = """
# () => Math.max(
#   document.documentElement ? document.documentElement.scrollHeight : 0,
#   document.body ? document.body.scrollHeight : 0
# )
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

# # ✅ FIX (Sep 2026 — THE BATCH BUG): max_workers=3 → 1.
# #
# # Playwright's sync API is thread-affine. With three workers, the Browser was
# # created on whichever thread ran initialize() and every capture that landed on
# # one of the other two raised greenlet.error("Cannot switch to a different
# # thread") instantly. One worker == one owning thread == the only arrangement
# # the sync API supports. Do not raise this number. If you need real capture
# # concurrency, run a second ScreenshotService with its OWN executor and its OWN
# # browser, and size it against available RAM.
# _executor  = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")
# _init_lock = threading.Lock()

# logger.info(
#     "📸 Playwright navigation budget: nav=%dms commit=%dms settle=%dms "
#     "lazy_scroll=%dms capture=%dms (worst-case=%dms) | block_trackers=%s | "
#     "max_capture=%dpx/%dMpx | workers=1 | legacy DEFAULT=%dms FALLBACK=%dms",
#     NAV_TIMEOUT_MS, COMMIT_TIMEOUT_MS, SETTLE_TIMEOUT_MS, LAZY_SCROLL_TIMEOUT_MS,
#     CAPTURE_TIMEOUT_MS,
#     NAV_TIMEOUT_MS + COMMIT_TIMEOUT_MS + SETTLE_TIMEOUT_MS
#     + LAZY_SCROLL_TIMEOUT_MS + CAPTURE_TIMEOUT_MS,
#     BLOCK_TRACKERS,
#     MAX_CAPTURE_HEIGHT_PX, MAX_CAPTURE_PIXELS // 1_000_000,
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


# # ✅ NEW (Sep 2026): recognise "the browser process is gone" so the caller can
# # relaunch instead of failing every subsequent capture against a dead object.
# _BROWSER_GONE_MARKERS = (
#     "target page, context or browser has been closed",
#     "browser has been closed",
#     "target closed",
#     "browser closed",
#     "connection closed",
#     "browser has disconnected",
# )


# def _is_browser_gone(exc: BaseException) -> bool:
#     msg = str(exc).lower()
#     return any(marker in msg for marker in _BROWSER_GONE_MARKERS)


# # ✅ NEW (Sep 2026): how tall a full-page capture may be before it endangers
# # the browser process. Returns a height in CSS pixels.
# def _full_page_clip_height(
#     css_width: int, document_height: int, device_scale_factor: float = 1.0
# ) -> int:
#     dpr = max(float(device_scale_factor or 1.0), 1.0)
#     width = max(int(css_width or 1), 1)
#     by_pixels  = int(MAX_CAPTURE_PIXELS / (width * dpr * dpr))
#     by_texture = int(MAX_CAPTURE_HEIGHT_PX / dpr)
#     return max(1, min(int(document_height), by_pixels, by_texture))


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
#         Resolve a device key to a Playwright descriptor dict.

#         ✅ CHANGED (Sep 2026): reads self.playwright.devices only. The old
#         fallback started a SECOND sync_playwright() instance in a throwaway
#         thread — a second thread-affinity hazard for a case that cannot happen
#         (capture_screenshot() awaits initialize() before calling this).
#         """
#         playwright_name = SUPPORTED_DEVICES.get(device_key)
#         if not playwright_name:
#             return None
#         if self.playwright is None:
#             raise RuntimeError(
#                 "Playwright is not initialized — cannot resolve device presets."
#             )
#         descriptor = self.playwright.devices.get(playwright_name)
#         return dict(descriptor) if descriptor else None

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
#             logger.info("✅ Playwright browser initialized (sync mode, 1 worker thread)")
#         except Exception as e:
#             msg = _friendly_playwright_init_error(e)
#             self._init_error = msg
#             self._initialized = False
#             logger.error("❌ Failed to initialize Playwright: %s", msg)
#             raise RuntimeError(msg) from e

#     def _sync_initialize(self) -> None:
#         """The ONLY sync_playwright()/chromium.launch() call in this module.

#         Runs on the single playwright worker thread, which then owns every
#         Playwright object created from it.
#         """
#         self.playwright = sync_playwright().start()
#         self.browser = self.playwright.chromium.launch(
#             headless=True,
#             args=_CHROMIUM_ARGS,
#         )
#         logger.info(
#             "🚀 Chromium launched on thread %s", threading.current_thread().name
#         )

#     # ✅ NEW (Sep 2026): browser liveness + relaunch.
#     #
#     # MUST be called from the playwright worker thread — it is, because its
#     # only caller (_sync_capture_with_recovery) runs there.
#     def _sync_ensure_browser(self) -> None:
#         if self.playwright is None:
#             self._sync_initialize()
#             self._initialized = True
#             self._init_error = None
#             return

#         if self.browser is not None and self.browser.is_connected():
#             return

#         logger.warning(
#             "♻️ Chromium is not connected (process died or was closed) — relaunching"
#         )
#         try:
#             if self.browser is not None:
#                 self.browser.close()
#         except Exception:
#             pass
#         self.browser = self.playwright.chromium.launch(
#             headless=True,
#             args=_CHROMIUM_ARGS,
#         )
#         self._initialized = True
#         self._init_error = None

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

#         ⚠️ wait_until and timeout are DEAD PARAMETERS (Aug 2026). Navigation
#         uses NAV_TIMEOUT_MS / COMMIT_TIMEOUT_MS with fixed wait conditions.

#         Result dict gained two keys in Sep 2026:
#           truncated:       bool  — the page was taller than the capture budget
#           document_height: int   — full document height in CSS px (0 if unknown)
#         """
#         fmt = (format or "png").lower().strip()

#         if fmt not in SUPPORTED_FORMATS:
#             if fmt == "webp" and not PILLOW_AVAILABLE:
#                 raise ValueError(
#                     f"WebP format requires Pillow. Install with: pip install Pillow. "
#                     f"Supported formats: {SUPPORTED_FORMATS}"
#                 )
#             raise ValueError(f"Unsupported format: {fmt}. Must be one of: {SUPPORTED_FORMATS}")

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

#         # ✅ CHANGED (Sep 2026): dispatch to the recovery wrapper, not straight
#         # to _sync_capture_screenshot.
#         return await loop.run_in_executor(
#             _executor,
#             self._sync_capture_with_recovery,
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

#     # ── Recovery wrapper (runs on the playwright worker thread) ──────────────

#     # ✅ NEW (Sep 2026). Everything Playwright-facing now goes through here:
#     #   1. make sure a live browser exists (relaunch if the process died),
#     #   2. run the capture,
#     #   3. if the browser died DURING the capture, relaunch and retry once.
#     # A second failure is surfaced normally.
#     def _sync_capture_with_recovery(self, *args: Any) -> Dict[str, Any]:
#         last_exc: Optional[BaseException] = None
#         for attempt in (1, 2):
#             self._sync_ensure_browser()
#             try:
#                 return self._sync_capture_screenshot(*args)
#             except PlaywrightError as e:
#                 last_exc = e
#                 if attempt == 1 and _is_browser_gone(e):
#                     logger.warning(
#                         "♻️ Browser died mid-capture — relaunching and retrying once: %s", e
#                     )
#                     self.browser = None
#                     continue
#                 raise
#         raise last_exc or RuntimeError("Screenshot capture failed")

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
#         All Playwright calls happen here. Runs on the single worker thread.

#         Execution order inside the page:
#           1. Build browser context (device descriptor overrides viewport/UA/DPR)
#           2. block trackers (per-pattern routes), navigate ONCE
#              (domcontentloaded → commit), then bounded networkidle settle
#           3. wait_for_selector (non-fatal)
#           4. remove_elements JS (non-fatal per-selector)
#           5. custom_js page.evaluate() (option-c: non-fatal)
#           6. 500ms settle wait
#           7. user delay
#          7b. lazy-load scroll pass (full_page or target_element, non-fatal)
#           8. [Phase 2] resolve target_element bounding box
#          8b. [Sep 2026] measure document height, compute the capture clip
#           9. capture (clipped when the document exceeds the capture budget)
#          10. [Phase 2] Pillow crop to bounding box
#          11. WebP re-encode or PDF
#          12. Temp file cleanup
#         """
#         if not self.browser:
#             raise RuntimeError("Playwright browser is not initialized")

#         timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
#         random_id = secrets.token_hex(8)
#         js_warning: Optional[str] = None

#         temp_filepath: Optional[Path] = None   # always cleaned up in finally

#         if target_element:
#             temp_filename  = f"screenshot_{timestamp}_{random_id}_full.png"
#             temp_filepath  = SCREENSHOTS_DIR / temp_filename
#             if fmt == "webp":
#                 final_filename = f"screenshot_{timestamp}_{random_id}.webp"
#             elif fmt in ("jpeg", "jpg"):
#                 final_filename = f"screenshot_{timestamp}_{random_id}.jpg"
#             elif fmt == "pdf":
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
#             temp_filename  = f"screenshot_{timestamp}_{random_id}.png"
#             final_filename = f"screenshot_{timestamp}_{random_id}.webp"
#             temp_filepath  = SCREENSHOTS_DIR / temp_filename
#             filepath       = SCREENSHOTS_DIR / final_filename
#             filename       = final_filename

#         else:
#             filename = f"screenshot_{timestamp}_{random_id}.{fmt}"
#             filepath = SCREENSHOTS_DIR / filename

#         # ── 1. Build browser context ──────────────────────────────────────────
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
#                 "device_scale_factor": 1,
#                 "color_scheme": "dark" if dark_mode else "light",
#                 "user_agent": (
#                     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
#                 ),
#             }

#         context = self.browser.new_context(**context_kwargs)
#         page    = context.new_page()

#         phase = "setup"
#         capture_truncated = False
#         document_height   = 0

#         try:
#             logger.info(
#                 "📸 Capturing screenshot: %s (format=%s nav=%dms commit=%dms "
#                 "settle=%dms capture=%dms delay=%ds remove=%d device=%s "
#                 "custom_js=%s target_element=%s)",
#                 url, fmt, NAV_TIMEOUT_MS, COMMIT_TIMEOUT_MS, SETTLE_TIMEOUT_MS,
#                 CAPTURE_TIMEOUT_MS, delay,
#                 len(remove_elements),
#                 "yes" if device_descriptor else "no",
#                 "yes" if custom_js else "no",
#                 repr(target_element) if target_element else "no",
#             )

#             # ── 2. Navigate ONCE, then settle ─────────────────────────────────
#             #
#             # ✅ FIX (Sep 2026): tracker blocking no longer uses a catch-all
#             # route. page.route("**/*", …) makes Chromium hand EVERY request to
#             # Python and wait for a verdict; on a page with hundreds of
#             # subresources that overhead alone can blow the navigation budget,
#             # and the user then gets told the WEBSITE was slow. Registering one
#             # route per tracker pattern lets Playwright push those patterns down
#             # to the browser, so non-tracker requests are never intercepted.
#             if BLOCK_TRACKERS:
#                 def _abort_tracker(route):
#                     try:
#                         return route.abort()
#                     except Exception:
#                         try:
#                             return route.continue_()
#                         except Exception:
#                             return None

#                 blocked_patterns = 0
#                 for pattern in _TRACKER_PATTERNS:
#                     try:
#                         page.route(f"**{pattern}**", _abort_tracker)
#                         blocked_patterns += 1
#                     except Exception as route_err:
#                         logger.warning(
#                             "⚠️ Tracker route %r unavailable (non-fatal): %s",
#                             pattern, route_err,
#                         )
#                 logger.info("🛡 Tracker routes registered: %d", blocked_patterns)

#             phase = "navigation"
#             page_loaded = False
#             last_error: Optional[Exception] = None

#             # Attempt 1 — domcontentloaded.
#             try:
#                 page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
#                 page_loaded = True
#             except PlaywrightError as e:
#                 last_error = e
#                 if "Timeout" not in str(e):
#                     raise
#                 logger.info(
#                     "⏱ domcontentloaded timed out after %dms — retrying with commit",
#                     NAV_TIMEOUT_MS,
#                 )

#             # Attempt 2 — commit.
#             if not page_loaded:
#                 try:
#                     page.goto(url, wait_until="commit", timeout=COMMIT_TIMEOUT_MS)
#                     page_loaded = True
#                     logger.info("✅ Navigated with commit (page still loading)")
#                 except PlaywrightError as e2:
#                     last_error = e2

#             if not page_loaded:
#                 raise last_error or PlaywrightError("Navigation failed")

#             # Settle — ask for quiet, accept not getting it.
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
#             if full_page or target_element:
#                 phase = "lazy_scroll"
#                 try:
#                     page.set_default_timeout(LAZY_SCROLL_TIMEOUT_MS)
#                     page.evaluate(_LAZY_SCROLL_JS)
#                     page.wait_for_timeout(600)
#                     logger.info("📜 Lazy-load scroll pass complete")
#                 except Exception as scroll_err:
#                     logger.warning(
#                         "⚠️ Lazy-load scroll failed (capture continues): %s", scroll_err
#                     )
#                 finally:
#                     try:
#                         page.set_default_timeout(CAPTURE_TIMEOUT_MS)
#                     except Exception:
#                         pass

#             # ── 8. Phase 2: resolve element bounding box ──────────────────────
#             element_bbox: Optional[Dict[str, float]] = None
#             device_scale_factor: float = 1.0

#             if target_element:
#                 phase = "element_lookup"
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

#             # ── 8b. Capture size budget ───────────────────────────────────────
#             #
#             # ✅ NEW (Sep 2026). THIS is what kept killing Chromium. A full-page
#             # capture rasterises width × document_height × 4 bytes at once; at
#             # 3440px wide on a tall listing page that is hundreds of MB and the
#             # OOM killer takes the browser, which then takes every later capture
#             # with it ("Target page, context or browser has been closed").
#             #
#             # Over-budget pages are trimmed instead. PDF is exempt — page.pdf()
#             # is paginated vector output, not one giant bitmap.
#             capture_clip: Optional[Dict[str, float]] = None

#             if (full_page or target_element) and fmt != "pdf":
#                 try:
#                     document_height = int(page.evaluate(_DOC_HEIGHT_JS) or 0)
#                 except Exception:
#                     document_height = 0

#                 if document_height > 0:
#                     safe_height = _full_page_clip_height(
#                         actual_viewport_width, document_height, device_scale_factor_ctx
#                     )

#                     # Never clip away the element we were asked to crop to.
#                     if element_bbox is not None:
#                         needed = int(element_bbox["y"] + element_bbox["height"] + 8)
#                         hard_cap = int(MAX_CAPTURE_HEIGHT_PX / max(device_scale_factor_ctx, 1.0))
#                         safe_height = max(safe_height, min(needed, hard_cap))

#                     if safe_height < document_height:
#                         capture_truncated = True
#                         capture_clip = {
#                             "x": 0, "y": 0,
#                             "width":  float(actual_viewport_width),
#                             "height": float(safe_height),
#                         }
#                         logger.warning(
#                             "✂️ Page is %dpx tall at %dpx wide (dpr=%.2f) — over the "
#                             "capture budget. Trimming to %dpx to protect the browser "
#                             "process.",
#                             document_height, actual_viewport_width,
#                             device_scale_factor_ctx, safe_height,
#                         )

#             # ── 9. Capture ────────────────────────────────────────────────────
#             phase = "capture"

#             if target_element:
#                 shot_opts: Dict[str, Any] = {
#                     "path": str(temp_filepath),
#                     "full_page": True,
#                     "type": "png",
#                     "timeout": CAPTURE_TIMEOUT_MS,
#                 }
#                 if capture_clip:
#                     shot_opts["clip"] = capture_clip
#                 page.screenshot(**shot_opts)

#             elif fmt == "pdf":
#                 page.pdf(
#                     path=str(filepath), format="A4", print_background=True,
#                 )

#             elif fmt == "webp":
#                 shot_opts = {
#                     "path": str(temp_filepath),
#                     "full_page": bool(full_page),
#                     "type": "png",
#                     "timeout": CAPTURE_TIMEOUT_MS,
#                 }
#                 if capture_clip:
#                     shot_opts["clip"] = capture_clip
#                 page.screenshot(**shot_opts)

#             else:
#                 options: Dict[str, Any] = {
#                     "path": str(filepath),
#                     "full_page": bool(full_page),
#                     "timeout": CAPTURE_TIMEOUT_MS,
#                 }
#                 if capture_clip:
#                     options["clip"] = capture_clip
#                 if fmt in ("jpeg", "jpg"):
#                     options["type"] = "jpeg"
#                     options["quality"] = 90
#                 else:
#                     options["type"] = "png"
#                 page.screenshot(**options)

#             # ── 10. Phase 2: Pillow crop ──────────────────────────────────────
#             if target_element and element_bbox is not None:
#                 phase = "crop"
#                 dpr = device_scale_factor
#                 x      = int(element_bbox["x"]      * dpr)
#                 y      = int(element_bbox["y"]      * dpr)
#                 x2     = int((element_bbox["x"] + element_bbox["width"])  * dpr)
#                 y2     = int((element_bbox["y"] + element_bbox["height"]) * dpr)

#                 full_img = Image.open(str(temp_filepath))
#                 img_w, img_h = full_img.size

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
#                 phase = "encode"
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
#                 "js_warning=%s element=%s truncated=%s",
#                 filename, fmt, file_size,
#                 bool(js_warning),
#                 repr(target_element) if target_element else "no",
#                 capture_truncated,
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
#                 "js_warning":       js_warning,           # Phase 1
#                 "element_selector": target_element,       # Phase 2 (None if unused)
#                 "truncated":        bool(capture_truncated),   # ✅ Sep 2026
#                 "document_height":  int(document_height),      # ✅ Sep 2026
#             }

#         except PlaywrightError as e:
#             error_msg = str(e)

#             # ✅ NEW (Sep 2026): a dead browser is not a capture failure — it is
#             # an infrastructure failure. Re-raise it unchanged so
#             # _sync_capture_with_recovery() can relaunch and retry. Wrapping it
#             # in ValueError here would hide it from the recovery path.
#             if _is_browser_gone(e):
#                 logger.error(
#                     "💥 Browser process is gone during %s for %s: %s", phase, url, error_msg
#                 )
#                 raise

#             logger.error(
#                 "❌ Playwright error during %s for %s: %s", phase, url, error_msg
#             )
#             if "Timeout" in error_msg:
#                 url_hint = url[:50] + "..." if len(url) > 50 else url

#                 if phase == "capture":
#                     raise ValueError(
#                         f"The page loaded, but rendering the screenshot took longer "
#                         f"than {CAPTURE_TIMEOUT_MS // 1000}s. This usually means the "
#                         f"page is very tall — try unchecking 'Capture full page', or "
#                         f"capture a more specific section of the site."
#                     ) from e

#                 raise ValueError(
#                     f"The website ({url_hint}) did not respond in time. It may be "
#                     f"very slow, blocking automated access, or continuously loading "
#                     f"content. Try again, or capture a more specific page."
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
#     """
#     limit = tier_limits.get("screenshots")
#     if limit == "unlimited" or limit is None:
#         return True

#     try:
#         limit = int(limit)
#     except (TypeError, ValueError, OverflowError):
#         logger.error("check_usage_limit: unparseable limit %r — allowing", limit)
#         return True

#     if db is not None:
#         try:
#             from usage_accounting import screenshots_used_this_period
#             return screenshots_used_this_period(db, user) < limit
#         except Exception as e:
#             logger.warning(
#                 "check_usage_limit: usage_accounting unavailable, "
#                 "falling back to user.usage_screenshots (%s)", e
#             )
#     else:
#         logger.warning(
#             "check_usage_limit called without db — using non-authoritative counter"
#         )

#     return int(user.usage_screenshots or 0) < limit

# # ===== END OF backend\screenshot_service.py ========

