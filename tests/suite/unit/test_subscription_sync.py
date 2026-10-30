# tests/suite/unit/test_subscription_sync.py
# =============================================================================
# subscription_sync decides what a customer has paid for. The rule that
# matters most: only an AFFIRMATIVE answer from Stripe may change a tier.
# An outage, a timeout or a missing price must leave a paying customer alone.
# =============================================================================
from datetime import datetime, timedelta

import pytest

import models
import subscription_sync as ss

NOW_TS = int(datetime(2026, 10, 10).timestamp())
END_TS = int(datetime(2026, 11, 10).timestamp())


def _sub(sid="sub_1", status="active", lookup_key="pixelperfect_pro_monthly",
         nickname=None, price_id="price_abc", product=None, metadata=None,
         item_level_period=False):
    price = {"id": price_id, "lookup_key": lookup_key, "nickname": nickname,
             "product": product}
    item = {"price": price}
    sub = {"id": sid, "status": status, "items": {"data": [item]},
           "metadata": metadata or {}}
    if item_level_period:            # newer Stripe API versions
        item.update(current_period_start=NOW_TS, current_period_end=END_TS)
    else:
        sub.update(current_period_start=NOW_TS, current_period_end=END_TS)
    return sub


def _load(db, uid):
    db.expire_all()
    return db.get(models.User, uid)


# ------------------------------------------------------------ tier from text
@pytest.mark.parametrize("text, tier", [
    ("pixelperfect_pro_monthly", "pro"),
    ("pixelperfect_business_yearly", "business"),
    ("PixelPerfect Premium", "premium"),
    ("BUSINESS", "business"),
    ("", None), (None, None), ("starter", None),
])
def test_tier_from_text(text, tier):
    assert ss._tier_from_text(text) == tier


def test_business_and_premium_win_over_pro_substring():
    # "premium" and "business" are checked before "pro" so that a name
    # containing both cannot be mapped to the cheaper tier.
    assert ss._tier_from_text("business pro bundle") == "business"
    assert ss._tier_from_text("premium (pro features)") == "premium"


# --------------------------------------------------- 5-layer tier resolution
def test_resolution_prefers_lookup_key():
    sub = _sub(lookup_key="pixelperfect_business_monthly", nickname="Pro")
    assert ss._resolve_tier_for_subscription(sub) == "business"


def test_resolution_falls_through_to_nickname_then_metadata():
    assert ss._resolve_tier_for_subscription(
        _sub(lookup_key=None, nickname="Premium plan", price_id="price_x")) == "premium"
    assert ss._resolve_tier_for_subscription(
        _sub(lookup_key=None, price_id="price_x", metadata={"plan": "business"})) == "business"


def test_resolution_uses_product_name_as_last_resort(fake_stripe):
    fake_stripe.products["prod_1"] = {"id": "prod_1", "name": "PixelPerfect Business"}
    sub = _sub(lookup_key=None, price_id="price_x", product="prod_1")
    assert ss._resolve_tier_for_subscription(sub) == "business"
    assert ("Product.retrieve", "prod_1") in fake_stripe.calls


def test_resolution_returns_none_when_nothing_matches(fake_stripe):
    sub = _sub(lookup_key=None, price_id="price_x", product="prod_404")
    assert ss._resolve_tier_for_subscription(sub) is None


# ------------------------------------------------------------- period fields
def test_period_read_from_item_level_on_new_api_versions():
    sub = _sub(item_level_period=True)
    assert ss._period_start_ts(sub) == NOW_TS
    assert ss._period_end_ts(sub) == END_TS


def test_ts_conversion_is_naive_utc():
    dt = ss._ts_to_naive_utc(1_790_000_000)
    assert dt.tzinfo is None
    assert dt == datetime(2026, 9, 21, 14, 13, 20)          # the UTC wall-clock value
    assert ss._ts_to_naive_utc(None) is None
    assert ss._ts_to_naive_utc("garbage") is None


# ------------------------------------------------------------- full sync
def test_sync_upgrades_and_persists_billing_period(db, make_user, fake_stripe):
    u = make_user("free", stripe_customer_id="cus_A")
    fake_stripe.subscriptions["cus_A"] = [_sub(lookup_key="pixelperfect_business_monthly")]

    ss.sync_user_subscription_from_stripe(_load(db, u.id), db)

    user = _load(db, u.id)
    assert user.subscription_tier == "business"
    assert user.subscription_id == "sub_1"
    assert user.stripe_subscription_status == "active"
    assert user.subscription_current_period_start == ss._ts_to_naive_utc(NOW_TS)
    assert user.subscription_current_period_end == ss._ts_to_naive_utc(END_TS)
    assert user.subscription_verified_at is not None


def test_sync_recovers_missing_customer_id_by_email(db, make_user, fake_stripe):
    u = make_user("free", email="buyer@example.com")
    fake_stripe.customers = [{"id": "cus_B", "email": "buyer@example.com",
                              "metadata": {"app_user_id": str(u.id)}}]
    fake_stripe.subscriptions["cus_B"] = [_sub()]

    ss.sync_user_subscription_from_stripe(_load(db, u.id), db)

    user = _load(db, u.id)
    assert user.stripe_customer_id == "cus_B"
    assert user.subscription_tier == "pro"


