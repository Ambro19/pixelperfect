# tests/suite/known_defects/test_known_defects.py
# =============================================================================
# Defects found while building this suite, each pinned by a test that asserts
# the CORRECT behaviour.
#
# Every test here is xfail(strict=True):
#   - today it fails, which pytest reports as "xfailed" (expected), so the
#     suite stays green while the defect is open;
#   - the moment the defect is fixed, the test passes, strict mode turns that
#     XPASS into a FAILURE, and the message tells you to delete the marker.
#     The test then stays as an ordinary regression test.
#
# IDs (F-01 ...) match the findings table in TESTING.md.
# =============================================================================
import asyncio
import io

import httpx
import pytest

import main
import models


def defect(fid: str, summary: str):
    # raises=AssertionError: only a failed ASSERTION counts as "defect still
    # present". Any other exception (a renamed attribute, a crash, a new
    # status code path) is reported as a real failure, so a fix written
    # differently - or a new breakage - cannot hide behind this marker.
    return pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=f"{fid}: {summary} -- if this now passes, the defect is fixed: "
               f"remove this marker so the test becomes a regression test.",
    )


URL = "https://example.com/"


# ----------------------------------------------------------------------- F-01
@defect("F-01", "services/screenshot_service.py runs Playwright sync API on 3 threads")
def test_f01_hot_path_executor_is_single_threaded():
    # Playwright's sync API is thread-affine. The root screenshot_service.py
    # fixed this in Sep 2026 (max_workers=1); the services/ copy, which serves
    # every single capture, still has 3. Proven with real Chromium in
    # integration/test_real_browser.py (5 of 6 overlapping captures fail).
    from concurrent.futures import ThreadPoolExecutor
    import services.screenshot_service as hot
    import screenshot_service as legacy

    def pools(mod):
        return [v for v in vars(mod).values() if isinstance(v, ThreadPoolExecutor)]

    for mod in (legacy, hot):
        found = pools(mod)
        assert found, f"{mod.__name__}: no module-level executor found - update this test"
        assert all(ex._max_workers == 1 for ex in found), mod.__name__


# ----------------------------------------------------------------------- F-02
@defect("F-02", "POST /api/v1/batch/submit/ (trailing slash) skips the screenshot quota")
def test_f02_trailing_slash_batch_enforces_quota(client, make_user, add_screenshots,
                                                 monkeypatch, fake_shots, db):
    # The batch router owns /api/v1/batch/submit; main.py's legacy wrapper
    # still owns the trailing-slash spelling and has no screenshot-quota
    # check and writes no BatchJob row.
    monkeypatch.setenv("PRO_SCREENSHOTS_LIMIT", "2")
    u = make_user("pro")
    add_screenshots(u.id, 2)                  # quota already used up
    urls = ["https://a.example.com/", "https://b.example.com/"]
    r = client.post("/api/v1/batch/submit/", headers=u.headers, json={"urls": urls})
    assert r.status_code == 429, r.text
    assert fake_shots.calls == []


# ----------------------------------------------------------------------- F-03
INTERNAL = [
    "http://127.0.0.1:8000/docs",
    "http://localhost/",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata service
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://[::1]/",
]


@defect("F-03", "capture URL may point at loopback / private / link-local addresses (SSRF)")
@pytest.mark.parametrize("target", INTERNAL)
def test_f03_capture_refuses_internal_addresses(client, make_user, fake_shots, target):
    r = client.post("/api/v1/screenshot", headers=make_user("free").headers, json={"url": target})
    assert r.status_code in (400, 422), (target, r.status_code)
    assert fake_shots.calls == []


@defect("F-03", "batch URLs may point at internal addresses (SSRF)")
def test_f03_batch_refuses_internal_addresses(client, make_user, fake_shots):
    r = client.post("/api/v1/batch/submit", headers=make_user("pro").headers,
                    json={"urls": ["http://169.254.169.254/latest/meta-data/"]})
    assert r.status_code in (400, 422)
    assert fake_shots.calls == []


