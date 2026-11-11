# tests/suite/unit/test_capture_guard.py
# =============================================================================
# The capture-engine safety limits (services/capture_guard.py) and the F-01
# single-thread rule. No browser needed; real-Chromium behaviour is covered by
# integration/test_real_browser.py (run with --browser).
# =============================================================================
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from services import capture_guard as cg


# ------------------------------------------------------------------ F-01
def test_both_capture_modules_run_playwright_on_one_thread():
    # Playwright's sync API is thread-affine. More than one worker thread per
    # module means captures land on a thread that does not own the browser.
    import screenshot_service as legacy
    import services.screenshot_service as hot

    for mod in (legacy, hot):
        pools = [v for v in vars(mod).values() if isinstance(v, ThreadPoolExecutor)]
        assert pools, f"{mod.__name__}: no module-level executor found - update this test"
        assert all(ex._max_workers == 1 for ex in pools), mod.__name__


def test_both_modules_use_the_shared_guard():
    import screenshot_service as legacy
    import services.screenshot_service as hot
    for mod in (legacy, hot):
        assert mod.CUSTOM_JS_RUNNER is cg.CUSTOM_JS_RUNNER
        assert mod.PRISTINE_TIMERS_JS is cg.PRISTINE_TIMERS_JS


# --------------------------------------------------------- wait_with_limits
async def _run(job_seconds, hard, queue_ahead_seconds=0.0, monkeypatch=None):
    """Run one job on a 1-thread pool, optionally behind a slow job."""
    pool = ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()
    stuck = []
    if queue_ahead_seconds:
        pool.submit(time.sleep, queue_ahead_seconds)
    clock = cg.JobClock()
    fut = loop.run_in_executor(pool, cg.timed_job(clock, lambda: time.sleep(job_seconds) or "done"))
    try:
        return await cg.wait_with_limits(fut, clock, hard, stuck.append, "job"), stuck
    finally:
        pool.shutdown(wait=False)


async def test_fast_capture_returns_its_result():
    result, stuck = await _run(0.1, hard=2)
    assert result == "done" and stuck == []


async def test_running_too_long_triggers_restart_and_timeout():
    t0 = time.monotonic()
    with pytest.raises(cg.CaptureTimeout) as e:
        await _run(3, hard=0.5)
    assert time.monotonic() - t0 < 2.5
    assert cg.PAGE_TIMEOUT_MARKER in str(e.value)


async def test_time_spent_queued_does_not_count():
    # 2.5 s waiting behind another job, 0.2 s running, limit 1.5 s: no restart.
    # (Generous margins: this must hold on a slow, busy Windows machine.)
    result, stuck = await _run(0.2, hard=1.5, queue_ahead_seconds=2.5)
    assert result == "done" and stuck == []


async def test_queue_limit_gives_up_without_restarting(monkeypatch):
    monkeypatch.setattr(cg, "CAPTURE_QUEUE_TIMEOUT_S", 0.5)
    pool = ThreadPoolExecutor(max_workers=1)
    pool.submit(time.sleep, 1.5)
    loop = asyncio.get_running_loop()
    clock, ran, stuck = cg.JobClock(), [], []
    fut = loop.run_in_executor(pool, cg.timed_job(clock, lambda: ran.append(1)))
    with pytest.raises(cg.EngineBusy) as e:
        await cg.wait_with_limits(fut, clock, 10, stuck.append, "job")
    assert cg.ENGINE_BUSY_MARKER in str(e.value)
    assert stuck == []                       # a busy queue is not a stuck engine
    with pytest.raises(cg.CaptureAbandoned):
        await fut                            # the abandoned capture never runs
    assert ran == []
    pool.shutdown(wait=True)


# ------------------------------------------------------------------ Guarded
def test_guarded_calls_pass_through_until_restart():
    class Svc:
        _epoch = 0

    class Page:
        url = "https://example.com"

        def title(self):
            return "ok"

    svc = Svc()
    page = cg.Guarded(Page(), svc, svc._epoch)
    assert page.title() == "ok" and page.url == "https://example.com"
    svc._epoch += 1                          # a forced restart happened
    with pytest.raises(cg.CaptureAbandoned):
        page.title()


# --------------------------------------------- layer 2: per-evaluate budget
async def test_a_stuck_evaluate_is_caught_long_before_the_hard_limit(monkeypatch):
    monkeypatch.setattr(cg, "EVALUATE_BUDGET_S", 0.5)

    class Svc:
        _epoch = 0

    class Page:
        def evaluate(self, *a):
            time.sleep(3)          # a synchronous infinite loop, in effect

    svc = Svc()
    pool = ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()
    clock, stuck = cg.JobClock(), []
    fut = loop.run_in_executor(
        pool, cg.timed_job(clock, lambda: cg.Guarded(Page(), svc, 0).evaluate("x")))
    t0 = time.monotonic()
    with pytest.raises(cg.CaptureTimeout) as e:
        await cg.wait_with_limits(fut, clock, 60, stuck.append, "job")
    assert time.monotonic() - t0 < 2
    assert stuck and "evaluate" in stuck[0]
    assert "A script on the page" in str(e.value)
    pool.shutdown(wait=False)


