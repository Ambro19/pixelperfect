# abuse_limits.py
# =============================================================================
# Release H (F-20): rate limits for the endpoints that send email to an
# address the caller chooses (forgot-password, forgot-username).
#
# Why: with no limit, anyone could make PixelPerfect send hundreds of emails
# to a stranger's inbox, burn through the email provider's daily quota
# (Resend's free plan: 100/day, shared with Brief!), and get the sending
# domain flagged as spam.
#
# The contact form only mails OneTechly, but every message still uses the
# shared email quota, so it gets its own per-IP limit.
#
# Two limits on the account-email endpoints:
#   - Per client IP: too many requests -> HTTP 429 with Retry-After.
#   - Per email address: after a few emails in an hour, further requests still
#     get the normal "ok" answer but no email is sent. The answer must not
#     change, or the endpoint would reveal which addresses have accounts.
#
# The state lives in memory: PixelPerfect runs as one process on Render, and a
# restart simply starts the windows over, which is acceptable for this.
# =============================================================================
import logging
import os
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

from fastapi import Request

log = logging.getLogger("pixelperfect")
_warned_no_cf_header = False

# Limits (kept small on purpose: these are rare, human-initiated actions)
IP_LIMIT, IP_WINDOW_S = 10, 15 * 60          # 10 requests per 15 min per IP
EMAIL_LIMIT, EMAIL_WINDOW_S = 3, 60 * 60     # 3 emails per hour per address and kind
CONTACT_LIMIT, CONTACT_WINDOW_S = 5, 60 * 60 # 5 contact-form messages per hour per IP


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_s: float, clock=time.monotonic):
        self.limit, self.window_s, self._clock = limit, window_s, clock
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> Tuple[bool, int]:
        """Record one attempt. Returns (allowed, seconds_until_next_allowed)."""
        now = self._clock()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False, max(1, int(self.window_s - (now - q[0])) + 1)
            q.append(now)
            return True, 0

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


ip_limiter = SlidingWindowLimiter(IP_LIMIT, IP_WINDOW_S)
email_limiter = SlidingWindowLimiter(EMAIL_LIMIT, EMAIL_WINDOW_S)
contact_limiter = SlidingWindowLimiter(CONTACT_LIMIT, CONTACT_WINDOW_S)


def client_ip(request: Optional[Request]) -> str:
    """The caller's IP.

    Render's edge runs on Cloudflare, which sets CF-Connecting-IP (and
    True-Client-IP) to the real client address, overwriting anything the
    client sent. X-Forwarded-For is NOT used: Render keeps whatever the client
    put there and appends a Cloudflare edge address, so neither end of it can
    be trusted. Without those headers (local runs, tests) the socket address
    is used."""
    if request is None:
        return "unknown"
    for header in ("cf-connecting-ip", "true-client-ip"):
        value = request.headers.get(header, "").strip()
        if value:
            return value
    global _warned_no_cf_header
    if not _warned_no_cf_header and os.getenv("ENVIRONMENT", "").lower() == "production":
        # If this appears in the Render log, every visitor would share the
        # proxy's address and the per-IP limit would apply to all of them.
        _warned_no_cf_header = True
        log.warning("Rate limits: no CF-Connecting-IP header on requests; "
                    "per-IP limits are keyed on the proxy address")
    return request.client.host if request.client else "unknown"


def reset_all() -> None:
    ip_limiter.clear()
    email_limiter.clear()
    contact_limiter.clear()

# ===== END OF abuse_limits.py ================================================