@defect("F-03", "webhook_url may point at internal addresses (SSRF)")
def test_f03_webhook_url_refuses_internal_addresses(client, make_user, monkeypatch):
    import routers.screenshot as rs
    sent = []

    async def record(webhook_url, **kw):
        sent.append(webhook_url)

    monkeypatch.setattr(rs, "send_webhook_notification", record)
    r = client.post("/api/v1/screenshot", headers=make_user("business").headers,
                    json={"url": URL, "webhook_url": "http://169.254.169.254/hook"})
    assert r.status_code in (400, 422)
    assert sent == []


# ----------------------------------------------------------------------- F-04
@defect("F-04", "deleting a batch job refunds the per-period batch quota")
def test_f04_deleting_job_does_not_refund_batch_quota(client, make_user, monkeypatch):
    monkeypatch.setenv("PRO_BATCH_LIMIT", "1")
    u = make_user("pro")
    body = {"urls": ["https://a.example.com/"]}
    job_id = client.post("/api/v1/batch/submit", headers=u.headers, json=body).json()["id"]
    assert client.post("/api/v1/batch/submit", headers=u.headers, json=body).status_code == 429
    client.delete(f"/api/v1/batch/jobs/{job_id}", headers=u.headers)
    assert client.post("/api/v1/batch/submit", headers=u.headers, json=body).status_code == 429


# ----------------------------------------------------------------------- F-05
def _reset_link(client, sent_mail, email):
    client.post("/auth/forgot-password", json={"email": email})
    return sent_mail[-1]["link"].split("token=", 1)[1]


@defect("F-05", "a password-reset link can be used again after it has been used")
def test_f05_reset_token_is_single_use(client, make_user, sent_mail):
    make_user(email="ivy@example.com")
    token = _reset_link(client, sent_mail, "ivy@example.com")
    assert client.post("/auth/reset-password",
                       json={"token": token, "new_password": "First-pass-1"}).status_code == 200
    r = client.post("/auth/reset-password", json={"token": token, "new_password": "Second-pass-2"})
    assert r.status_code == 400


@defect("F-05", "a reset link issued before a password change still works after it")
def test_f05_reset_token_dies_when_password_changes(client, make_user, sent_mail):
    u = make_user(email="jo@example.com")
    token = _reset_link(client, sent_mail, "jo@example.com")
    assert client.post("/user/change_password", headers=u.headers, json={
        "current_password": u.password, "new_password": "Changed-pass-3"}).status_code == 200
    r = client.post("/auth/reset-password", json={"token": token, "new_password": "Attacker-4"})
    assert r.status_code == 400


# ----------------------------------------------------------------------- F-06
@defect("F-06", "an old token logs in as whoever registers the same username later")
def test_f06_token_does_not_carry_over_to_new_owner_of_username(client, make_user):
    u = make_user("pro", username="sam")
    old_headers = u.headers
    assert client.delete("/user/delete_account", headers=old_headers).status_code == 200
    client.post("/register", json={"username": "sam", "email": "newsam@example.com",
                                   "password": "Another-pass-1"})
    assert client.get("/users/me", headers=old_headers).status_code == 401


@defect("F-06", "after a rename, the old token logs in as the next owner of the name")
def test_f06_rename_does_not_hand_token_to_next_registrant(client, make_user):
    a = make_user("free", username="alice")
    old_headers = a.headers
    assert client.put("/user/update_profile", headers=old_headers,
                      json={"username": "alice2"}).status_code == 200
    client.post("/register", json={"username": "alice", "email": "bob@example.com",
                                   "password": "Bob-pass-12"})
    r = client.get("/users/me", headers=old_headers)
    assert r.status_code == 401 or r.json().get("email") != "bob@example.com"


@defect("F-06", "tokens issued before a password change stay valid for 24 h")
def test_f06_password_change_revokes_old_tokens(client, make_user):
    u = make_user("pro")
    assert client.post("/user/change_password", headers=u.headers, json={
        "current_password": u.password, "new_password": "Brand-new-pass-1"}).status_code == 200
    assert client.get("/users/me", headers=u.headers).status_code == 401


