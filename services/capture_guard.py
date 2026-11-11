# backend/services/capture_guard.py
# =============================================================================
# Capture-engine safety limits, shared by BOTH screenshot_service modules
# (services/screenshot_service.py for single captures, screenshot_service.py
# for batch). Kept in one place so the two modules cannot drift apart.
#
# ✅ NEW (Oct 2026 — findings F-01 and F-14)
#
# The problem (F-14): page.evaluate() has no timeout in Playwright. A page
# that never lets our helper script finish (for example by replacing
# window.setInterval, which the lazy-load scroll relies on), a custom_js that
# returns a promise that never resolves, or a synchronous infinite loop left
# the capture worker thread waiting forever. With one worker thread per module
# (the F-01 fix), that means EVERY later capture waits forever too.
#
# Three layers of defence:
#
#   1. In the page (fast path): every script we run is raced against a timer
#      captured by an init script BEFORE any page script runs. Handles the
#      ordinary cases (sabotaged timers, never-settling promises) in seconds,
#      and the capture carries on.
#
#   2. Per script call (EVALUATE_BUDGET_S): a page can still defeat layer 1
#      (a synchronous infinite loop, or tampering with Promise internals).
#      Every page.evaluate() made through Guarded publishes a deadline; if the
#      call is still running past it, the watcher restarts the engine within
#      seconds instead of waiting for the whole-capture limit.
#
#   3. Per capture (CAPTURE_HARD_TIMEOUT_S): a backstop for anything else.
#
# Restarting = mark the Playwright connection closed, kill this module's
# driver process (Chromium exits with it), let the abandoned capture stop at
# its next call (Guarded), and start a fresh engine on the worker thread.
# =============================================================================
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("pixelperfect")

# Upper bound for one whole capture, in seconds, counting only the time it is
# RUNNING (never time spent queued). The worst legitimate case is about
# nav 25 + commit 15 + settle 8 + selector 10 + delay 10 + custom_js 10 +
# lazy scroll 7 + screenshot 60 = 145 s, so 180 s never cuts off a capture
# that would have succeeded.
CAPTURE_HARD_TIMEOUT_S = float(os.getenv("CAPTURE_HARD_TIMEOUT_S", "180"))

# How long a capture may wait in the queue before it STARTS. Generous: queue
# time is normal under load and must not be mistaken for a stuck capture.
CAPTURE_QUEUE_TIMEOUT_S = float(os.getenv("CAPTURE_QUEUE_TIMEOUT_S", "300"))

# In-page limit for our own helper scripts (element removal).
SCRIPT_TIMEOUT_MS = int(os.getenv("CAPTURE_SCRIPT_TIMEOUT_MS", "5000"))

# In-page limit for a customer's custom_js. Before Oct 2026 there was none.
CUSTOM_JS_TIMEOUT_MS = int(os.getenv("CUSTOM_JS_TIMEOUT_MS", "10000"))

# Longest any single page.evaluate() may run, in-page limits included, before
# the engine is restarted (layer 2). Larger than every in-page limit above
# and the lazy-scroll limit, so it only fires when layer 1 was defeated.
EVALUATE_BUDGET_S = float(os.getenv("CAPTURE_EVALUATE_BUDGET_S", "20"))

# Message markers used by routers/screenshot.py and batch.py.
#   restarted / busy -> infrastructure fault, HTTP 503, "please retry"
#   did not finish   -> the page itself would not render in time, HTTP 504
ENGINE_RESTARTED_MARKER = "capture engine was restarted"
ENGINE_BUSY_MARKER = "capture engine is busy"
PAGE_TIMEOUT_MARKER = "did not finish rendering"


class CaptureTimeout(RuntimeError):
    """The page kept a capture running past a limit; the capture was stopped."""

    def __init__(self, seconds: float, what: str = "The page"):
        super().__init__(
            f"{what} {PAGE_TIMEOUT_MARKER} within {seconds:.0f} seconds, so the capture "
            f"was stopped. The page may be blocking automated capture."
        )


class EngineBusy(RuntimeError):
    """A capture could not start in time (long queue, or a wedged engine)."""

    def __init__(self, seconds: float, wedged: bool = False):
        detail = ("it needs a server restart" if wedged
                  else f"this capture waited {seconds:.0f} seconds without starting")
        super().__init__(f"The {ENGINE_BUSY_MARKER}: {detail}. Please try again shortly.")


class CaptureAbandoned(RuntimeError):
    """Raised inside a capture that the time limit has already given up on."""


# -----------------------------------------------------------------------------
# Timing a capture
# -----------------------------------------------------------------------------
_local = threading.local()   # the JobClock of the capture running on this thread


