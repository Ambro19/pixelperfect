# url_safety.py
# =============================================================================
# Release I (F-03): stop PixelPerfect from being used to reach its own private
# network (SSRF).
#
# A screenshot API fetches whatever URL it is given. Without a check, anyone
# could ask it to capture http://127.0.0.1:10000/docs, the database host, a
# cloud metadata address (169.254.169.254), or any machine on Render's private
# network, and get back a picture of the response. The same is true for the
# Business-tier webhook_url, which the server POSTs to.
#
# Three layers:
#   1. assert_public_url(): before any capture or webhook, the hostname is
#      resolved and EVERY address it resolves to must be public. Only http and
#      https are accepted.
#   2. PRIVATE_URL_PATTERN: a route the capture engines register on each
#      page, so Chromium aborts requests to literal private addresses and
#      localhost, including redirects and sub-resources (images, iframes,
#      fetch, WebSockets). The pattern is matched inside Playwright's
#      driver, so ordinary requests are not slowed down (see the Sep 2026
#      tracker-route fix).
#   3. assert_public_final_url(): after navigation, the page's final address
#      is checked again, which catches a public URL that redirected to a
#      hostname pointing at a private address.
#
# Known limit (documented in TESTING.md): a public page that embeds a
# sub-resource from a hostname whose DNS points at a private address is not
# blocked by layers 2 or 3. Closing that needs an egress proxy.
#
# ALLOW_PRIVATE_CAPTURE_TARGETS=1 turns all of this off. It exists for local
# development and the real-browser tests (which serve pages on 127.0.0.1);
# it must never be set on Render.
# =============================================================================
import ipaddress
import logging
import os
import re
import socket
from typing import Iterable, List, Optional
from urllib.parse import urlsplit

log = logging.getLogger("pixelperfect")

BLOCKED_MESSAGE = (
    "This URL points to a private or internal network address, which "
    "PixelPerfect does not capture. Use a public website address."
)

_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan")


class UnsafeURL(ValueError):
    """The URL must not be fetched. str(e) is safe to show to the caller."""


def private_targets_allowed() -> bool:
    return (os.getenv("ALLOW_PRIVATE_CAPTURE_TARGETS") or "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve(host: str) -> List[str]:
    """All addresses the host resolves to. Tests replace this function."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


def _is_public(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text.split("%", 1)[0])   # drop IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _host_of(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise UnsafeURL("Only http:// and https:// URLs can be captured.")
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise UnsafeURL("The URL has no host name.")
    return host


def assert_public_host(host: str) -> None:
    if private_targets_allowed():
        return
    if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise UnsafeURL(BLOCKED_MESSAGE)
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            addresses = _resolve(host)
        except (socket.gaierror, UnicodeError, OSError):
            raise UnsafeURL(f"We couldn't find the website '{host}'. Check the address.") from None
        if not addresses:
            raise UnsafeURL(f"We couldn't find the website '{host}'. Check the address.")
    bad = [a for a in addresses if not _is_public(a)]
    if bad:
        log.warning("SSRF blocked: %s resolves to %s", host, ", ".join(bad))
        raise UnsafeURL(BLOCKED_MESSAGE)


def assert_public_url(url: str) -> None:
    """Raise UnsafeURL unless url is http(s) and its host is public."""
    assert_public_host(_host_of(str(url)))


def first_unsafe(urls: Iterable[str]) -> Optional[str]:
    """For batches: the first URL that fails, or None."""
    for u in urls:
        try:
            assert_public_url(u)
        except UnsafeURL:
            return u
    return None


def assert_public_final_url(url: str) -> None:
    """After navigation: about:blank and data: pages are fine; anything else
    must still be public (catches redirects to private hostnames)."""
    if not url or url.startswith(("about:", "data:", "blob:")):
        return
    assert_public_url(url)


# One regular expression rather than many globs: Playwright matches a route
# pattern inside its driver, so non-matching requests never wait on Python,
# and glob syntax cannot express IPv6 brackets safely (a "[" in a glob is
# passed to the driver's regex engine unescaped and crashes the driver).
# Chromium canonicalises numeric host forms (2130706433, 0x7f000001) to
# dotted quads before requesting, so those are covered too.
_V4_PRIVATE = (
    r"(?:127|10|0)\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|169\.254\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"
)
_V6_PRIVATE = r"\[(?:::1?|::ffff:[^\]]*|f[cd][0-9a-f]*:[^\]]*|fe[89ab][0-9a-f]*:[^\]]*)\]"
_NAMES = r"localhost|[^/?#:@]*\.(?:localhost|local|internal|lan|home\.arpa)"
PRIVATE_URL_PATTERN = re.compile(
    rf"^(?:https?|wss?)://(?:[^/?#@]*@)?(?:{_NAMES}|{_V4_PRIVATE}|{_V6_PRIVATE})\.?(?::\d+)?(?:[/?#]|$)",
    re.IGNORECASE,
)


def block_private_requests(page) -> bool:
    """Register the abort route for private targets on a Playwright page.
    Returns True when registered. Never raises."""
    if private_targets_allowed():
        return False

    def _abort(route):
        try:
            log.warning("SSRF blocked in browser: %s", route.request.url)
            return route.abort("blockedbyclient")
        except Exception:
            return None

    try:
        page.route(PRIVATE_URL_PATTERN, _abort)
        return True
    except Exception as e:
        log.warning("Private-address route unavailable: %s", e)
        return False

# ===== END OF url_safety.py ==================================================