def test_sync_corrects_customer_mismatch(db, make_user, fake_stripe):
    # DB says cus_OLD; the paid subscription lives on a sibling customer.
    u = make_user("free", email="dup@example.com", stripe_customer_id="cus_OLD")
    fake_stripe.customers = [
        {"id": "cus_OLD", "email": "dup@example.com", "metadata": {"app_user_id": str(u.id)}},
        {"id": "cus_NEW", "email": "dup@example.com", "metadata": {"app_user_id": str(u.id)}},
    ]
    fake_stripe.subscriptions["cus_NEW"] = [_sub()]

    ss.sync_user_subscription_from_stripe(_load(db, u.id), db)

    user = _load(db, u.id)
    assert user.stripe_customer_id == "cus_NEW"
    assert user.subscription_tier == "pro"


def test_stripe_outage_never_downgrades(db, make_user, fake_stripe):
    u = make_user("pro", stripe_customer_id="cus_A", subscription_id="sub_1",
                  stripe_subscription_status="active")
    fake_stripe.fail_subscription_list = True

    ss.sync_user_subscription_from_stripe(_load(db, u.id), db)

    assert _load(db, u.id).subscription_tier == "pro"


def test_failed_sibling_search_also_blocks_downgrade(db, make_user, fake_stripe):
    u = make_user("pro", stripe_customer_id="cus_A", subscription_id="sub_1",
                  stripe_subscription_status="active")
    fake_stripe.subscriptions["cus_A"] = []
    fake_stripe.fail_customer_list = True

    ss.sync_user_subscription_from_stripe(_load(db, u.id), db)

    assert _load(db, u.id).subscription_tier == "pro"


def test_affirmative_no_subscription_downgrades_and_clears_anchor(db, make_user, fake_stripe):
    u = make_user(
        "pro", stripe_customer_id="cus_A", subscription_id="sub_1",
        stripe_subscription_status="active",
        subscription_current_period_start=datetime(2026, 9, 1),
        subscription_current_period_end=datetime(2026, 10, 1),
    )
    fake_stripe.subscriptions["cus_A"] = []

    ss.sync_user_subscription_from_stripe(_load(db, u.id), db)

    user = _load(db, u.id)
    assert user.subscription_tier == "free"
    assert user.stripe_subscription_status == "canceled"
    assert user.subscription_current_period_start is None
    assert user.subscription_current_period_end is None


def test_manually_granted_tier_without_stripe_state_is_left_alone(db, make_user, fake_stripe):
    # A tier set by hand (no subscription id, no Stripe status) is not
    # Stripe's to take away.
    u = make_user("business", stripe_customer_id="cus_A")
    fake_stripe.subscriptions["cus_A"] = []

    ss.sync_user_subscription_from_stripe(_load(db, u.id), db)

    assert _load(db, u.id).subscription_tier == "business"


def test_unresolvable_tier_leaves_tier_unchanged(db, make_user, fake_stripe):
    u = make_user("pro", stripe_customer_id="cus_A")
    fake_stripe.subscriptions["cus_A"] = [
        _sub(lookup_key=None, nickname=None, price_id="price_x", product=None)
    ]

    ss.sync_user_subscription_from_stripe(_load(db, u.id), db)

    assert _load(db, u.id).subscription_tier == "pro"


def test_trialing_counts_as_entitled(db, make_user, fake_stripe):
    u = make_user("free", stripe_customer_id="cus_A")
    fake_stripe.subscriptions["cus_A"] = [_sub(status="trialing")]

    ss.sync_user_subscription_from_stripe(_load(db, u.id), db)

    assert _load(db, u.id).subscription_tier == "pro"


def test_sync_without_stripe_key_is_a_no_op(db, make_user, monkeypatch):
    monkeypatch.setattr(ss, "stripe", None)
    u = make_user("pro")
    ss.sync_user_subscription_from_stripe(_load(db, u.id), db)
    assert _load(db, u.id).subscription_tier == "pro"


# ---------------------------------------------------- overdue local downgrade
def test_overdue_with_lapsed_status_downgrades_locally(db, make_user, fake_stripe):
    u = make_user("pro", stripe_subscription_status="past_due",
                  subscription_expires_at=datetime.utcnow() - timedelta(days=ss.GRACE_DAYS + 1))
    ss._apply_local_overdue_downgrade_if_possible(_load(db, u.id), db)
    assert _load(db, u.id).subscription_tier == "free"
    assert not [c for c in fake_stripe.calls if c[0].startswith("Subscription")]


def test_within_grace_period_is_left_alone(db, make_user, fake_stripe):
    u = make_user("pro", stripe_subscription_status="past_due",
                  subscription_expires_at=datetime.utcnow() - timedelta(days=1))
    ss._apply_local_overdue_downgrade_if_possible(_load(db, u.id), db)
    assert _load(db, u.id).subscription_tier == "pro"


def test_overdue_but_active_status_reverifies_once_then_throttles(db, make_user, fake_stripe):
    u = make_user("pro", stripe_customer_id="cus_A", subscription_id="sub_1",
                  stripe_subscription_status="active",
                  subscription_expires_at=datetime.utcnow() - timedelta(days=30))
    fake_stripe.fail_subscription_list = True   # Stripe is down, too

    ss._apply_local_overdue_downgrade_if_possible(_load(db, u.id), db)
    first = len(fake_stripe.calls)
    assert first > 0
    ss._apply_local_overdue_downgrade_if_possible(_load(db, u.id), db)
    assert len(fake_stripe.calls) == first      # throttled: no second storm
    assert _load(db, u.id).subscription_tier == "pro"


def test_free_user_is_never_touched(db, make_user, fake_stripe):
    u = make_user("free", subscription_expires_at=datetime(2020, 1, 1))
    ss._apply_local_overdue_downgrade_if_possible(_load(db, u.id), db)
    assert fake_stripe.calls == []
