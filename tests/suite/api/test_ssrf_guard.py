# tests/suite/api/test_ssrf_guard.py
# =============================================================================
# Release I (F-03): every entry point refuses private targets before any
# capture starts, and webhooks re-check at delivery time.
# =============================================================================
import io

import pytest


def test_single_capture_refuses_hostname_that_resolves_private(client, make_user, fake_shots):
    r = client.post("/api/v1/screenshot", headers=make_user("free").headers,
                    json={"url": "http://internal.attacker.test/"})
    assert r.status_code == 400
    assert "private or internal" in r.json()["detail"]
    assert fake_shots.calls == []


def test_public_capture_still_works(client, make_user, fake_shots):
    r = client.post("/api/v1/screenshot", headers=make_user("free").headers,
                    json={"url": "https://example.com/"})
    assert r.status_code == 200, r.text
    assert len(fake_shots.calls) == 1


def test_refused_capture_does_not_use_quota(client, make_user, fake_shots):
    u = make_user("free")
    assert client.post("/api/v1/screenshot", headers=u.headers, json={"url": "http://10.0.0.1/"}).status_code == 400
    assert client.post("/api/v1/screenshot", headers=u.headers, json={"url": "https://example.com/"}).status_code == 200
    assert client.get("/subscription_status", headers=u.headers).json()["usage"]["screenshots"] == 1


def test_webhook_url_resolving_private_is_refused(client, make_user, fake_shots):
    r = client.post("/api/v1/screenshot", headers=make_user("business").headers,
                    json={"url": "https://example.com/", "webhook_url": "https://metadata.attacker.test/hook"})
    assert r.status_code == 400
    assert fake_shots.calls == []


async def test_webhook_is_rechecked_at_delivery(monkeypatch):
    import routers.screenshot as rs
    import url_safety
    monkeypatch.setattr(url_safety, "_resolve", lambda h: ["10.9.9.9"])   # DNS changed later
    posted = []

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): posted.append(a)
    monkeypatch.setattr(rs.httpx, "AsyncClient", Client)
    await rs.send_webhook_notification("https://hooks.example.com/x", {"ok": True}, secret="s", max_retries=1)
    assert posted == []


def test_batch_with_one_private_url_is_refused_whole(client, make_user, fake_shots):
    r = client.post("/api/v1/batch/submit", headers=make_user("pro").headers,
                    json={"urls": ["https://example.com/", "http://192.168.0.1/router"]})
    assert r.status_code == 400
    assert "http://192.168.0.1/router" in r.json()["detail"]
    assert fake_shots.calls == []


def test_batch_file_upload_is_checked_too(client, make_user, fake_shots):
    data = "https://example.com/\nhttp://localhost:10000/health\n".encode()
    r = client.post("/api/v1/batch/submit_file", headers=make_user("pro").headers,
                    files={"file": ("urls.txt", io.BytesIO(data), "text/plain")})
    assert r.status_code == 400
    assert fake_shots.calls == []


def test_public_batch_still_accepted(client, make_user, fake_shots):
    r = client.post("/api/v1/batch/submit", headers=make_user("pro").headers,
                    json={"urls": ["https://a.example.com/", "https://b.example.com/"]})
    assert r.status_code == 200, r.text
