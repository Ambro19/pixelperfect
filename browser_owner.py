"""
backend/browser_owner.py — PixelPerfect Screenshot API
September 2026

Fixes two production errors:

  1. greenlet.error: "Cannot switch to a different thread"
     Playwright's SYNC API is greenlet-based and thread-affine: the
     Playwright instance, Browser, BrowserContext and Page may only be used
     from the thread that called sync_playwright().start(). Touching a shared
     browser from any other thread (a batch worker, a run_in_executor pool
     thread, a thread that relaunched the browser after a crash) raises this
     error instantly — which is why those items show 0 bytes and no time.

  2. "Browser.new_context: Target page, context or browser has been closed"
     The Chromium process died (typically out of memory: a 3440px-wide
     full-page capture of a very long page is hundreds of MB of raw bitmap),
     and the code kept a reference to the dead Browser object and reused it.

The fix:
  * ONE dedicated thread owns Playwright. Every capture — single or batch,
    sync or async caller — is submitted to that thread and runs there.
    The thread-affinity error becomes structurally impossible.
  * Before every job the owner checks browser.is_connected() and relaunches
    in its own thread if Chromium died. A job that fails because the browser
    died mid-capture is retried once on the fresh browser.
  * full_page_clip_height() caps the rasterised area of full-page captures,
    so ultrawide captures of very long pages can no longer OOM Chromium.

Throughput: one owner runs captures serially. On a 2 GB Render instance that
is the safe default. If you need concurrency, create 2 BrowserOwner instances
(each owns its own thread + Chromium) and round-robin between them; memory
scales with the number of owners.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import threading
from typing import Any, Callable, Optional

from playwright.sync_api import Browser, Error as PlaywrightError, sync_playwright

log = logging.getLogger("pixelperfect.browser")

CHROMIUM_ARGS = [
    "--disable-dev-shm-usage",   # /dev/shm is tiny in containers
    "--no-sandbox",
    "--disable-gpu",
]

# Chromium cannot reliably rasterise textures taller than 16384px, and the
# pixel budget keeps a single capture well inside 2 GB of RAM.
MAX_CAPTURE_HEIGHT_PX = 16_384
MAX_CAPTURE_PIXELS = 40_000_000   # ≈160 MB as raw RGBA

_BROWSER_GONE_MARKERS = (
    "has been closed",
    "target closed",
    "browser closed",
    "connection closed",
)


def is_browser_gone_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _BROWSER_GONE_MARKERS)


def full_page_clip_height(css_width: int, document_height: int, device_scale_factor: float = 1.0) -> int:
    """Largest safe capture height (CSS px) for a full-page screenshot.

    Device presets can use DPR 2–3, which multiplies the real pixel count,
    so pass the context's device_scale_factor.
    """
    dpr = max(float(device_scale_factor or 1.0), 1.0)
    by_pixels = int(MAX_CAPTURE_PIXELS / (max(css_width, 1) * dpr * dpr))
    by_texture = int(MAX_CAPTURE_HEIGHT_PX / dpr)
    return max(1, min(document_height, by_pixels, by_texture))


class BrowserOwner:
    """Owns Playwright + Chromium on one dedicated thread.

    Jobs are callables of the form ``fn(browser, *args, **kwargs)``. They run
    on the owner thread, must create their own BrowserContext and must close
    it in a ``finally`` block.
    """

    def __init__(self, launch_args: Optional[list[str]] = None, name: str = "playwright-owner"):
        self._launch_args = list(launch_args or CHROMIUM_ARGS)
        self._jobs: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._ready = threading.Event()
        self._startup_error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=90):
            raise RuntimeError("Playwright owner thread did not start within 90s")
        if self._startup_error is not None:
            raise RuntimeError("Playwright failed to start") from self._startup_error

    # ── Public API (callable from ANY thread) ────────────────────────────────
    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._jobs.put((fn, args, kwargs, fut))
        return fut

    def call(self, fn: Callable[..., Any], *args: Any, timeout: Optional[float] = None, **kwargs: Any) -> Any:
        """Blocking call — use from batch worker threads.

        Note: a timeout stops the caller waiting; it does not abort the job
        already running on the owner thread. Keep Playwright's own timeouts
        (goto, waits) tighter than this.
        """
        return self.submit(fn, *args, **kwargs).result(timeout=timeout)

    async def acall(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Awaitable call — use from FastAPI async endpoints."""
        return await asyncio.wrap_future(self.submit(fn, *args, **kwargs))

    def shutdown(self, timeout: float = 15.0) -> None:
        self._jobs.put(None)
        self._thread.join(timeout)

    # ── Owner thread ─────────────────────────────────────────────────────────
    def _run(self) -> None:
        try:
            pw = sync_playwright().start()
            browser: Optional[Browser] = pw.chromium.launch(args=self._launch_args)
        except BaseException as exc:  # surface startup failure to __init__
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        log.info("Chromium launched on thread %s", threading.current_thread().name)

        def ensure_browser() -> Browser:
            nonlocal browser
            if browser is not None and browser.is_connected():
                return browser
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            log.warning("Chromium was disconnected; relaunching")
            browser = pw.chromium.launch(args=self._launch_args)
            return browser

        while True:
            job = self._jobs.get()
            if job is None:
                break
            fn, args, kwargs, fut = job
            if not fut.set_running_or_notify_cancel():
                continue

            for attempt in (1, 2):
                try:
                    result = fn(ensure_browser(), *args, **kwargs)
                except PlaywrightError as exc:
                    if attempt == 1 and is_browser_gone_error(exc):
                        log.warning("Capture hit a closed browser; retrying once on a fresh browser: %s", exc)
                        continue
                    fut.set_exception(exc)
                except Exception as exc:
                    fut.set_exception(exc)
                else:
                    fut.set_result(result)
                break

        try:
            if browser is not None:
                browser.close()
        finally:
            pw.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Example capture job — adapt your existing capture function to this shape.
