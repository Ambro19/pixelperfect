#!/usr/bin/env python
# tests/manual/repro_f14_capture_hang.py
# =============================================================================
# Reproduces finding F-14 with real Chromium: page.evaluate() calls in the
# capture pipeline have no timeout (_JS_TIMEOUT_MS is defined but never used),
# so a hostile page or a never-resolving custom_js freezes the capture worker
# thread. Every later capture queued on that thread waits forever too.
#
# Not part of the pytest suite on purpose: a stuck worker thread cannot be
# freed from Python, so it would also block pytest from exiting.
#
# Usage (from the backend folder):
#     python tests/manual/repro_f14_capture_hang.py
# Optional: set PP_CHROMIUM_EXECUTABLE if Playwright cannot find Chromium.
#
# Before the capture-engine release: "STILL RUNNING after 30s".
# Since the fix (Oct 2026): every case finishes or fails cleanly within
# seconds. The automated equivalents are in tests/suite/integration
# (run with --browser); this script remains as a quick manual check.
# =============================================================================
import asyncio
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("SECRET_KEY", "repro-only")

PAGES = {
    # Breaks the lazy-load scroll helper, which waits on setInterval.
    "/interval": b"<html><body style='height:3000px'>"
                 b"<script>window.setInterval=function(){return 0}</script>hi</body></html>",
    "/plain": b"<html><body>ok</body></html>",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.split("?")[0], b"x")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    exe = os.getenv("PP_CHROMIUM_EXECUTABLE")
    if exe:
        from playwright.sync_api._generated import BrowserType
        real = BrowserType.launch

        def launch(self, *a, **kw):
            kw.setdefault("executable_path", exe)
            return real(self, *a, **kw)

        BrowserType.launch = launch

    import services.screenshot_service as hot

    async def run():
        svc = hot.ScreenshotService()
        await svc.initialize()
        cases = [
            ("Free: full_page on a hostile page", dict(url=base + "/interval", full_page=True)),
            ("Pro: custom_js that never resolves", dict(url=base + "/plain",
                                                        custom_js="() => new Promise(() => {})")),
            ("Next ordinary capture", dict(url=base + "/plain")),
        ]
        for name, kw in cases:
            t0 = time.time()
            try:
                await asyncio.wait_for(svc.capture_screenshot(width=800, height=600, **kw), 30)
                print(f"{name}: finished in {time.time() - t0:.1f}s", flush=True)
            except asyncio.TimeoutError:
                print(f"{name}: STILL RUNNING after 30s (worker thread stuck)", flush=True)
            except Exception as e:
                print(f"{name}: failed cleanly in {time.time() - t0:.1f}s: {e}", flush=True)

    asyncio.run(run())
    os._exit(0)   # the stuck worker thread would otherwise keep the process alive


if __name__ == "__main__":
    main()
