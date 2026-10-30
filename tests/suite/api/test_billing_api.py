# tests/suite/api/test_billing_api.py
# =============================================================================
# Stripe-facing endpoints: webhook verification and idempotency, checkout,
# portal, cancellation, subscription status/state. Stripe is a fake
# (conftest.FakeStripe); no request leaves the process.
# =============================================================================
from datetime import datetime

import pytest

import main
import models
import webhook_handler


def _event(eid="evt_1", etype="customer.subscription.updated", **obj):
    return {"id": eid, "type": etype, "data": {"object": obj}}


@pytest.fixture
def no_sync(monkeypatch):
    """Record sync calls from the webhook handler instead of running them."""
    calls = []
    monkeypatch.setattr(webhook_handler, "sync_user_subscription_from_stripe",
                        lambda user, db: calls.append(user.id))
    return calls


# ------------------------------------------------------------------- webhook
def test_webhook_503_when_stripe_not_configured(client):
    assert client.post("/webhook/stripe", content=b"{}").status_code == 503


def test_webhook_bad_signature_is_400(client, make_user, fake_stripe, no_sync):
    # A well-formed, mappable event body, so the ONLY reason to refuse it is
    # the signature. (An empty body would be refused for other reasons too.)
    make_user(stripe_customer_id="cus_W")
    fake_stripe.webhook_event = _event(customer="cus_W", status="active")
    fake_stripe.webhook_raises = ValueError("No signatures found")
    body = b'{"id":"evt_1","type":"customer.subscription.updated","data":{"object":{"customer":"cus_W"}}}'
    r = client.post("/webhook/stripe", content=body, headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid signature"
    assert no_sync == []


def test_webhook_missing_secret_is_500(client, fake_stripe, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET")
    assert client.post("/webhook/stripe", content=b"{}").status_code == 500


def test_webhook_updates_mapped_user_and_syncs(client, make_user, fake_stripe, no_sync, db):
    u = make_user("free", stripe_customer_id="cus_W")
    fake_stripe.webhook_event = _event(customer="cus_W", status="active",
                                       current_period_end=1_800_000_000)
    r = client.post("/webhook/stripe", content=b"{}", headers={"stripe-signature": "sig"})
    assert r.status_code == 200, r.text
    assert r.json()["mapped_user"] is True and r.json()["user_id"] == u.id
    assert no_sync == [u.id]
    db.expire_all()
    user = db.get(models.User, u.id)
    assert user.stripe_subscription_status == "active"
    assert user.subscription_expires_at is not None


def test_webhook_duplicate_event_is_acknowledged_once(client, make_user, fake_stripe, no_sync):
    make_user(stripe_customer_id="cus_W")
    fake_stripe.webhook_event = _event(customer="cus_W", status="active")
    first = client.post("/webhook/stripe", content=b"{}")
    second = client.post("/webhook/stripe", content=b"{}")
    assert first.status_code == second.status_code == 200
    assert second.json() == {"status": "ok", "duplicate": True}
    assert len(no_sync) == 1


def test_webhook_irrelevant_event_ignored(client, fake_stripe, no_sync):
    fake_stripe.webhook_event = _event(etype="charge.refunded")
    r = client.post("/webhook/stripe", content=b"{}")
    assert r.json()["ignored"] is True and no_sync == []


def test_webhook_unmapped_customer_is_acknowledged(client, fake_stripe, no_sync):
    fake_stripe.webhook_event = _event(customer="cus_UNKNOWN")
    r = client.post("/webhook/stripe", content=b"{}")
    assert r.status_code == 200 and r.json()["mapped_user"] is False


def test_checkout_completed_attaches_customer_by_email(client, make_user, fake_stripe, no_sync, db):
    u = make_user(email="payer@example.com")
    fake_stripe.webhook_event = _event(
        etype="checkout.session.completed", customer="cus_NEW",
        customer_details={"email": "Payer@Example.com"},
    )
    r = client.post("/webhook/stripe", content=b"{}")
    assert r.json()["user_id"] == u.id
    db.expire_all()
    assert db.get(models.User, u.id).stripe_customer_id == "cus_NEW"


def test_checkout_does_not_steal_a_customer_id_owned_by_someone_else(
        client, make_user, fake_stripe, no_sync, db):
    owner = make_user(stripe_customer_id="cus_TAKEN")
    other = make_user(email="other@example.com")
    fake_stripe.webhook_event = _event(
        etype="checkout.session.completed", customer="cus_TAKEN",
        customer_details={"email": "other@example.com"},
    )
    r = client.post("/webhook/stripe", content=b"{}")
    # Matched by customer id first, so it maps to the owner and never
    # re-attaches the id to the email match.
    assert r.json()["user_id"] == owner.id
    db.expire_all()
    assert db.get(models.User, other.id).stripe_customer_id is None


def test_webhook_sync_failure_is_non_fatal(client, make_user, fake_stripe, monkeypatch):
    make_user(stripe_customer_id="cus_W")

    def boom(user, db):
        raise RuntimeError("stripe timeout")

    monkeypatch.setattr(webhook_handler, "sync_user_subscription_from_stripe", boom)
    fake_stripe.webhook_event = _event(customer="cus_W", status="active")
    assert client.post("/webhook/stripe", content=b"{}").status_code == 200


# ------------------------------------------------------------------ checkout
@pytest.mark.parametrize("payload", [
    {"plan": "gold"}, {"plan": "free"}, {"plan": "pro", "billing_cycle": "weekly"},
])
def test_checkout_rejects_bad_plan_or_cycle(client, make_user, fake_stripe, payload):
    u = make_user(stripe_customer_id="cus_C")
    assert client.post("/billing/create_checkout_session", headers=u.headers,
                       json=payload).status_code == 400


def test_checkout_503_without_stripe(client, make_user):
    u = make_user()
    assert client.post("/billing/create_checkout_session", headers=u.headers,
                       json={"plan": "pro"}).status_code == 503


@pytest.mark.parametrize("cycle, env_key", [
    ("monthly", "STRIPE_PRO_LOOKUP_KEY_MONTHLY"),
    ("yearly", "STRIPE_PRO_LOOKUP_KEY_YEARLY"),
])
def test_checkout_uses_lookup_key_and_tags_user(client, make_user, fake_stripe, monkeypatch,
                                                cycle, env_key):
    monkeypatch.setenv(env_key, f"pp_pro_{cycle}")
    fake_stripe.prices[f"pp_pro_{cycle}"] = {"id": f"price_{cycle}"}
    u = make_user(stripe_customer_id="cus_C")
    r = client.post("/billing/create_checkout_session", headers=u.headers,
                    json={"plan": "PRO", "billing_cycle": cycle})
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://checkout.stripe.test/")
    [(_, kw)] = [c for c in fake_stripe.calls if c[0] == "checkout.Session.create"]
    assert kw["customer"] == "cus_C"
    assert kw["line_items"] == [{"price": f"price_{cycle}", "quantity": 1}]
    assert kw["client_reference_id"] == str(u.id)
    assert kw["metadata"]["plan"] == "pro"
    assert kw["success_url"].startswith("http://frontend.test/")


def test_checkout_missing_lookup_key_is_500(client, make_user, fake_stripe):
    u = make_user(stripe_customer_id="cus_C")
    r = client.post("/billing/create_checkout_session", headers=u.headers, json={"plan": "business"})
    assert r.status_code == 500


# --------------------------------------------------------------------- cancel
def test_cancel_on_free_is_400(client, make_user, fake_stripe):
    u = make_user("free")
    assert client.post("/billing/cancel_subscription", headers=u.headers).status_code == 400


def test_cancel_sets_cancel_at_period_end(client, make_user, fake_stripe):
    u = make_user("pro", stripe_customer_id="cus_X")
    fake_stripe.subscriptions["cus_X"] = [
        {"id": "sub_X", "status": "active", "current_period_end": 1_800_000_000}
    ]
    r = client.post("/billing/cancel_subscription", headers=u.headers)
    assert r.status_code == 200, r.text
    assert ("Subscription.modify", "sub_X", {"cancel_at_period_end": True}) in fake_stripe.calls
    datetime.fromisoformat(r.json()["cancel_at"])


def test_cancel_already_scheduled_returns_iso_date(client, make_user, fake_stripe):
    u = make_user("pro", stripe_customer_id="cus_X")
    fake_stripe.subscriptions["cus_X"] = [
        {"id": "sub_X", "status": "past_due", "cancel_at_period_end": True,
         "cancel_at": 1_800_000_000}
    ]
    r = client.post("/billing/cancel_subscription", headers=u.headers)
    assert r.status_code == 200
    assert isinstance(r.json()["cancel_at"], str)
    datetime.fromisoformat(r.json()["cancel_at"])


def test_cancel_stripe_outage_is_500_not_silent(client, make_user, fake_stripe):
    u = make_user("pro", stripe_customer_id="cus_X")
    fake_stripe.fail_subscription_list = True
    assert client.post("/billing/cancel_subscription", headers=u.headers).status_code == 500


# ---------------------------------------------------------------- portal
def test_portal_creates_missing_customer_on_the_fly(client, make_user, fake_stripe, db):
    u = make_user(email="newbie@example.com")
    r = client.post("/billing/create_portal_session", headers=u.headers, json={})
    assert r.status_code == 200
    [(_, kw)] = [c for c in fake_stripe.calls if c[0] == "Customer.create"]
    assert kw["email"] == "newbie@example.com"
    assert kw["metadata"]["app_user_id"] == str(u.id)
    db.expire_all()
    assert db.get(models.User, u.id).stripe_customer_id == "cus_1"


def test_portal_400_when_customer_cannot_be_created(client, make_user, fake_stripe, monkeypatch):
    def refuse(**kw):
        raise RuntimeError("stripe down")

    monkeypatch.setattr(fake_stripe.Customer, "create", staticmethod(refuse))
    u = make_user()
    assert client.post("/billing/create_portal_session", headers=u.headers, json={}).status_code == 400


def test_portal_default_return_url(client, make_user, fake_stripe):
    u = make_user(stripe_customer_id="cus_P")
    r = client.post("/billing/create_portal_session", headers=u.headers, json={})
    assert r.status_code == 200
    [(_, kw)] = [c for c in fake_stripe.calls if c[0] == "billing_portal.Session.create"]
    assert kw == {"customer": "cus_P", "return_url": "http://frontend.test/dashboard"}


# ------------------------------------------------------- subscription status
def test_subscription_status_shape(client, make_user, add_screenshots):
    u = make_user("pro")
    add_screenshots(u.id, 2)
    r = client.get("/subscription_status", headers=u.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "pro"
    assert body["usage"]["screenshots"] == 2
    assert body["limits"]["screenshots"] == 5000
    assert body["tier_concurrency_limit"] == 3
    assert body["next_reset_basis"] == "calendar_month"
    datetime.fromisoformat(body["next_reset"])


def test_subscription_status_uses_billing_anchor(client, make_user):
    now = datetime.utcnow()
    from datetime import timedelta
    u = make_user("pro",
                  subscription_current_period_start=now - timedelta(days=3),
                  subscription_current_period_end=now + timedelta(days=27))
    body = client.get("/subscription_status", headers=u.headers).json()
    assert body["next_reset_basis"] == "billing_cycle"


def test_subscription_state_cached_and_fresh(client, make_user, fake_stripe):
    u = make_user("pro", stripe_customer_id="cus_S")
    fake_stripe.subscriptions["cus_S"] = [
        {"id": "sub_S", "status": "active", "current_period_end": 1_800_000_000}
    ]
    a = client.get("/billing/subscription_state", headers=u.headers).json()
    assert a["has_subscription"] is True and a["cancel_at_period_end"] is False
    n = len(fake_stripe.calls)
    client.get("/billing/subscription_state", headers=u.headers)
    assert len(fake_stripe.calls) == n                     # served from cache
    client.get("/billing/subscription_state?fresh=1", headers=u.headers)
    assert len(fake_stripe.calls) == n + 1


def test_subscription_state_failure_is_not_cached(client, make_user, fake_stripe):
    u = make_user("pro", stripe_customer_id="cus_S")
    fake_stripe.fail_subscription_list = True
    assert client.get("/billing/subscription_state", headers=u.headers).json()["has_subscription"] is False
    fake_stripe.fail_subscription_list = False
    fake_stripe.subscriptions["cus_S"] = [{"id": "sub_S", "status": "active"}]
    assert client.get("/billing/subscription_state", headers=u.headers).json()["has_subscription"] is True
