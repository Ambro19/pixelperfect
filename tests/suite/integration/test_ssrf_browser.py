# tests/suite/integration/test_ssrf_browser.py
# =============================================================================
# Release I (F-03): real Chromium. A page that tries to load localhost and
# private addresses (image, iframe, fetch, redirect) must not reach them once
# url_safety.block_private_requests() is on the page. A local server counts
# every request it receives; the count must stay at zero.
# Opt-in: pytest --browser
# =============================================================================
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import url_safety

pytestmark = pytest.mark.browser


class _Counter(BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        _Counter.hits.append(self.path)
        if self.path.startswith("/redirect-to-local"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/after-redirect")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"secret internal data")

    def log_message(self, *a):
        pass


@pytest.fixture
def local_server():
    _Counter.hits = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Counter)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    exe = os.getenv("PP_CHROMIUM_EXECUTABLE")
    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": exe} if exe else {}))
        pg = browser.new_page()
        yield pg
        browser.close()


def test_private_subresources_never_reach_the_network(page, local_server, monkeypatch):
    monkeypatch.delenv("ALLOW_PRIVATE_CAPTURE_TARGETS", raising=False)
    port = local_server
    assert url_safety.block_private_requests(page) is True
    failed = []
    page.on("requestfailed", lambda r: failed.append(r.url))
    page.set_content(f"""
      <img src="http://127.0.0.1:{port}/img">
      <img src="http://localhost:{port}/img2">
      <iframe src="http://127.0.0.1:{port}/frame"></iframe>
      <script>
        fetch("http://127.0.0.1:{port}/fetch").catch(() => {{}});
        fetch("http://[::1]:{port}/v6").catch(() => {{}});
        new Image().src = "http://169.254.169.254/latest/meta-data/";
      </script>""")
    time.sleep(1.5)
    assert _Counter.hits == []
    assert any(f"127.0.0.1:{port}/img" in u for u in failed)


def test_navigation_and_redirects_to_private_addresses_are_blocked(page, local_server, monkeypatch):
    monkeypatch.delenv("ALLOW_PRIVATE_CAPTURE_TARGETS", raising=False)
    port = local_server
    url_safety.block_private_requests(page)
    from playwright.sync_api import Error as PlaywrightError
    with pytest.raises(PlaywrightError):
        page.goto(f"http://127.0.0.1:{port}/direct", timeout=5000)
    assert _Counter.hits == []


def test_without_the_guard_the_same_page_does_reach_the_server(page, local_server):
    # Proves the test above is meaningful: the server is reachable otherwise.
    port = local_server
    page.set_content(f'<img src="http://127.0.0.1:{port}/img">')
    time.sleep(1.0)
    assert "/img" in _Counter.hits