# ----------------------------------------------------------------------- F-07
@defect("F-07", "a webhook that fails is marked seen, so Stripe's retry is dropped")
def test_f07_failed_webhook_is_processed_on_retry(client, make_user, fake_stripe, monkeypatch):
    import webhook_handler
    make_user(stripe_customer_id="cus_W")
    fake_stripe.webhook_event = {
        "id": "evt_retry", "type": "customer.subscription.updated",
        "data": {"object": {"customer": "cus_W", "status": "active"}},
    }
    attempts = {"n": 0}
    real = webhook_handler._process_event

    async def flaky(db, event_type, obj):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("database blip")
        return await real(db, event_type, obj)

    monkeypatch.setattr(webhook_handler, "_process_event", flaky)
    monkeypatch.setattr(webhook_handler, "sync_user_subscription_from_stripe", lambda u, d: None)

    assert client.post("/webhook/stripe", content=b"{}").status_code == 500
    retry = client.post("/webhook/stripe", content=b"{}")   # Stripe retries
    assert retry.json().get("duplicate") is not True
    assert attempts["n"] == 2


# ----------------------------------------------------------------------- F-08
@defect("F-08", "registration accepts an empty password")
def test_f08_register_requires_a_real_password(client):
    r = client.post("/register", json={"username": "weak", "email": "weak@example.com",
                                       "password": ""})
    assert r.status_code in (400, 422)


@defect("F-08", "password reset accepts a 1-character password (change_password requires 8)")
def test_f08_reset_enforces_same_rule_as_change_password(client, make_user):
    make_user(email="kim@example.com")
    token = main.serializer.dumps({"email": "kim@example.com"})
    r = client.post("/auth/reset-password", json={"token": token, "new_password": "a"})
    assert r.status_code in (400, 422)


@defect("F-08", "registration accepts an invalid email and an empty username")
def test_f08_register_validates_email_and_username(client):
    r = client.post("/register", json={"username": "  ", "email": "not-an-email",
                                       "password": "Long-enough-1"})
    assert r.status_code in (400, 422)


# ----------------------------------------------------------------------- F-09
@defect("F-09", "forgot-password misses the account if the email is typed with capitals")
def test_f09_forgot_password_is_case_insensitive(client, make_user, sent_mail):
    # The whole flow, not just the lookup: the reset link carries the email
    # as typed, so fixing only the lookup still leaves a link that fails.
    u = make_user(email="dave@example.com")
    client.post("/auth/forgot-password", json={"email": "Dave@Example.com"})
    assert len(sent_mail) == 1
    token = sent_mail[0]["link"].split("token=", 1)[1]
    assert client.post("/auth/reset-password",
                       json={"token": token, "new_password": "Case-fixed-1"}).status_code == 200
    assert client.post("/token_json", json={"username": u.username,
                                            "password": "Case-fixed-1"}).status_code == 200


# ----------------------------------------------------------------------- F-10
@defect("F-10", "the 'created_by' Stripe metadata fix landed in unmounted payment.py")
def test_f10_new_stripe_customers_are_tagged(client, make_user, fake_stripe):
    u = make_user()
    client.post("/billing/create_portal_session", headers=u.headers, json={})
    creates = [c[1] for c in fake_stripe.calls if c[0] == "Customer.create"]
    assert creates, "no customer was created"
    assert all(kw["metadata"].get("created_by") == "pixelperfect_screenshot_api" for kw in creates)


# ----------------------------------------------------------------------- F-11
@defect("F-11", "billing-portal return_url is caller-controlled (open redirect)")
def test_f11_portal_return_url_stays_on_our_site(client, make_user, fake_stripe):
    u = make_user(stripe_customer_id="cus_P")
    client.post("/billing/create_portal_session", headers=u.headers,
                json={"return_url": "https://evil.example.com/phish"})
    portals = [c[1] for c in fake_stripe.calls if c[0] == "billing_portal.Session.create"]
    assert portals and all(kw["return_url"].startswith("http://frontend.test/") for kw in portals)


