# tests/suite/api/test_screenshot_api.py
# =============================================================================
# POST /api/v1/screenshot and the screenshot read/delete endpoints.
# The capture engine and storage are fakes (see conftest.py); everything else
# — routing, auth, tier gates, quota, persistence — is the real code.
# =============================================================================
import pytest

import models

URL = "https://example.com/"


def _shoot(client, user, **body):
    return client.post("/api/v1/screenshot", headers=user.headers, json={"url": URL, **body})


def _used(client, user):
    r = client.get("/subscription_status", headers=user.headers)
    assert r.status_code == 200, r.text
    return r.json()["usage"]["screenshots"]


# --------------------------------------------------------------- happy path
def test_capture_persists_row_and_reports_usage(client, make_user, db, fake_shots, fake_storage):
    u = make_user("free")
    r = _shoot(client, u, width=1280, height=720)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == URL
    assert body["width"] == 1280 and body["height"] == 720
    assert body["usage"] == {**body["usage"], "current": 1, "limit": 100, "remaining": 99}
    assert body["screenshot_url"] == f"https://r2.test/screenshots/{u.id}/{body['screenshot_id']}.png"

    row = db.get(models.Screenshot, body["screenshot_id"])
    assert row.user_id == u.id and row.status == "completed"
    assert row.size_bytes > 0 and row.expires_at is not None
    assert fake_shots.calls[0]["url"] == URL
    assert f"screenshots/{u.id}/{body['screenshot_id']}.png" in fake_storage.files


def test_capture_with_api_key(client, make_user):
    u = make_user("free")
    r = client.post("/api/v1/screenshot", headers=u.key_headers, json={"url": URL})
    assert r.status_code == 200


def test_trailing_slash_variant_is_also_a_capture(client, make_user):
    # Regression for the Aug 2026 405: the router's GET "/" owns this path too.
    u = make_user("free")
    r = client.post("/api/v1/screenshot/", headers=u.headers, json={"url": URL})
    assert r.status_code == 200, r.text


def test_return_url_false_hides_url(client, make_user):
    u = make_user("free")
    assert _shoot(client, u, return_url=False).json()["screenshot_url"] is None


def test_options_are_forwarded_to_the_engine(client, make_user, fake_shots):
    u = make_user("pro")
    r = _shoot(client, u, full_page=True, dark_mode=True, delay=2, format="webp",
               remove_elements=[".cookie"], custom_js="1+1", wait_for_selector="#app",
               device="iphone_13")
    assert r.status_code == 200, r.text
    call = fake_shots.calls[0]
    assert call["full_page"] is True and call["dark_mode"] is True
    assert call["delay"] == 2 and call["format"] == "webp"
    assert call["remove_elements"] == [".cookie"]
    assert call["custom_js"] == "1+1" and call["wait_for_selector"] == "#app"
    assert call["device"] == "iphone_13"
    assert r.json()["device_used"] == "iphone_13"


def test_r2_failure_falls_back_to_local_file(client, make_user, fake_storage, monkeypatch, tmp_path):
    import routers.screenshot as rs
    monkeypatch.setattr(rs, "SCREENSHOTS_DIR", tmp_path / "local")
    fake_storage.fail_uploads = True
    u = make_user("free")
    r = _shoot(client, u)
    assert r.status_code == 200
    sid = r.json()["screenshot_id"]
    assert r.json()["screenshot_url"] == f"/screenshots/{u.id}/{sid}.png"
    assert (tmp_path / "local" / str(u.id) / f"{sid}.png").exists()


# ----------------------------------------------------------- input validation
@pytest.mark.parametrize("body", [
    {"url": "not a url"},
    {"url": URL, "width": 100},
    {"url": URL, "width": 5000},
    {"url": URL, "height": 100},
    {"url": URL, "format": "gif"},
    {"url": URL, "delay": 11},
    {"url": URL, "quality": 101},
    {"url": URL, "custom_js": "x" * 10_001},
])
def test_invalid_input_is_422(client, make_user, body):
    u = make_user("premium")
    assert client.post("/api/v1/screenshot", headers=u.headers, json=body).status_code == 422


