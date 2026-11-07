# tests/suite/api/test_account_deletion.py
# =============================================================================
# DELETE /user/delete_account: permanent erasure. Everything belonging to the
# user goes; nothing belonging to anyone else is touched; their credentials
# stop working; an active paid subscription is cancelled in Stripe.
# =============================================================================
import pytest

import models


def _counts(db, uid):
    return {
        "users": db.query(models.User).filter_by(id=uid).count(),
        "screenshots": db.query(models.Screenshot).filter_by(user_id=uid).count(),
        "tombstones": db.query(models.ScreenshotDeletion).filter_by(user_id=uid).count(),
        "api_keys": db.query(models.ApiKey).filter_by(user_id=uid).count(),
        "batch_jobs": db.query(models.BatchJob).filter_by(user_id=uid).count(),
        "subscriptions": db.query(models.Subscription).filter_by(user_id=uid).count(),
    }


def _seed(client, db, user):
    """Give the user one of everything."""
    sid = client.post("/api/v1/screenshot", headers=user.headers,
                      json={"url": "https://example.com/"}).json()["screenshot_id"]
    keep = client.post("/api/v1/screenshot", headers=user.headers,
                       json={"url": "https://example.com/2"}).json()["screenshot_id"]
    assert client.delete(f"/api/v1/screenshots/{sid}", headers=user.headers).status_code == 200
    db.add(models.BatchJob(id=f"job{user.id}", user_id=user.id, total_urls=1))
    db.add(models.Subscription(user_id=user.id, stripe_subscription_id=f"sub_{user.id}",
                               stripe_customer_id=f"cus_{user.id}", tier="pro", status="active"))
    db.commit()
    return keep


def test_delete_account_erases_everything(client, make_user, db):
    u = make_user("free")
    _seed(client, db, u)
    before = _counts(db, u.id)
    assert all(v >= 1 for v in before.values()), before

    r = client.delete("/user/delete_account", headers=u.headers)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert _counts(db, u.id) == {k: 0 for k in before}


def test_delete_account_leaves_other_users_alone(client, make_user, db):
    victim, bystander = make_user("free"), make_user("free")
    _seed(client, db, victim)
    _seed(client, db, bystander)
    before = _counts(db, bystander.id)
    assert client.delete("/user/delete_account", headers=victim.headers).status_code == 200
    db.expire_all()
    assert _counts(db, bystander.id) == before


def test_credentials_stop_working(client, make_user, db):
    u = make_user("free")
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    assert client.get("/users/me", headers=u.headers).status_code == 401
    assert client.get("/users/me", headers=u.key_headers).status_code == 401
    assert client.post("/token_json", json={"username": u.username,
                                            "password": u.password}).status_code == 401


def test_username_and_email_are_free_again(client, make_user):
    u = make_user("free", username="reuse", email="reuse@example.com")
    client.delete("/user/delete_account", headers=u.headers)
    r = client.post("/register", json={"username": "reuse", "email": "reuse@example.com",
                                       "password": "Again-pass-1"})
    assert r.status_code == 200


def test_paid_subscription_is_cancelled_in_stripe(client, make_user, fake_stripe):
    u = make_user("pro", stripe_customer_id="cus_D")
    fake_stripe.subscriptions["cus_D"] = [{"id": "sub_D", "status": "active"}]
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    assert ("Subscription.delete", "sub_D") in fake_stripe.calls


# ---------------------------------------------------------------------------
# Billing-safe deletion (decision of Oct 2026, fixes F-16):
# billing must be confirmed cancelled BEFORE any data is deleted. If Stripe
# cannot confirm, the request is refused with 503 and nothing is deleted.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["active", "trialing", "past_due", "unpaid", "incomplete", "paused"])
def test_every_subscription_that_can_bill_is_cancelled(client, make_user, fake_stripe, status):
    u = make_user("pro", stripe_customer_id="cus_D")
    fake_stripe.subscriptions["cus_D"] = [{"id": "sub_D", "status": status}]
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    assert ("Subscription.delete", "sub_D") in fake_stripe.calls


def test_finished_subscriptions_are_left_alone(client, make_user, fake_stripe):
    u = make_user("pro", stripe_customer_id="cus_D")
    fake_stripe.subscriptions["cus_D"] = [
        {"id": "sub_old", "status": "canceled"},
        {"id": "sub_exp", "status": "incomplete_expired"},
        {"id": "sub_live", "status": "active"},
    ]
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    deletes = [c[1] for c in fake_stripe.calls if c[0] == "Subscription.delete"]
    assert deletes == ["sub_live"]