# ----------------------------------------------------------------------- F-12
@defect("F-12", "two simultaneous captures can both pass the last unit of quota")
async def test_f12_quota_holds_under_concurrency(make_user, fake_shots, fake_storage, monkeypatch):
    monkeypatch.setenv("FREE_SCREENSHOTS_LIMIT", "1")
    fake_shots.delay_s = 0.2
    u = make_user("free")
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        rs = await asyncio.gather(*[
            ac.post("/api/v1/screenshot", headers=u.headers, json={"url": URL}) for _ in range(2)
        ])
    assert sorted(r.status_code for r in rs) == [200, 429]


# ----------------------------------------------------------------------- F-13
@defect("F-13", "retry_failed recaptures without checking the period screenshot quota")
def test_f13_retry_respects_quota(client, make_user, fake_shots, monkeypatch, add_screenshots):
    monkeypatch.setenv("PRO_SCREENSHOTS_LIMIT", "3")
    real = fake_shots.capture_screenshot
    state = {"fail": True}

    async def sometimes(url, **kw):
        if state["fail"] and "c.example" in url:
            raise RuntimeError("timeout")
        return await real(url, **kw)

    monkeypatch.setattr(fake_shots, "capture_screenshot", sometimes)
    u = make_user("pro")
    urls = ["https://a.example.com/", "https://b.example.com/", "https://c.example.com/"]
    job_id = client.post("/api/v1/batch/submit", headers=u.headers, json={"urls": urls}).json()["id"]
    add_screenshots(u.id, 1)                  # 3 of 3 now used
    state["fail"] = False
    client.post(f"/api/v1/batch/jobs/{job_id}/retry_failed", headers=u.headers)
    used = client.get("/subscription_status", headers=u.headers).json()["usage"]["screenshots"]
    assert used <= 3


# ----------------------------------------------------------------------- F-14
# A hostile page or never-resolving custom_js freezes the capture worker
# (page.evaluate has no timeout). Reproduced with real Chromium by
# tests/manual/repro_f14_capture_hang.py. Not automated: a stuck worker thread
# cannot be freed and would stop pytest from exiting.


# ----------------------------------------------------------------------- F-15
def _sub(sid="sub_V", status="active"):
    return {"id": sid, "status": status,
            "items": {"data": [{"price": {"id": "price_1",
                                          "lookup_key": "pixelperfect_pro_monthly"}}]},
            "metadata": {}, "current_period_start": 1_790_000_000,
            "current_period_end": 1_792_000_000}


@defect("F-15", "sync adopts any Stripe customer with the same (unverified) email")
def test_f15_sync_never_adopts_a_stranger_customer(client, fake_stripe, db):
    # An orphaned customer (deleted account, or another product on the same
    # Stripe account) still holds a paid subscription.
    fake_stripe.customers = [{"id": "cus_VICTIM", "email": "victim@example.com",
                              "metadata": {}}]
    fake_stripe.subscriptions["cus_VICTIM"] = [_sub(status="trialing")]
    assert client.post("/register", json={"username": "newcomer", "email": "victim@example.com",
                                          "password": "Whatever-pass-1"}).status_code == 200
    tok = client.post("/token_json", json={"username": "newcomer",
                                           "password": "Whatever-pass-1"}).json()["access_token"]
    client.get("/subscription_status?sync=1", headers={"Authorization": f"Bearer {tok}"})
    db.expire_all()
    user = db.query(models.User).filter_by(username="newcomer").one()
    assert user.stripe_customer_id != "cus_VICTIM"
    assert user.subscription_tier == "free"


# ----------------------------------------------------------------------- F-16
@defect("F-16", "account deletion leaves trialing / past_due subscriptions billing")
@pytest.mark.parametrize("status", ["trialing", "past_due"])
def test_f16_deletion_cancels_every_live_subscription(client, make_user, fake_stripe, status):
    u = make_user("pro", stripe_customer_id="cus_D")
    fake_stripe.subscriptions["cus_D"] = [{"id": "sub_D", "status": status}]
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    assert ("Subscription.delete", "sub_D") in fake_stripe.calls


