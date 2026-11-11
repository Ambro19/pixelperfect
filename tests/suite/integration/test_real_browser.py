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


# A page that sabotages the timers our lazy-load scroll relies on.
HOSTILE = (b"<!doctype html><html><body style='margin:0;height:3000px;background:#fff'>"
           b"<script>window.setInterval=function(){return 0};"
           b"window.setTimeout=function(){return 0}</script>hi</body></html>")
# Pages that sabotage what our in-page time limit relies on.
RACE_SABOTAGE = (b"<!doctype html><html><body style='margin:0;height:3000px;background:#fff'>"
                 b"<script>Promise.race=()=>new Promise(()=>{});"
                 b"window.Promise=function(){};</script>race</body></html>")
THEN_SABOTAGE = (b"<!doctype html><html><body style='margin:0;height:3000px;background:#fff'>"
                 b"<script>Promise.prototype.then=function(){return this};</script>then</body></html>")
# A page that freezes itself with a synchronous loop shortly after loading.
SELF_LOOP = (b"<!doctype html><html><body style='margin:0;height:3000px;background:#fff'>"
             b"<script>setTimeout(()=>{while(true){}}, 300)</script>loop</body></html>")
# A page whose Content-Security-Policy forbids eval.
CSP_PAGE = b"<!doctype html><html><body style='margin:0;background:#ff0000'>csp</body></html>"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/hostile"):
            body, headers = HOSTILE, {}
        elif self.path.startswith("/race"):
            body, headers = RACE_SABOTAGE, {}
        elif self.path.startswith("/then"):
            body, headers = THEN_SABOTAGE, {}
        elif self.path.startswith("/selfloop"):
            body, headers = SELF_LOOP, {}
        elif self.path.startswith("/csp"):
            body, headers = CSP_PAGE, {"Content-Security-Policy": "script-src 'none'"}
        else:
            body, headers = PAGE, {}
        if self.path.startswith("/slow"):
            # Holds each capture open long enough that several overlap.
            import time
            time.sleep(0.5)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

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


# Both modules are tested: services/ serves single captures, root serves batch.
HOT = "services (single captures)"
LEGACY = "root (batch)"


@pytest.fixture(params=[HOT, LEGACY])
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


# ---------------------------------------------------------------------------
# F-14: a page or custom_js must never be able to freeze the capture engine.
# ---------------------------------------------------------------------------
import time as _time


async def test_hostile_page_cannot_stall_full_page_capture(svc, site):
    t0 = _time.monotonic()
    r = await svc.capture_screenshot(url=site + "hostile", width=800, height=600, full_page=True)
    assert r["filepath"]
    assert _time.monotonic() - t0 < 40          # used to wait forever


async def test_never_resolving_custom_js_is_skipped_with_a_warning(svc, site):
    t0 = _time.monotonic()
    r = await svc.capture_screenshot(url=site, width=800, height=600,
                                     custom_js="() => new Promise(() => {})")
    assert "did not finish" in (r.get("js_warning") or "")
    assert _time.monotonic() - t0 < 30


@pytest.mark.parametrize("js", [
    "document.body.style.background = 'rgb(0, 0, 255)';",                    # statements
    "() => { document.body.style.background = 'rgb(0, 0, 255)'; }",          # arrow
    "async () => { document.body.style.background = 'rgb(0, 0, 255)'; }",    # async arrow
    "function () { document.body.style.background = 'rgb(0, 0, 255)'; }",    # anonymous
    "async function () { document.body.style.background = 'rgb(0, 0, 255)'; }",
    "function paint() { document.body.style.background = 'rgb(0, 0, 255)'; }",  # named
    "  /* comment */ document.body.style.background = 'rgb(0, 0, 255)'",     # comment first
])
async def test_custom_js_forms_still_run(svc, site, js):
    r = await svc.capture_screenshot(url=site, width=800, height=600, custom_js=js,
                                     remove_elements=["#banner"])
    assert r.get("js_warning") is None
    assert _pixel(r["filepath"], (400, 400)) == (0, 0, 255)


async def test_custom_js_syntax_error_is_a_warning_not_a_failure(svc, site):
    r = await svc.capture_screenshot(url=site, width=800, height=600,
                                     custom_js="this is not valid javascript !!@#$")
    assert r["filepath"] and "SyntaxError" in (r.get("js_warning") or "")


async def test_custom_js_runs_on_a_page_whose_csp_forbids_eval(svc, site):
    r = await svc.capture_screenshot(url=site + "csp", width=800, height=600,
                                     custom_js="document.body.style.background = 'rgb(0, 0, 255)';")
    assert r.get("js_warning") is None
    assert _pixel(r["filepath"], (400, 300)) == (0, 0, 255)


async def test_infinite_loop_is_stopped_and_engine_recovers(svc, svc_module, site, monkeypatch):
    # A synchronous infinite loop cannot be interrupted from JavaScript. The
    # hard limit stops it, the capture queued behind it still succeeds, and the
    # engine keeps working afterwards.
    import asyncio
    from services.capture_guard import CaptureTimeout
    monkeypatch.setattr(svc_module, "CAPTURE_HARD_TIMEOUT_S", 8)
    t0 = _time.monotonic()
    stuck = asyncio.create_task(svc.capture_screenshot(
        url=site, width=800, height=600, custom_js="() => { while (true) {} }"))
    await asyncio.sleep(1)
    queued = asyncio.create_task(svc.capture_screenshot(url=site, width=800, height=600))
    with pytest.raises(CaptureTimeout):
        await stuck
    assert _time.monotonic() - t0 < 20
    r = await queued
    assert _pixel(r["filepath"], (400, 400)) == (255, 0, 0)
    r = await svc.capture_screenshot(url=site, width=800, height=600)
    assert _pixel(r["filepath"], (400, 400)) == (255, 0, 0)


async def test_sabotaged_promise_race_cannot_stall_the_capture(svc, site):
    # Our in-page limit uses the Promise functions captured before the page ran.
    t0 = _time.monotonic()
    r = await svc.capture_screenshot(url=site + "race", width=800, height=600, full_page=True)
    assert r["filepath"] and _time.monotonic() - t0 < 40


async def test_page_that_defeats_in_page_limits_is_stopped_within_the_script_budget(
        svc, site, monkeypatch):
    # Promise.prototype.then tampering defeats in-page racing; layer 2 (the
    # per-evaluate budget) must stop it within seconds, not the 180 s limit,
    # and the engine must keep working.
    from services import capture_guard as cg
    monkeypatch.setattr(cg, "EVALUATE_BUDGET_S", 6)
    t0 = _time.monotonic()
    try:
        await svc.capture_screenshot(url=site + "then", width=800, height=600, full_page=True)
    except cg.CaptureTimeout:
        pass                                      # stopped: fine
    assert _time.monotonic() - t0 < 30
    r = await svc.capture_screenshot(url=site, width=800, height=600)
    assert _pixel(r["filepath"], (400, 400)) == (255, 0, 0)


async def test_page_that_freezes_itself_is_stopped_and_engine_recovers(svc, site, monkeypatch):
    # No custom_js: the page's own script loops forever (what any free user
    # could submit, including through batch).
    from services import capture_guard as cg
    monkeypatch.setattr(cg, "EVALUATE_BUDGET_S", 6)
    t0 = _time.monotonic()
    with pytest.raises(Exception):
        await svc.capture_screenshot(url=site + "selfloop", width=800, height=600, full_page=True)
    assert _time.monotonic() - t0 < 60
    r = await svc.capture_screenshot(url=site, width=800, height=600)
    assert _pixel(r["filepath"], (400, 400)) == (255, 0, 0)
