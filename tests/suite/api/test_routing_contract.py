# tests/suite/api/test_routing_contract.py
# =============================================================================
# The route table, startup, health, CORS and security headers.
#
# Several past bugs were routing bugs: a POST shadowed by a GET (405), a
# second unguarded POST that skipped the concurrency limit, a router
# registered before a wrapper so the wrapper never ran. These tests pin WHICH
# function serves each public path, so a re-ordered include_router shows up
# here rather than in production.
# =============================================================================
import pytest
from starlette.routing import Match

import main

EXPECTED = {
    # (method, path): module that must serve it
    ("POST", "/register"): "main",
    ("POST", "/token"): "main",
    ("POST", "/token_json"): "main",
    ("GET", "/users/me"): "main",
    ("POST", "/api/v1/screenshot"): "main",          # concurrency-guarded wrapper
    ("POST", "/api/v1/screenshot/"): "main",
    ("GET", "/api/v1/screenshot/"): "routers.screenshot",
    ("GET", "/api/v1/screenshot/devices"): "routers.screenshot",
    ("GET", "/api/v1/screenshot/abc"): "routers.screenshot",
    ("DELETE", "/api/v1/screenshot/abc"): "routers.screenshot",
    ("DELETE", "/api/v1/screenshots/abc"): "main",
    ("POST", "/api/v1/batch/submit"): "batch",       # router wins over main's wrapper
    ("POST", "/api/v1/batch/submit_file"): "batch",
    ("GET", "/api/v1/batch/jobs"): "batch",
    ("POST", "/webhook/stripe"): "main",
    ("GET", "/subscription_status"): "main",
    ("GET", "/api/v1/user/screenshot-history"): "history",
}


def _serving_module(method: str, path: str):
    scope = {"type": "http", "method": method, "path": path, "root_path": "",
             "query_string": b"", "headers": []}
    for route in main.app.router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return getattr(route.endpoint, "__module__", None)
    return None


@pytest.mark.parametrize("method, path", sorted(EXPECTED))
def test_route_is_served_by_expected_module(method, path):
    assert _serving_module(method, path) == EXPECTED[(method, path)]


def test_no_unguarded_screenshot_post_in_router():
    # The Aug 2026 fix removed create_screenshot as a route. If it comes back,
    # captures would skip enforce_tier_concurrency.
    import routers.screenshot as rs
    posts = [r for r in rs.router.routes if "POST" in getattr(r, "methods", set())]
    assert posts == []


def test_required_batch_file_route_check_passes():
    main._verify_required_routes(main.app)   # raises in non-prod if missing


def test_startup_runs_migrations_and_marks_ready(fake_shots, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "SCREENSHOT_READY", False)
    with TestClient(main.app) as c:
        assert main.SCREENSHOT_READY is True
        assert c.get("/health").json()["services"]["screenshot_service"] == "ready"


def test_startup_fails_loudly_outside_production(monkeypatch):
    from fastapi.testclient import TestClient

    class Broken:
        async def initialize(self):
            raise RuntimeError("Executable doesn't exist")

        async def close(self):
            pass

    monkeypatch.setattr(main, "screenshot_service", Broken())
    with pytest.raises(RuntimeError):
        with TestClient(main.app):
            pass


# --------------------------------------------------------------- basics
def test_root_and_health(client):
    assert client.get("/").json()["status"] == "running"
    h = client.get("/health").json()
    assert h["status"] == "healthy"
    assert h["services"]["stripe"] == "not_configured"
    assert h["tier_concurrency"] == {"starter": 2, "pro": 3, "business": 5}
    assert client.head("/").status_code == 200
    assert client.head("/health").status_code == 200


def test_health_leaks_no_secrets(client):
    text = client.get("/health").text
    assert main.SECRET_KEY not in text
    assert "sk_" not in text


def test_openapi_schema_builds(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert "/api/v1/screenshot" in r.json()["paths"]


# ---------------------------------------------------------- security headers
def test_security_headers_on_api_responses(client):
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["server"] == "PixelPerfect"


def test_docs_are_framable_and_csp_free(client):
    r = client.get("/docs")
    assert r.status_code == 200
    assert "x-frame-options" not in r.headers
    assert "content-security-policy" not in r.headers


def test_prod_csp_allows_the_api_and_stripe():
    assert "https://api.pixelperfectapi.net" in main.PROD_CSP
    assert "https://js.stripe.com" in main.PROD_CSP
    assert "frame-ancestors 'none'" in main.PROD_CSP


# --------------------------------------------------------------------- CORS
def test_cors_allows_production_frontend(client):
    r = client.get("/", headers={"Origin": "https://pixelperfectapi.net"})
    assert r.headers.get("access-control-allow-origin") == "https://pixelperfectapi.net"


def test_cors_refuses_unknown_origin(client):
    r = client.get("/", headers={"Origin": "https://evil.example.com"})
    assert r.headers.get("access-control-allow-origin") in (None, "")


# --------------------------------------------------------------- contact form
def test_contact_form_sends(client, monkeypatch):
    sent = []
    monkeypatch.setattr(main, "_send_email_sync", lambda *a: sent.append(a))
    r = client.post("/contact", json={"name": "N", "email": "n@example.com",
                                      "subject": "", "message": "Hello", "category": "billing"})
    assert r.status_code == 200
    assert sent and sent[0][3] == "Hello" and sent[0][2] == "(No subject)"


@pytest.mark.parametrize("payload", [
    {"name": " ", "email": "n@example.com", "subject": "s", "message": "m"},
    {"name": "N", "email": "n@example.com", "subject": "s", "message": "x" * 5001},
])
def test_contact_form_validation(client, payload, monkeypatch):
    monkeypatch.setattr(main, "_send_email_sync", lambda *a: None)
    assert client.post("/contact", json=payload).status_code == 422


def test_contact_form_smtp_failure_is_503(client, monkeypatch):
    def boom(*a):
        raise OSError("smtp down")

    monkeypatch.setattr(main, "_send_email_sync", boom)
    r = client.post("/contact", json={"name": "N", "email": "n@example.com",
                                      "subject": "s", "message": "m"})
    assert r.status_code == 503