@defect("F-16", "account deletion skips Stripe when the local tier says free")
def test_f16_deletion_checks_stripe_even_if_local_tier_is_free(client, make_user, fake_stripe):
    u = make_user("free", stripe_customer_id="cus_E")       # a missed webhook
    fake_stripe.subscriptions["cus_E"] = [{"id": "sub_E", "status": "active"}]
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    assert ("Subscription.delete", "sub_E") in fake_stripe.calls


# ----------------------------------------------------------------------- F-17
@defect("F-17", "an API key alone can change the account email (then reset the password)")
def test_f17_api_key_cannot_change_email(client, make_user):
    u = make_user("pro", email="owner@example.com")
    r = client.put("/user/update_profile", headers=u.key_headers,
                   json={"email": "thief@example.com"})
    assert r.status_code in (400, 401, 403, 422)


@defect("F-17", "an API key alone can delete the account")
def test_f17_api_key_cannot_delete_account(client, make_user):
    u = make_user("pro")
    assert client.delete("/user/delete_account", headers=u.key_headers).status_code in (401, 403)


# ----------------------------------------------------------------------- F-18
@defect("F-18", "History-page delete leaves the stored image in R2")
def test_f18_history_delete_removes_stored_object(client, make_user, fake_storage):
    u = make_user("pro")
    sid = client.post("/api/v1/screenshot", headers=u.headers, json={"url": URL}).json()["screenshot_id"]
    assert client.delete(f"/api/v1/screenshots/{sid}", headers=u.headers).status_code == 200
    assert fake_storage.files == {}


@defect("F-18", "account deletion leaves the user's images in R2")
def test_f18_account_deletion_removes_stored_objects(client, make_user, fake_storage):
    u = make_user("pro")
    client.post("/api/v1/screenshot", headers=u.headers, json={"url": URL})
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    assert fake_storage.files == {}


# ----------------------------------------------------------------------- F-19
@defect("F-19", "500 responses echo internal exception text to the caller")
def test_f19_errors_do_not_leak_internals(client, make_user, fake_shots):
    fake_shots.raise_exc = RuntimeError(
        "psycopg2.OperationalError: password authentication failed for user render_admin")
    r = client.post("/api/v1/screenshot", headers=make_user("pro").headers, json={"url": URL})
    assert r.status_code == 500
    assert "render_admin" not in r.text and "psycopg2" not in r.text


# ----------------------------------------------------------------------- F-20
@defect("F-20", "forgot-password has no rate limit (one Gmail account sends every mail)")
def test_f20_forgot_password_is_rate_limited(client, make_user, sent_mail):
    make_user(email="target@example.com")
    codes = [client.post("/auth/forgot-password", json={"email": "target@example.com"}).status_code
             for _ in range(30)]
    assert 429 in codes or len(sent_mail) < 30


# ----------------------------------------------------------------------- F-21
@defect("F-21", "wait_for_selector is documented Pro+ but works on Free")
def test_f21_wait_for_selector_is_pro_plus(client, make_user, fake_shots):
    r = client.post("/api/v1/screenshot", headers=make_user("free").headers,
                    json={"url": URL, "wait_for_selector": "#app"})
    assert r.status_code == 403
    assert fake_shots.calls == []


# ----------------------------------------------------------------------- F-22
@defect("F-22", "checkout opens a second subscription for someone already subscribed")
def test_f22_checkout_refused_when_already_subscribed(client, make_user, fake_stripe, monkeypatch):
    monkeypatch.setenv("STRIPE_PRO_LOOKUP_KEY_MONTHLY", "pp_pro_m")
    fake_stripe.prices["pp_pro_m"] = {"id": "price_pro"}
    u = make_user("pro", stripe_customer_id="cus_S")
    fake_stripe.subscriptions["cus_S"] = [_sub(sid="sub_S")]
    r = client.post("/billing/create_checkout_session", headers=u.headers, json={"plan": "pro"})
    assert r.status_code in (400, 409)