async def test_other_page_calls_do_not_publish_a_deadline():
    class Svc:
        _epoch = 0

    seen = []

    class Page:
        def screenshot(self):
            seen.append(cg._local.clock.call_deadline)

    clock = cg.JobClock()
    cg.timed_job(clock, lambda: cg.Guarded(Page(), Svc(), 0).screenshot())()
    assert seen == [None]


def test_start_and_abandon_cannot_both_win():
    # The worker may only start a capture the waiter has not abandoned, and
    # the waiter may only abandon a capture that has not started.
    for _ in range(200):
        clock = cg.JobClock()
        ran = []
        job = cg.timed_job(clock, lambda: ran.append(1))
        def run():
            try:
                job()
            except cg.CaptureAbandoned:
                pass

        t = threading.Thread(target=run)
        t.start()
        with clock.lock:
            if clock.started_at is None:
                clock.abandoned = True
        t.join()
        assert (clock.started_at is not None) == bool(ran)
        assert not (clock.abandoned and ran)


# ------------------------------------------------------- restarts (no browser)
def _service(module):
    return module.ScreenshotService()


@pytest.mark.parametrize("modname", ["services.screenshot_service", "screenshot_service"])
async def test_a_failed_restart_is_retried_not_permanent(modname, monkeypatch):
    import importlib
    mod = importlib.import_module(modname)
    svc = _service(mod)
    svc._needs_restart = True
    attempts = []

    def flaky_init():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Chromium failed to launch (out of memory)")
        svc.browser, svc.playwright = object(), object()

    monkeypatch.setattr(svc, "_sync_initialize", flaky_init)
    with pytest.raises(RuntimeError):
        await svc.initialize()
    assert svc._init_error is None and svc._needs_restart
    await svc.initialize()                       # the next request recovers
    assert svc.is_ready() and not svc._needs_restart


@pytest.mark.parametrize("modname", ["services.screenshot_service", "screenshot_service"])
async def test_if_the_kill_fails_requests_fail_fast(modname):
    import importlib
    mod = importlib.import_module(modname)
    svc = _service(mod)
    svc.playwright = object()                    # kill_driver cannot reach a driver
    svc._force_restart("test")
    t0 = time.monotonic()
    with pytest.raises(cg.EngineBusy) as e:
        await svc.capture_screenshot(url="https://example.com")
    assert time.monotonic() - t0 < 1
    assert "server restart" in str(e.value)


@pytest.mark.parametrize("modname", ["services.screenshot_service", "screenshot_service"])
def test_restart_flag_is_set_before_the_epoch_moves(modname, monkeypatch):
    import importlib
    mod = importlib.import_module(modname)
    svc = _service(mod)
    seen = []
    monkeypatch.setattr(mod, "kill_driver", lambda pw: True)

    real = type(svc).__setattr__

    def spy(self, name, value):
        if name == "_epoch":
            seen.append(self._needs_restart)
        real(self, name, value)

    monkeypatch.setattr(type(svc), "__setattr__", spy)
    svc.playwright = object()
    svc._force_restart("test")
    assert seen == [True]


# ------------------------------------------------------------- page scripts
def test_bounded_js_wraps_the_helper_with_pristine_timer_and_race():
    js = cg.bounded_js("(x) => x + 1", 1234)
    assert "(x) => x + 1" in js and "1234" in js
    assert "T.race(" in js and "new T.Promise(" in js
    assert "Promise.race(" not in js.replace("Promise.race.bind", "")


def test_custom_js_is_passed_as_data_and_function_sources_are_called():
    assert "[src, ms]" in cg.CUSTOM_JS_RUNNER
    assert "(0, eval)(code)" in cg.CUSTOM_JS_RUNNER
    assert "function" in cg.CUSTOM_JS_RUNNER.split("const code")[1].split(";")[0]


def test_pristine_values_cannot_be_overwritten_by_the_page():
    assert "writable: false" in cg.PRISTINE_TIMERS_JS
    assert "configurable: false" in cg.PRISTINE_TIMERS_JS
    assert "race:" in cg.PRISTINE_TIMERS_JS and "Promise:" in cg.PRISTINE_TIMERS_JS


# ----------------------------------------------------- Playwright internals
def test_kill_driver_unblocks_a_real_driver():
    # kill_driver relies on private Playwright attributes. This starts a real
    # driver (no browser needed), kills it from another thread, and checks
    # that the next call on it fails at once instead of hanging. If a
    # Playwright upgrade breaks this, the upgrade cannot pass the suite.
    started, killed, box = threading.Event(), threading.Event(), {}

    def worker():
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        box["pw"] = pw
        started.set()
        killed.wait(30)
        t0 = time.monotonic()
        try:
            pw.request.new_context()             # a real protocol call
            box["result"] = "returned"
        except Exception as e:
            box["result"] = type(e).__name__
        box["seconds"] = time.monotonic() - t0
        cg.forget_dead_driver()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    assert started.wait(60)
    assert cg.kill_driver(box["pw"]) is True
    killed.set()
    t.join(15)
    assert not t.is_alive(), "the call on a killed driver hung"
    assert box["result"] != "returned"
    assert box["seconds"] < 5


def test_kill_driver_never_raises():
    assert cg.kill_driver(object()) is False