class JobClock:
    """Shared between the waiting request and the worker thread for one capture."""

    __slots__ = ("started_at", "abandoned", "call_deadline", "call_name", "lock")

    def __init__(self) -> None:
        self.started_at: Optional[float] = None    # set when the capture starts running
        self.abandoned = False                     # set when the requester stops waiting
        self.call_deadline: Optional[float] = None # set while a page.evaluate() runs
        self.call_name = ""
        self.lock = threading.Lock()               # start-vs-abandon must be atomic


def timed_job(clock: JobClock, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap the worker-thread function so it records when it really starts,
    and skips captures whose requester already gave up while it was queued."""
    def job(*args: Any) -> Any:
        with clock.lock:
            if clock.abandoned:
                raise CaptureAbandoned("Capture skipped: the request stopped waiting.")
            clock.started_at = time.monotonic()
        _local.clock = clock
        try:
            return fn(*args)
        finally:
            _local.clock = None
    return job


def restart_clock() -> None:
    """Worker thread: restart the running capture's clock (used by the batch
    module when it retries a capture after Chromium crashed)."""
    clock = getattr(_local, "clock", None)
    if clock is not None:
        clock.started_at = time.monotonic()


async def wait_with_limits(future: "asyncio.Future", clock: JobClock,
                           hard_timeout_s: float, on_stuck: Callable[[str], None],
                           label: str) -> Any:
    """
    Wait for a capture running on the worker thread, enforcing layers 2 and 3.
    Time spent queued never counts, so a busy server never triggers a restart.
    """
    submitted = time.monotonic()
    while True:
        done, _ = await asyncio.wait({future}, timeout=0.25)
        if done:
            return future.result()
        now = time.monotonic()
        deadline = clock.call_deadline
        if deadline is not None and now > deadline:
            on_stuck(f"{label}: page.{clock.call_name}() still running after its budget")
            raise CaptureTimeout(EVALUATE_BUDGET_S, "A script on the page")
        with clock.lock:
            started = clock.started_at
            if started is None and now - submitted > CAPTURE_QUEUE_TIMEOUT_S:
                clock.abandoned = True
                raise EngineBusy(now - submitted)
        if started is not None and now - started > hard_timeout_s:
            on_stuck(f"{label} ran for more than {hard_timeout_s:.0f}s")
            raise CaptureTimeout(hard_timeout_s)


# -----------------------------------------------------------------------------
# Guarding Playwright objects
# -----------------------------------------------------------------------------
class Guarded:
    """
    Wraps a Playwright Browser, BrowserContext or Page for one capture.

    - After a forced restart the old driver is dead and ANY further call on
      its objects can wait forever. Every call first checks the service's
      restart counter and raises CaptureAbandoned instead.
    - page.evaluate() calls publish a deadline (layer 2) for the watcher.
    """

    __slots__ = ("_obj", "_svc", "_epoch")

    def __init__(self, obj: Any, svc: Any, epoch: int):
        self._obj, self._svc, self._epoch = obj, svc, epoch

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._obj, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            if self._svc._epoch != self._epoch:
                raise CaptureAbandoned(f"Capture abandoned: the {ENGINE_RESTARTED_MARKER}.")
            clock = getattr(_local, "clock", None)
            if name != "evaluate" or clock is None:
                return attr(*args, **kwargs)
            clock.call_name = name
            clock.call_deadline = time.monotonic() + EVALUATE_BUDGET_S
            try:
                return attr(*args, **kwargs)
            finally:
                clock.call_deadline = None

        return call


# -----------------------------------------------------------------------------
# In-page scripts
# -----------------------------------------------------------------------------
# Runs before any page script (context.add_init_script). Captures the real
# timer and Promise functions where the page cannot replace them.
PRISTINE_TIMERS_JS = """
(() => {
  try {
    if (window.__ppTimers) return;
    const t = Object.freeze({
      setTimeout:    window.setTimeout.bind(window),
      clearTimeout:  window.clearTimeout.bind(window),
      setInterval:   window.setInterval.bind(window),
      clearInterval: window.clearInterval.bind(window),
      Promise:       window.Promise,
      race:          window.Promise.race.bind(window.Promise),
    });
    Object.defineProperty(window, '__ppTimers',
      { value: t, writable: false, configurable: false, enumerable: false });
  } catch (e) { /* never break the page */ }
})();
"""

_T_FALLBACK = "{ setTimeout, clearTimeout, Promise, race: Promise.race.bind(Promise) }"


def bounded_js(fn_source: str, timeout_ms: int = SCRIPT_TIMEOUT_MS) -> str:
    """
    Wrap one of OUR trusted helper functions so it gives up after timeout_ms.

    fn_source must be a JavaScript function expression taking at most one
    argument, e.g. "(selectors) => {...}". Never pass customer input here: it
    is pasted into the script text. Customer code goes through
    CUSTOM_JS_RUNNER, where it is passed as data.
    """
    ms = int(timeout_ms)
    return f"""async (arg) => {{
  const T = window.__ppTimers || {_T_FALLBACK};
  const run = ({fn_source.strip()});
  let timer;
  const limit = new T.Promise((_, reject) => {{
    timer = T.setTimeout(
      () => reject(new Error('PP_EVAL_TIMEOUT: page script did not finish within {ms} ms')),
      {ms});
  }});
  const work = new T.Promise((resolve, reject) => {{
    try {{ resolve(run(arg)); }} catch (e) {{ reject(e); }}
  }});
  try {{
    return await T.race([work, limit]);
  }} finally {{
    T.clearTimeout(timer);
  }}
}}"""


# Runs a customer's custom_js with a time limit. The code arrives as DATA
# (the first argument), never pasted into this script.
#
# Semantics match Playwright's page.evaluate(string), which is what customers
# used before: the source runs as a global script (statements allowed); if
# the result is a function it is called, and a returned promise is awaited,
# now for at most `ms`. Like Playwright, a source starting with "function" or
# "async function" is treated as a function expression (without the
# parentheses, eval would read it as a declaration and never call it).
# Indirect eval needs the context's bypass_csp=True, which the capture code
# sets whenever custom_js is present.
CUSTOM_JS_RUNNER = """async ([src, ms]) => {
  const T = window.__ppTimers || %s;
  let timer;
  const limit = new T.Promise((_, reject) => {
    timer = T.setTimeout(
      () => reject(new Error('custom_js did not finish within ' + ms + ' ms and was skipped')),
      ms);
  });
  const trimmed = src.trim();
  const code = /^(async\\s+)?function\\b/.test(trimmed) ? '(' + trimmed + ')' : src;
  const work = new T.Promise((resolve, reject) => {
    try {
      let value = (0, eval)(code);
      if (typeof value === 'function') value = value();
      resolve(value);
    } catch (e) { reject(e); }
  });
  try {
    await T.race([work, limit]);
  } finally {
    T.clearTimeout(timer);
  }
  return null;
}""" % _T_FALLBACK


# -----------------------------------------------------------------------------
# Restarting
# -----------------------------------------------------------------------------
def forget_dead_driver() -> None:
    """
    Worker thread only. A sync Playwright instance marks its asyncio loop as
    "running" in the thread that uses it. After its driver was killed that
    loop never runs again, but the mark stays, and a new sync_playwright()
    refuses to start ("using Playwright Sync API inside the asyncio loop").
    Clear the mark. The dead objects themselves are simply dropped: calling
    anything on them (even close/stop) could hang. Each forced restart leaves
    the dead loop's file descriptors open (about 14); forced restarts are
    rare, and the process is replaced on every deploy.
    """
    asyncio._set_running_loop(None)


def _reap(pid: int) -> None:
    """Collect the killed driver's exit status so it does not linger as a
    zombie (asyncio's own watcher belonged to the dead loop). POSIX only."""
    if os.name != "posix":
        return

    def wait() -> None:
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
            pass   # already reaped by someone else

    threading.Thread(target=wait, name=f"reap-{pid}", daemon=True).start()


def kill_driver(playwright: Any) -> bool:
    """
    Kill the Playwright driver process behind a sync Playwright instance.

    Safe to call from any thread. Chromium exits when its driver goes away,
    and the Playwright call blocked in the worker thread raises, which frees
    that thread.

    Uses private Playwright attributes (pinned: playwright==1.49.1 in
    requirements.txt). If an upgrade moves them, this logs CRITICAL and
    returns False, and test_kill_driver_unblocks_a_real_driver fails, so the
    upgrade cannot ship unnoticed.
    """
    try:
        connection = playwright._impl_obj._connection
        proc = connection._transport._proc
        # Mark the connection closed FIRST, so every later call on it raises
        # at once instead of waiting for a reply from the dead driver. A plain
        # attribute assignment, so safe from another thread.
        from playwright._impl._errors import TargetClosedError
        connection._closed_error = TargetClosedError(
            f"Target page, context or browser has been closed: the {ENGINE_RESTARTED_MARKER}"
        )
        proc.kill()
        _reap(proc.pid)
        logger.error("🔪 Killed Playwright driver (pid %s) to free a stuck capture", proc.pid)
        return True
    except Exception:
        logger.critical(
            "🚨 Could not kill the Playwright driver. The capture engine stays stuck "
            "until the server restarts (redeploy or restart the Render service).",
            exc_info=True,
        )
        return False