def test_stripe_is_asked_even_when_local_tier_says_free(client, make_user, fake_stripe):
    # A missed webhook can leave the local tier at "free" while Stripe still bills.
    u = make_user("free", stripe_customer_id="cus_E")
    fake_stripe.subscriptions["cus_E"] = [{"id": "sub_E", "status": "active"}]
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    assert ("Subscription.delete", "sub_E") in fake_stripe.calls


def _assert_nothing_deleted(client, db, u, before):
    db.expire_all()
    assert db.get(models.User, u.id) is not None
    assert _counts(db, u.id) == before
    assert client.get("/users/me", headers=u.headers).status_code == 200      # still signed in
    assert client.get("/users/me", headers=u.key_headers).status_code == 200  # key still works


@pytest.mark.parametrize("failure", ["list", "cancel"])
def test_stripe_unreachable_refuses_and_deletes_nothing(client, make_user, fake_stripe, db,
                                                        monkeypatch, failure):
    u = make_user("pro", stripe_customer_id="cus_D")
    _seed(client, db, u)
    fake_stripe.subscriptions["cus_D"] = [{"id": "sub_D", "status": "active"}]
    before = _counts(db, u.id)
    if failure == "list":
        fake_stripe.fail_subscription_list = True
    else:
        def boom(sid):
            raise RuntimeError("stripe timeout")
        monkeypatch.setattr(fake_stripe.Subscription, "delete", staticmethod(boom))

    r = client.delete("/user/delete_account", headers=u.headers)
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "60"
    assert "account was not deleted" in r.json()["detail"]
    assert "stripe timeout" not in r.text and "subscription list down" not in r.text  # no internals
    _assert_nothing_deleted(client, db, u, before)


def test_missing_stripe_key_refuses_when_user_has_a_customer(client, make_user, db, monkeypatch):
    # The configuration-mistake case: STRIPE_SECRET_KEY missing or mistyped on
    # the server. Previously Stripe was silently skipped and the deletion went
    # ahead while the subscription kept billing.
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    u = make_user("pro", stripe_customer_id="cus_D")
    before = _counts(db, u.id)
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 503
    _assert_nothing_deleted(client, db, u, before)


def test_retry_after_a_failure_completes_the_deletion(client, make_user, fake_stripe, db):
    u = make_user("pro", stripe_customer_id="cus_D")
    fake_stripe.subscriptions["cus_D"] = [{"id": "sub_D", "status": "active"}]
    fake_stripe.fail_subscription_list = True
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 503
    fake_stripe.fail_subscription_list = False
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    db.expire_all()
    assert db.get(models.User, u.id) is None


def test_mistyped_key_gives_a_configuration_message(client, make_user, fake_stripe, db):
    # A wrong or revoked key is not a passing outage: say so, and don't
    # invite the user to retry every minute.
    u = make_user("pro", stripe_customer_id="cus_D")
    fake_stripe.auth_error = True
    before = _counts(db, u.id)
    r = client.delete("/user/delete_account", headers=u.headers)
    assert r.status_code == 503
    assert "problem on our side" in r.json()["detail"]
    assert "retry-after" not in r.headers
    assert "sk_live" not in r.text
    _assert_nothing_deleted(client, db, u, before)


def test_customer_deleted_in_stripe_does_not_block_deletion(client, make_user, fake_stripe, db):
    # "No such customer" (test-mode id on a live key, or removed in the
    # dashboard) means nothing can bill there. Refusing would lock the user
    # into an account they can never delete.
    u = make_user("free", stripe_customer_id="cus_GONE")
    fake_stripe.missing_customers.add("cus_GONE")
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    db.expire_all()
    assert db.get(models.User, u.id) is None


def test_second_customer_owned_by_this_account_is_also_cancelled(client, make_user, fake_stripe):
    u = make_user("pro", email="dup@example.com", stripe_customer_id="cus_A")
    fake_stripe.customers = [
        {"id": "cus_A", "email": "dup@example.com", "metadata": {"app_user_id": str(u.id)}},
        {"id": "cus_B", "email": "dup@example.com", "metadata": {"app_user_id": str(u.id)}},
    ]
    fake_stripe.subscriptions["cus_B"] = [{"id": "sub_B", "status": "active"}]
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    assert ("Subscription.delete", "sub_B") in fake_stripe.calls


