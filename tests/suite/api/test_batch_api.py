# tests/suite/api/test_batch_api.py
# =============================================================================
# Batch: submit (JSON and file), the background processor, job reads,
# cancel / retry / delete, and every limit on the way in.
#
# TestClient runs background tasks before it returns the response, so after a
# submit the job has already been processed when the test continues.
# =============================================================================
import io

import pytest

import batch
import models

URLS = ["https://a.example.com/", "https://b.example.com/", "https://c.example.com/"]


def _submit(client, user, urls=URLS, **extra):
    return client.post("/api/v1/batch/submit", headers=user.headers,
                       json={"urls": urls, **extra})


# --------------------------------------------------------------- submit
def test_free_tier_cannot_batch(client, make_user, fake_shots):
    r = _submit(client, make_user("free"))
    assert r.status_code == 403
    assert fake_shots.calls == []


def test_pro_batch_runs_to_completion(client, make_user, db, fake_shots):
    u = make_user("pro")
    r = _submit(client, u, dark_mode=True, delay=1, remove_elements=[".ad"])
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    assert r.json()["total"] == 3

    job = client.get(f"/api/v1/batch/jobs/{job_id}", headers=u.headers).json()
    assert job["status"] == "completed"
    assert job["completed"] == 3 and job["failed"] == 0
    assert all(it["screenshot_url"] for it in job["items"])

    rows = db.query(models.Screenshot).filter_by(user_id=u.id).all()
    assert len(rows) == 3 and {r.batch_job_id for r in rows} == {job_id}
    assert all(r.storage_url.startswith("https://r2.test/") for r in rows)   # R2 branch
    db_job = db.get(models.BatchJob, job_id)
    assert db_job.status == "completed" and db_job.completed_count == 3
    assert [c["url"] for c in fake_shots.calls] == URLS
    assert all(c["dark_mode"] and c["delay"] == 1 and c["remove_elements"] == [".ad"]
               for c in fake_shots.calls)


def test_partial_failure_marks_job_partial(client, make_user, fake_shots, monkeypatch):
    real = fake_shots.capture_screenshot

    async def flaky(url, **kw):
        if "b.example" in url:
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED at page.goto")
        return await real(url, **kw)

    monkeypatch.setattr(fake_shots, "capture_screenshot", flaky)
    u = make_user("pro")
    job_id = _submit(client, u).json()["id"]
    job = client.get(f"/api/v1/batch/jobs/{job_id}", headers=u.headers).json()
    assert job["status"] == "partial"
    [bad] = [it for it in job["items"] if it["status"] == "failed"]
    assert "could not be found" in bad["message"]     # friendly, not raw


@pytest.mark.parametrize("body, code", [
    ({"urls": []}, 400),
    ({"urls": ["ftp://x", "nonsense"]}, 400),
    ({"urls": URLS, "format": "bmp"}, 422),
    ({"urls": URLS, "width": 5000}, 422),
])
def test_bad_submissions(client, make_user, body, code):
    u = make_user("pro")
    assert client.post("/api/v1/batch/submit", headers=u.headers, json=body).status_code == code


def test_batch_size_cap_per_tier(client, make_user):
    u = make_user("pro")
    urls = [f"https://s{i}.example.com/" for i in range(batch.TIER_BATCH_LIMITS["pro"] + 1)]
    r = _submit(client, u, urls=urls)
    assert r.status_code == 403
    assert "exceeds your tier limit" in r.json()["detail"]


def test_batch_period_quota(client, make_user, db, monkeypatch):
    monkeypatch.setenv("PRO_BATCH_LIMIT", "2")
    u = make_user("pro")
    assert _submit(client, u, urls=URLS[:1]).status_code == 200
    assert _submit(client, u, urls=URLS[:1]).status_code == 200
    r = _submit(client, u, urls=URLS[:1])
    assert r.status_code == 429
    assert "2/2" in r.json()["detail"]


def test_batch_respects_screenshot_quota(client, make_user, add_screenshots, monkeypatch, fake_shots):
    monkeypatch.setenv("PRO_SCREENSHOTS_LIMIT", "10")
    u = make_user("pro")
    add_screenshots(u.id, 8)
    r = _submit(client, u)                    # 3 URLs, only 2 left
    assert r.status_code == 429
    assert "only 2 screenshot(s) remain" in r.json()["detail"]
    assert fake_shots.calls == []
    assert _submit(client, u, urls=URLS[:2]).status_code == 200


def test_batch_width_gate(client, make_user, monkeypatch):
    # Free cannot batch at all, so check the gate function directly for Free
    # and through the API for Pro.
    from fastapi import HTTPException
    from types import SimpleNamespace
    free = SimpleNamespace(subscription_tier="free")
    with pytest.raises(HTTPException) as e:
        batch._enforce_capture_gates("png", 2560, free)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        batch._enforce_capture_gates("pdf", 1280, free)
    assert e.value.status_code == 403
    batch._enforce_capture_gates("pdf", 3840, SimpleNamespace(subscription_tier="pro"))
    assert _submit(client, make_user("pro"), width=3840).status_code == 200