def test_capture_requires_auth(client):
    assert client.post("/api/v1/screenshot", json={"url": URL}).status_code == 401


# ------------------------------------------------------------------ quota
def test_quota_blocks_at_limit_with_429(client, make_user, add_screenshots, monkeypatch, fake_shots):
    monkeypatch.setenv("FREE_SCREENSHOTS_LIMIT", "3")
    u = make_user("free")
    add_screenshots(u.id, 2)
    assert _shoot(client, u).status_code == 200
    r = _shoot(client, u)
    assert r.status_code == 429
    assert "3/3" in r.json()["detail"]
    assert len(fake_shots.calls) == 1        # the refused request never captured


@pytest.mark.parametrize("delete_path", [
    "/api/v1/screenshots/{id}",   # main.py, used by History.js
    "/api/v1/screenshot/{id}",    # routers/screenshot.py, API clients
])
def test_deleting_does_not_refund_quota(client, make_user, db, delete_path, monkeypatch):
    monkeypatch.setenv("FREE_SCREENSHOTS_LIMIT", "2")
    u = make_user("free")
    ids = [_shoot(client, u).json()["screenshot_id"] for _ in range(2)]
    assert _shoot(client, u).status_code == 429

    for sid in ids:
        r = client.delete(delete_path.format(id=sid), headers=u.headers)
        assert r.status_code == 200 and r.json()["usage_refunded"] is False

    assert db.query(models.Screenshot).filter_by(user_id=u.id).count() == 0
    assert db.query(models.ScreenshotDeletion).filter_by(user_id=u.id).count() == 2
    assert _used(client, u) == 2
    assert _shoot(client, u).status_code == 429


def test_api_delete_removes_stored_object(client, make_user, fake_storage):
    u = make_user("free")
    sid = _shoot(client, u).json()["screenshot_id"]
    key = f"screenshots/{u.id}/{sid}.png"
    assert key in fake_storage.files
    assert client.delete(f"/api/v1/screenshot/{sid}", headers=u.headers).status_code == 200
    assert fake_storage.deleted == [key] and key not in fake_storage.files


def test_premium_is_unlimited(client, make_user, add_screenshots):
    u = make_user("premium")
    add_screenshots(u.id, 150)
    r = _shoot(client, u)
    assert r.status_code == 200
    assert r.json()["usage"]["limit"] == "unlimited"
    assert r.json()["usage"]["remaining"] == "unlimited"


# --------------------------------------------------------------- tier gates
GATES = [
    # (body extras, minimum tier that may use it)
    ({"format": "pdf"}, "pro"),
    ({"custom_js": "document.title"}, "pro"),
    ({"device": "iphone_13"}, "pro"),
    ({"width": 2560}, "pro"),
    ({"target_element": "#hero"}, "business"),
    ({"webhook_url": "https://hooks.example.com/x"}, "business"),
]
ORDER = ["free", "pro", "business", "premium"]


@pytest.mark.parametrize("extra, minimum", GATES)
@pytest.mark.parametrize("tier", ORDER)
def test_tier_gates(client, make_user, fake_shots, monkeypatch, extra, minimum, tier):
    import routers.screenshot as rs

    async def _no_webhook(*a, **k):
        return None

    monkeypatch.setattr(rs, "send_webhook_notification", _no_webhook)
    u = make_user(tier)
    r = _shoot(client, u, **extra)
    allowed = ORDER.index(tier) >= ORDER.index(minimum)
    if allowed:
        assert r.status_code == 200, (tier, extra, r.text)
    else:
        assert r.status_code in (400, 403), (tier, extra, r.text)
        assert fake_shots.calls == []      # gate fires before the browser does


def test_device_preset_skips_width_gate(client, make_user):
    # A device preset replaces the viewport, so its width check does not apply.
    u = make_user("pro")
    assert _shoot(client, u, device="iphone_13", width=3840).status_code == 200


