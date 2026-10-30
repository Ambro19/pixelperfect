# tests/suite/integration/test_real_browser.py
# =============================================================================
# Real Chromium, real Playwright, both screenshot_service modules.
#
# Opt-in:  pytest --browser
# Needs:   python -m playwright install chromium   (once per machine)
#
# Pages are served from a local HTTP server on 127.0.0.1, so no internet is
# needed. If your Chromium lives somewhere Playwright does not look, set
# PP_CHROMIUM_EXECUTABLE to its full path.
# =============================================================================
import asyncio
import io
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.browser

PAGE = b"""<!doctype html><html><head><style>
  html,body{margin:0;background:#ff0000}
  #banner{position:fixed;top:0;left:0;width:100%;height:100px;background:#0000ff}
  .tall{height:3000px}
  @media (prefers-color-scheme: dark){ body{background:#00ff00} }
</style></head><body><div id="banner"></div><div class="tall"></div></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/slow"):
            # Holds each capture open long enough that several overlap.
            import time
            time.sleep(0.5)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


@pytest.fixture(scope="module", autouse=True)
def _chromium_path():
    exe = os.getenv("PP_CHROMIUM_EXECUTABLE")
    if not exe:
        yield
        return
    from playwright.sync_api._generated import BrowserType
    real = BrowserType.launch

    def launch(self, *a, **kw):
        kw.setdefault("executable_path", exe)
        return real(self, *a, **kw)

    BrowserType.launch = launch
    yield
    BrowserType.launch = real


def _services():
    import screenshot_service as legacy
    import services.screenshot_service as hot
    return {"services (single captures)": hot, "root (batch)": legacy}


# KNOWN DEFECT (see tests/suite/known_defects and TESTING.md, finding F-01):
# services/screenshot_service.py runs Playwright's sync API on a 3-thread
# pool. The browser belongs to one thread; any capture handed to another
# thread fails with "Cannot switch to a different thread". Sequential tests
# pass or fail by luck, so they are marked non-strict on that module; the
# overlap test below is strict. Delete these markers once _executor has
# max_workers=1 (all 18 pass with that one-line change).
_F01 = "F-01: services/screenshot_service.py uses a 3-thread executor"
HOT = "services (single captures)"
LEGACY = "root (batch)"


@pytest.fixture(params=[
    pytest.param(HOT, marks=pytest.mark.xfail(reason=_F01, strict=False)),
    LEGACY,
])
def svc_module(request):
    return _services()[request.param]


@pytest.fixture(scope="module", autouse=True)
def _chromium_available(_chromium_path):
    """Skip the module only if Chromium itself cannot start. Probed in a
    fresh thread so it shares no state with the services under test."""
    box = {}

    def probe():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                pw.chromium.launch().close()
        except Exception as e:     # pragma: no cover - environment specific
            box["err"] = e

    t = threading.Thread(target=probe)
    t.start()
    t.join()
    if "err" in box:
        pytest.skip(f"Chromium not available: {box['err']}")


@pytest.fixture
async def svc(svc_module, tmp_path, monkeypatch):
    # Chromium is known to work (probe above), so an initialize() failure
    # here is a real failure of the module under test, not a skip.
    monkeypatch.setattr(svc_module, "SCREENSHOTS_DIR", tmp_path)
    s = svc_module.ScreenshotService()
    await s.initialize()
    yield s
    await s.close()


def _pixel(path, xy):
    from PIL import Image
    with Image.open(path) as im:
        return im.convert("RGB").getpixel(xy)


async def test_basic_capture_is_a_real_png_at_requested_size(svc, site):
    from PIL import Image
    r = await svc.capture_screenshot(url=site, width=800, height=600)
    with Image.open(r["filepath"]) as im:
        assert im.format == "PNG"
        assert im.size == (800, 600)
    assert _pixel(r["filepath"], (400, 50)) == (0, 0, 255)     # banner
    assert _pixel(r["filepath"], (400, 400)) == (255, 0, 0)    # body


async def test_remove_elements_hides_selector(svc, site):
    r = await svc.capture_screenshot(url=site, width=800, height=600, remove_elements=["#banner"])
    assert _pixel(r["filepath"], (400, 50)) == (255, 0, 0)


async def test_dark_mode_sets_color_scheme(svc, site):
    r = await svc.capture_screenshot(url=site, width=800, height=600, dark_mode=True)
    assert _pixel(r["filepath"], (400, 400)) == (0, 255, 0)


async def test_full_page_is_taller_than_viewport(svc, site):
    from PIL import Image
    r = await svc.capture_screenshot(url=site, width=800, height=600, full_page=True)
    with Image.open(r["filepath"]) as im:
        assert im.size[1] >= 3000


@pytest.mark.parametrize("fmt, magic", [
    ("jpeg", b"\xff\xd8\xff"), ("webp", b"RIFF"), ("pdf", b"%PDF"),
])
async def test_formats(svc, site, fmt, magic):
    r = await svc.capture_screenshot(url=site, width=800, height=600, format=fmt)
    with open(r["filepath"], "rb") as f:
        assert f.read(4).startswith(magic)


async def test_unknown_device_is_value_error(svc, site):
    with pytest.raises(ValueError):
        await svc.capture_screenshot(url=site, device="nokia_3310")


@pytest.mark.parametrize("svc_module", [
    pytest.param(HOT, marks=pytest.mark.xfail(reason=_F01, strict=True)),
    LEGACY,
], indirect=True)
async def test_concurrent_captures_all_succeed(svc, site):
    # Several captures in flight at once: what the per-user concurrency
    # limit allows (2-5) and what several users at once produce. The /slow
    # page holds each capture open so they genuinely overlap.
    results = await asyncio.gather(
        *[svc.capture_screenshot(url=site + "slow", width=640, height=480) for _ in range(6)],
        return_exceptions=True,
    )
    errors = [repr(r) for r in results if isinstance(r, BaseException)]
    assert errors == [], errors