# ---------------------------------------------------------------- file upload
@pytest.mark.parametrize("name, content", [
    ("urls.csv", b"https://a.example.com,https://b.example.com\n"),
    ("urls.txt", b"https://a.example.com\nhttps://b.example.com\n"),
    ("urls.tsv", b"https://a.example.com\thttps://b.example.com\n"),
])
def test_submit_file(client, make_user, name, content):
    u = make_user("pro")
    r = client.post("/api/v1/batch/submit_file", headers=u.headers,
                    files={"file": (name, io.BytesIO(content), "text/plain")},
                    data={"format": "jpeg", "remove_elements": ".cookie, #popup"})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 2 and r.json()["format"] == "jpeg"


@pytest.mark.parametrize("name, content, data, code", [
    ("urls.exe", b"https://a.example.com", {}, 400),
    ("urls.txt", b"\xff\xfe\x00bad", {}, 400),
    ("urls.txt", b"no urls here", {}, 400),
    ("urls.txt", b"https://a.example.com", {"delay": "11"}, 422),
])
def test_submit_file_rejections(client, make_user, name, content, data, code):
    u = make_user("pro")
    r = client.post("/api/v1/batch/submit_file", headers=u.headers,
                    files={"file": (name, io.BytesIO(content), "text/plain")}, data=data)
    assert r.status_code == code, r.text


def test_submit_file_free_tier_403(client, make_user):
    r = client.post("/api/v1/batch/submit_file", headers=make_user("free").headers,
                    files={"file": ("u.txt", io.BytesIO(b"https://a.example.com"), "text/plain")})
    assert r.status_code == 403


# ----------------------------------------------------------- reads & ownership
def test_jobs_are_private(client, make_user):
    owner, other = make_user("pro"), make_user("pro")
    job_id = _submit(client, owner).json()["id"]
    for method, path in [
        ("get", f"/api/v1/batch/jobs/{job_id}"),
        ("post", f"/api/v1/batch/jobs/{job_id}/cancel"),
        ("post", f"/api/v1/batch/jobs/{job_id}/retry_failed"),
        ("delete", f"/api/v1/batch/jobs/{job_id}"),
    ]:
        r = getattr(client, method)(path, headers=other.headers)
        assert r.status_code == 404, (method, path)
    assert client.get("/api/v1/batch/jobs", headers=other.headers).json() == []
    assert client.get(f"/api/v1/batch/jobs/{job_id}", headers=owner.headers).status_code == 200


def test_job_survives_restart_via_db_reconstruction(client, make_user):
    u = make_user("pro")
    job_id = _submit(client, u).json()["id"]
    batch.JOBS.clear()                         # simulate a server restart
    job = client.get(f"/api/v1/batch/jobs/{job_id}", headers=u.headers).json()
    assert job["status"] == "completed" and job["completed"] == 3
    listing = client.get("/api/v1/batch/jobs", headers=u.headers).json()
    assert [j["id"] for j in listing] == [job_id]


def test_cancel_finished_job_is_400(client, make_user):
    u = make_user("pro")
    job_id = _submit(client, u).json()["id"]
    r = client.post(f"/api/v1/batch/jobs/{job_id}/cancel", headers=u.headers)
    assert r.status_code == 400


def test_cancel_queued_job(client, make_user, db):
    u = make_user("pro")
    job = batch._build_and_store_job("q1", u.id, URLS, "png", 1920, 1080, False, db)
    r = client.post("/api/v1/batch/jobs/q1/cancel", headers=u.headers)
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled" and r.json()["failed"] == 3
    db.expire_all()
    assert db.get(models.BatchJob, "q1").status == "cancelled"


def test_retry_failed_recaptures_only_failed_items(client, make_user, fake_shots, monkeypatch):
    real = fake_shots.capture_screenshot
    state = {"fail": True}

    async def sometimes(url, **kw):
        if state["fail"] and "c.example" in url:
            raise RuntimeError("timeout")
        return await real(url, **kw)

    monkeypatch.setattr(fake_shots, "capture_screenshot", sometimes)
    u = make_user("pro")
    job_id = _submit(client, u).json()["id"]
    assert len(fake_shots.calls) == 2
    state["fail"] = False
    r = client.post(f"/api/v1/batch/jobs/{job_id}/retry_failed", headers=u.headers)
    assert r.status_code == 200
    assert fake_shots.calls[-1]["url"] == "https://c.example.com/"
    job = client.get(f"/api/v1/batch/jobs/{job_id}", headers=u.headers).json()
    assert job["status"] == "completed" and job["completed"] == 3


def test_delete_job_keeps_its_screenshots(client, make_user, db):
    u = make_user("pro")
    job_id = _submit(client, u).json()["id"]
    r = client.delete(f"/api/v1/batch/jobs/{job_id}", headers=u.headers)
    assert r.status_code == 200
    db.expire_all()
    assert db.get(models.BatchJob, job_id) is None
    rows = db.query(models.Screenshot).filter_by(user_id=u.id).all()
    assert len(rows) == 3 and all(r.batch_job_id is None for r in rows)   # ON DELETE SET NULL