def test_devices_list_is_pro_plus(client, make_user):
    assert client.get("/api/v1/screenshot/devices", headers=make_user("free").headers).status_code == 403
    r = client.get("/api/v1/screenshot/devices", headers=make_user("pro").headers)
    assert r.status_code == 200 and "iphone_13" in r.json()["devices"]


# ------------------------------------------------------------- engine errors
def test_engine_thread_error_is_503_not_500(client, make_user, fake_shots, db):
    fake_shots.raise_exc = RuntimeError("greenlet.error: Cannot switch to a different thread")
    u = make_user("free")
    r = _shoot(client, u)
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "5"
    assert db.query(models.Screenshot).count() == 0


def test_service_value_error_is_400(client, make_user, fake_shots):
    fake_shots.raise_exc = ValueError("Selector '#nope' matched no element")
    r = _shoot(client, make_user("business"), target_element="#nope")
    assert r.status_code == 400
    assert "#nope" in r.json()["detail"]


def test_unexpected_error_is_500_and_writes_nothing(client, make_user, fake_shots, db):
    fake_shots.raise_exc = KeyError("boom")
    r = _shoot(client, make_user("free"))
    assert r.status_code == 500
    assert db.query(models.Screenshot).count() == 0


def test_truncated_capture_is_reported(client, make_user, fake_shots):
    fake_shots.result = {"truncated": True, "document_height": 40_000}
    r = _shoot(client, make_user("free"), full_page=True)
    assert r.status_code == 200
    assert r.json()["truncated"] is True
    assert "40000px" in r.json()["message"]


# ------------------------------------------------------- reads & ownership
def test_get_and_list_own_screenshots(client, make_user):
    u = make_user("free")
    sid = _shoot(client, u).json()["screenshot_id"]
    assert client.get(f"/api/v1/screenshot/{sid}", headers=u.headers).json()["id"] == sid
    listing = client.get("/api/v1/screenshot/", headers=u.headers).json()
    assert listing["total"] == 1 and listing["screenshots"][0]["id"] == sid


def test_other_users_screenshots_are_invisible(client, make_user):
    owner, intruder = make_user("free"), make_user("free")
    sid = _shoot(client, owner).json()["screenshot_id"]
    assert client.get(f"/api/v1/screenshot/{sid}", headers=intruder.headers).status_code == 404
    assert client.get("/api/v1/screenshot/", headers=intruder.headers).json()["total"] == 0
    for path in ("/api/v1/screenshot/{id}", "/api/v1/screenshots/{id}"):
        assert client.delete(path.format(id=sid), headers=intruder.headers).status_code == 404
    # Still there for the owner.
    assert client.get(f"/api/v1/screenshot/{sid}", headers=owner.headers).status_code == 200


def test_history_endpoints_are_scoped_to_user(client, make_user, add_screenshots):
    a, b = make_user("free"), make_user("free")
    add_screenshots(a.id, 3)
    add_screenshots(b.id, 1)

    ha = client.get("/api/v1/user/screenshot-history", headers=a.headers).json()
    hb = client.get("/api/v1/user/screenshot-history", headers=b.headers).json()
    assert (ha["total"], hb["total"]) == (3, 1)
    assert {s["id"] for s in ha["screenshots"]}.isdisjoint({s["id"] for s in hb["screenshots"]})

    sa = client.get("/api/v1/user/activity-summary", headers=a.headers).json()
    sb = client.get("/api/v1/user/activity-summary", headers=b.headers).json()
    assert (sa["total"], sb["total"]) == (3, 1)

    assert client.get("/api/v1/user/recent-activity", headers=a.headers).status_code == 200


def test_usage_stats_endpoint(client, make_user, add_screenshots):
    u = make_user("pro")
    add_screenshots(u.id, 4)
    r = client.get("/api/v1/screenshot/stats/usage", headers=u.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["features"]["pdf"] is True and body["features"]["webhooks"] is False
