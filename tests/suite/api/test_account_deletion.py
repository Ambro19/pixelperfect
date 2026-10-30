# tests/suite/api/test_account_deletion.py
# =============================================================================
# DELETE /user/delete_account: permanent erasure. Everything belonging to the
# user goes; nothing belonging to anyone else is touched; their credentials
# stop working; an active paid subscription is cancelled in Stripe.
# =============================================================================
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


def test_stripe_failure_does_not_block_erasure(client, make_user, fake_stripe, db):
    # CURRENT POLICY, pinned on purpose: erasure wins over billing. If Stripe
    # is unreachable the account is still deleted - which can leave a
    # subscription billing with nobody able to cancel it. The alternative
    # (refuse deletion until Stripe confirms) is a product decision; see
    # TESTING.md, "Decisions for you". Change this test if you choose it.
    u = make_user("pro", stripe_customer_id="cus_D")
    fake_stripe.fail_subscription_list = True
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