def test_owned_customer_found_even_without_a_stored_id(client, make_user, fake_stripe):
    u = make_user("pro", email="nolocal@example.com")
    fake_stripe.customers = [
        {"id": "cus_C", "email": "nolocal@example.com", "metadata": {"app_user_id": str(u.id)}},
    ]
    fake_stripe.subscriptions["cus_C"] = [{"id": "sub_C", "status": "active"}]
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    assert ("Subscription.delete", "sub_C") in fake_stripe.calls


def test_a_strangers_customer_with_the_same_email_is_never_touched(client, make_user, fake_stripe):
    # Emails are not verified: someone could register with the victim's
    # address and then delete "their" account. Only customers tagged with
    # THIS account's id may be cancelled.
    u = make_user("free", email="victim@example.com")
    fake_stripe.customers = [
        {"id": "cus_VICTIM", "email": "victim@example.com", "metadata": {"app_user_id": "999"}},
        {"id": "cus_UNTAGGED", "email": "victim@example.com", "metadata": {}},
    ]
    fake_stripe.subscriptions["cus_VICTIM"] = [{"id": "sub_V", "status": "active"}]
    fake_stripe.subscriptions["cus_UNTAGGED"] = [{"id": "sub_U", "status": "active"}]
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    assert not [c for c in fake_stripe.calls if c[0] == "Subscription.delete"]


def test_retry_after_a_partial_cancel_finishes_the_job(client, make_user, fake_stripe, db, monkeypatch):
    u = make_user("pro", stripe_customer_id="cus_D")
    fake_stripe.subscriptions["cus_D"] = [{"id": "sub_1", "status": "active"},
                                          {"id": "sub_2", "status": "trialing"}]
    real_delete = fake_stripe.Subscription.delete

    def flaky(sid):
        if sid == "sub_2":
            raise fake_stripe.error.RateLimitError("Too many requests")
        return real_delete(sid)

    monkeypatch.setattr(fake_stripe.Subscription, "delete", staticmethod(flaky))
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 503
    monkeypatch.setattr(fake_stripe.Subscription, "delete", staticmethod(real_delete))
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    deletes = [c[1] for c in fake_stripe.calls if c[0] == "Subscription.delete"]
    assert deletes == ["sub_1", "sub_2"]            # sub_1 not cancelled twice


def test_every_page_of_subscriptions_is_checked(client, make_user, fake_stripe):
    u = make_user("pro", stripe_customer_id="cus_D")
    subs = [{"id": f"sub_{i}", "status": "canceled"} for i in range(150)]
    subs.append({"id": "sub_last", "status": "past_due"})     # on the second page
    fake_stripe.subscriptions["cus_D"] = subs
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    assert ("Subscription.delete", "sub_last") in fake_stripe.calls


async def test_slow_stripe_does_not_freeze_other_requests(make_user, fake_stripe, monkeypatch):
    # Stripe's client is synchronous. Called directly in the async endpoint,
    # one slow call would stall every other request on the server.
    import asyncio
    import time
    import httpx
    import main

    u = make_user("pro", stripe_customer_id="cus_D")
    real_list = fake_stripe.Subscription.list

    def slow_list(**kw):
        time.sleep(0.8)
        return real_list(**kw)

    monkeypatch.setattr(fake_stripe.Subscription, "list", staticmethod(slow_list))
    gaps, stop = [], False

    async def heartbeat():
        last = time.monotonic()
        while not stop:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        beat = asyncio.create_task(heartbeat())
        r = await ac.delete("/user/delete_account", headers=u.headers)
        stop = True
        await beat
    assert r.status_code == 200
    assert max(gaps) < 0.5, max(gaps)


def test_user_without_stripe_customer_needs_no_stripe(client, make_user, db):
    # Never reached Stripe (no customer id): nothing to cancel, deletion proceeds
    # even with Stripe unconfigured.
    u = make_user("free")
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    db.expire_all()
    assert db.get(models.User, u.id) is None


def test_local_screenshot_files_are_removed(client, make_user, db, tmp_path):
    u = make_user("free")
    f = tmp_path / "shot.png"
    f.write_bytes(b"x")
    db.add(models.Screenshot(user_id=u.id, url="https://e.com", screenshot_path=str(f)))
    db.commit()
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200
    assert not f.exists()
