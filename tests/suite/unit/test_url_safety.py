# tests/suite/unit/test_url_safety.py
# =============================================================================
# Release I (F-03): the SSRF guard itself. DNS is faked by conftest (FAKE_DNS).
# =============================================================================
from pathlib import Path

import pytest

import url_safety
from url_safety import UnsafeURL, assert_public_url, first_unsafe, assert_public_final_url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:10000/docs", "http://localhost/", "http://LOCALHOST./",
    "http://api.localhost/", "http://printer.local/", "http://db.internal/",
    "http://10.0.0.5/", "http://172.16.0.1/", "http://172.31.255.255/", "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/", "http://100.64.0.1/", "http://0.0.0.0/",
    "http://[::1]/", "http://[fd00::1]/", "http://[fe80::1]/", "http://[::ffff:127.0.0.1]/",
    "http://224.0.0.1/",
    "http://2130706433/",          # 127.0.0.1 written as one number
    "http://0x7f000001/",          # ... and in hex
    "http://internal.attacker.test/", "http://metadata.attacker.test/",
    "http://v6-loopback.attacker.test/", "http://mapped.attacker.test/",
    "http://cgnat.attacker.test/",
    "http://rebind.attacker.test/",  # one public and one private answer: refused
])
def test_private_and_internal_targets_are_refused(url, monkeypatch):
    # Integer / hex host forms are resolved by the OS resolver, as a browser
    # would; model that here.
    real = url_safety._resolve
    monkeypatch.setattr(url_safety, "_resolve",
                        lambda h: ["127.0.0.1"] if h in ("2130706433", "0x7f000001") else real(h))
    with pytest.raises(UnsafeURL):
        assert_public_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/", "gopher://x/",
                                 "javascript:alert(1)", "http:///nohost"])
def test_non_http_or_hostless_urls_are_refused(url):
    with pytest.raises(UnsafeURL):
        assert_public_url(url)


@pytest.mark.parametrize("url", ["https://example.com/", "http://93.184.216.34/",
                                 "https://www.pixelperfectapi.net/pricing?x=1",
                                 "http://[2606:2800:220:1:248:1893:25c8:1946]/"])
def test_public_targets_pass(url):
    assert_public_url(url)


def test_dns_failure_gives_a_clear_message(monkeypatch):
    import socket

    def fail(host):
        raise socket.gaierror("Name or service not known")
    monkeypatch.setattr(url_safety, "_resolve", fail)
    with pytest.raises(UnsafeURL) as e:
        assert_public_url("https://no-such-site.example/")
    assert "couldn't find" in str(e.value)


def test_first_unsafe_returns_the_offender():
    assert first_unsafe(["https://a.example.com/", "http://10.0.0.1/", "https://b.example.com/"]) == "http://10.0.0.1/"
    assert first_unsafe(["https://a.example.com/"]) is None


def test_final_url_check_allows_blank_pages_and_refuses_private_redirects():
    assert_public_final_url("about:blank")
    assert_public_final_url("data:text/html,hi")
    assert_public_final_url("https://example.com/landed")
    with pytest.raises(UnsafeURL):
        assert_public_final_url("http://internal.attacker.test/admin")


def test_allow_flag_turns_the_guard_off(monkeypatch):
    monkeypatch.setenv("ALLOW_PRIVATE_CAPTURE_TARGETS", "1")
    assert_public_url("http://127.0.0.1:10000/docs")
    assert url_safety.block_private_requests(object()) is False


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "https://127.0.0.1:8443/x", "ws://127.0.0.1:9000/s", "wss://localhost/s",
    "http://localhost:10000/health", "http://LOCALHOST/", "http://localhost./", "http://api.localhost/",
    "http://printer.local/", "http://db.internal:5432/", "http://10.0.0.5/", "http://10.255.255.255",
    "http://172.16.0.1/", "http://172.31.0.1/", "http://192.168.1.1/?q=1", "http://169.254.169.254/latest",
    "http://100.64.0.1/", "http://100.127.255.255/", "http://0.0.0.0/", "http://[::1]/", "http://[::]/",
    "http://[::ffff:127.0.0.1]/", "http://[fd00::1]:8080/", "http://[fe80::1]/", "http://user@127.0.0.1/",
])
def test_browser_pattern_blocks_private_targets(url):
    assert url_safety.PRIVATE_URL_PATTERN.match(url), url


@pytest.mark.parametrize("url", [
    "https://example.com/", "https://10.example.com/", "https://127.example.net/", "http://172.15.0.1/",
    "http://172.32.0.1/", "http://100.63.0.1/", "http://100.128.0.1/", "https://localhost.example.com/",
    "https://cdn.example.com/img?ip=127.0.0.1", "http://[2606:2800:220:1:248:1893:25c8:1946]/",
    "https://example.com/redirect?to=http://127.0.0.1/", "http://93.184.216.34/",
])
def test_browser_pattern_leaves_public_targets_alone(url):
    assert not url_safety.PRIVATE_URL_PATTERN.match(url), url


def test_route_registration_never_raises():
    class Page:
        def route(self, glob, handler):
            raise RuntimeError("page closed")
    assert url_safety.block_private_requests(Page()) is False


def test_registered_route_aborts_matching_requests():
    calls = []

    class Route:
        class request:
            url = "http://127.0.0.1:10000/docs"

        def abort(self, reason):
            calls.append(reason)

    class Page:
        def route(self, pattern, handler):
            calls.append(pattern)
            handler(Route())

    assert url_safety.block_private_requests(Page()) is True
    assert calls == [url_safety.PRIVATE_URL_PATTERN, "blockedbyclient"]


# ------------------------------------------------------ engine wiring guard
# The real-browser tests prove the engines block private requests, but they
# only run with --browser. These checks run every time.
BACKEND = Path(__file__).resolve().parents[3]


def _live(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return text.split("# ===== END OF", 1)[0] if "# ===== END OF" in text else text


@pytest.mark.parametrize("engine", ["services/screenshot_service.py", "screenshot_service.py"])
def test_both_engines_register_the_guard_after_the_tracker_routes(engine):
    code = "\n".join(l for l in _live(BACKEND / engine).splitlines() if not l.lstrip().startswith("#"))
    guard = code.find("block_private_requests(page)")
    assert guard > 0, "engine no longer registers the private-address route"
    last_tracker_route = code.rfind("page.route(", 0, guard)
    assert last_tracker_route >= 0 and code.find("page.route(", guard) < 0, \
        "the private-address route must be the last route registered"
    final = code.find("assert_public_final_url(page.url)")
    assert final > guard, "engine no longer checks the final URL after navigation"