# The only structural requirements: take `browser` first, own the context,
# close it in `finally`, and apply the clip for full-page raster formats.
# ─────────────────────────────────────────────────────────────────────────────
def capture_example(browser: Browser, url: str, width: int, height: int,
                    full_page: bool, image_type: str = "png") -> tuple[bytes, dict]:
    dpr = 1.0
    context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=dpr)
    try:
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # … existing settle window, lazy-load scroll, remove_elements, custom_js …

        meta: dict = {"truncated": False}
        if not full_page:
            return page.screenshot(type=image_type), meta

        doc_h = int(page.evaluate(
            "() => Math.max(document.documentElement.scrollHeight,"
            " document.body ? document.body.scrollHeight : 0)"
        ))
        clip_h = full_page_clip_height(width, doc_h, dpr)
        if clip_h < doc_h:
            meta.update(truncated=True, document_height=doc_h, captured_height=clip_h)
            shot = page.screenshot(type=image_type, full_page=True,
                                   clip={"x": 0, "y": 0, "width": width, "height": clip_h})
            return shot, meta
        return page.screenshot(type=image_type, full_page=True), meta
    finally:
        context.close()


# ─────────────────────────────────────────────────────────────────────────────
# Wiring (sketch)
#
#   # main.py
#   from contextlib import asynccontextmanager
#   from app.browser_owner import BrowserOwner
#
#   @asynccontextmanager
#   async def lifespan(app):
#       app.state.browser_owner = BrowserOwner()
#       yield
#       app.state.browser_owner.shutdown()
#
#   # single capture endpoint (async)
#   shot, meta = await request.app.state.browser_owner.acall(
#       capture_example, req.url, req.width, req.height, req.full_page, "png")
#
#   # batch worker (any thread)
#   shot, meta = browser_owner.call(
#       capture_example, item.url, job.width, job.height, job.full_page, "png",
#       timeout=120)
#
# Remove every other sync_playwright() / chromium.launch() call in the
# codebase — the owner must be the only thing that touches Playwright.
# ─────────────────────────────────────────────────────────────────────────────