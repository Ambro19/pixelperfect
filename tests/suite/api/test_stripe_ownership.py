# tests/suite/api/test_stripe_ownership.py
# =============================================================================
# Release K (F-15): a Stripe customer belongs to a PixelPerfect user only when
# its metadata.app_user_id says so. Email addresses are never verified, so
# anyone can register with someone else's address; matching on email alone
# could hand them a stranger's paid subscription, the billing portal on the
# stranger's card, or let them cancel it.
# =============================================================================
import models
import subscription_sync as ss
from subscription_sync import customer_belongs_to

EMAIL = "victim@example.com"


def _sub(sid="sub_V", status="active"):
    return {"id": sid, "status": status,
            "items": {"data": [{"price": {"id": "price_1",
                                          "lookup_key": "pixelperfect_pro_monthly"}}]},
            "metadata": {}, "current_period_start": 1_790_000_000,
            "current_period_end": 1_792_000_000}


def _load(db, uid):
    db.expire_all()
    return db.get(models.User, uid)


# ------------------------------------------------------------------ helper
def test_customer_belongs_to():
    assert customer_belongs_to({"metadata": {"app_user_id": "7"}}, 7)
    assert customer_belongs_to({"metadata": {"app_user_id": "7"}}, "7")
    assert not customer_belongs_to({"metadata": {"app_user_id": "8"}}, 7)
    assert not customer_belongs_to({"metadata": {}}, 7)
    assert not customer_belongs_to({}, 7)
    assert not customer_belongs_to({"metadata": {"app_user_id": "7"}}, None)


# -------------------------------------------------------- sync: email lookup
def test_sync_does_not_adopt_a_customer_stamped_for_another_user(db, make_user, fake_stripe):
    u = make_user("free", email=EMAIL)
    fake_stripe.customers = [{"id": "cus_OTHER", "email": EMAIL,
                              "metadata": {"app_user_id": str(u.id + 1000)}}]
    fake_stripe.subscriptions["cus_OTHER"] = [_sub()]
    ss.sync_user_subscription_from_stripe(_load(db, u.id), db)
    user = _load(db, u.id)
    assert user.stripe_customer_id is None and user.subscription_tier == "free"


def test_sync_picks_the_owned_customer_among_several_with_the_same_email(db, make_user, fake_stripe):
    u = make_user("free", email=EMAIL)
    fake_stripe.customers = [
        {"id": "cus_STRANGER", "email": EMAIL, "metadata": {}},
        {"id": "cus_MINE", "email": EMAIL, "metadata": {"app_user_id": str(u.id)}},
    ]
    fake_stripe.subscriptions["cus_STRANGER"] = [_sub("sub_S")]
    fake_stripe.subscriptions["cus_MINE"] = [_sub("sub_M")]
    ss.sync_user_subscription_from_stripe(_load(db, u.id), db)
    user = _load(db, u.id)
    assert user.stripe_customer_id == "cus_MINE"
    assert user.subscription_id == "sub_M"


# ---------------------------------------------- sync: duplicate-customer rescue
def test_rescue_skips_a_sibling_customer_that_is_not_ours(db, make_user, fake_stripe):
    u = make_user("free", email=EMAIL, stripe_customer_id="cus_MINE")
    fake_stripe.customers = [
        {"id": "cus_MINE", "email": EMAIL, "metadata": {"app_user_id": str(u.id)}},
        {"id": "cus_STRANGER", "email": EMAIL, "metadata": {}},
    ]
    fake_stripe.subscriptions["cus_STRANGER"] = [_sub()]
    ss.sync_user_subscription_from_stripe(_load(db, u.id), db)
    user = _load(db, u.id)
    assert user.stripe_customer_id == "cus_MINE"
    assert user.subscription_tier == "free"


# ------------------------------------------------------- cancel_subscription
def test_cancel_never_cancels_a_strangers_subscription(client, make_user, fake_stripe):
    # A paid account whose customer id was lost; a stranger's customer shares
    # the email. Cancelling must not touch the stranger's subscription.
    u = make_user("pro", email=EMAIL)
    fake_stripe.customers = [{"id": "cus_STRANGER", "email": EMAIL, "metadata": {}}]
    fake_stripe.subscriptions["cus_STRANGER"] = [_sub("sub_STRANGER")]
    r = client.post("/billing/cancel_subscription", headers=u.headers)
    assert r.status_code == 400
    assert fake_stripe.subscriptions["cus_STRANGER"][0]["status"] == "active"
    assert not [c for c in fake_stripe.calls
                if c[0] in ("Subscription.modify", "Subscription.delete")]
