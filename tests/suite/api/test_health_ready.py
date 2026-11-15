# tests/suite/api/test_health_ready.py
# =============================================================================
# Release G: /health/ready tells the uptime monitor whether a customer could
# take a screenshot right now, and /health no longer publishes internals.
# =============================================================================
import pytest

import main
import services.screenshot_service as api_module


@pytest.fixture
def engines(client, monkeypatch):
    """The two engines the app uses right now (the suite swaps in fakes),
    each starting clean for this test."""
    pair = (main.screenshot_service, api_module.screenshot_service)
    for svc in pair:
        monkeypatch.setattr(svc, "_wedged", False, raising=False)
        monkeypatch.setattr(svc, "_init_error", None, raising=False)
    return pair


def test_ready_when_database_and_engines_are_fine(client, engines):
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"
    assert r.json()["checks"] == {"database": "ok", "capture_engine": "ok"}
    assert r.headers["cache-control"] == "no-store"
    assert client.head("/health/ready").status_code == 200


def test_engine_waiting_to_restart_still_counts_as_ready(client, engines, monkeypatch):
    # After a forced kill the engine restarts on the next capture: not an outage.
    monkeypatch.setattr(engines[1], "_needs_restart", True, raising=False)
    assert client.get("/health/ready").status_code == 200


@pytest.mark.parametrize("which", [0, 1])
def test_a_wedged_engine_makes_the_service_not_ready(client, engines, monkeypatch, which):
    monkeypatch.setattr(engines[which], "_wedged", True)
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"
    assert r.json()["checks"]["capture_engine"] == "failing"
    assert client.head("/health/ready").status_code == 503


def test_an_engine_that_cannot_start_makes_the_service_not_ready(client, engines, monkeypatch):
    secret_detail = "Executable doesn't exist at /ms-playwright/chromium-1234/chrome"
    monkeypatch.setattr(engines[1], "_init_error", secret_detail)
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert "/ms-playwright" not in r.text           # only ok/failing is published


def test_database_down_makes_the_service_not_ready(client, engines, monkeypatch):
    class DeadEngine:
        def connect(self):
            raise RuntimeError("could not connect to server: password=hunter2")
    monkeypatch.setattr(main, "engine", DeadEngine())
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["checks"]["database"] == "failing"
    assert "hunter2" not in r.text


def test_health_no_longer_publishes_error_text_or_paths(client, monkeypatch):
    monkeypatch.setattr(main, "SCREENSHOT_LAST_ERROR", "Executable doesn't exist at /ms-playwright/x")
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["screenshot_service_error"] is True
    assert "/ms-playwright" not in r.text
    assert "screenshots_dir" not in body and str(main.SCREENSHOTS_DIR) not in r.text
