# tests/suite/api/test_checkout_pause.py
# =============================================================================
# Release F: PAID_CHECKOUT_PAUSED=true stops every new paid subscription at
# the server, whatever page or script asks for it. Free accounts keep working.
# =============================================================================
import pytest

import main


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_paused_checkout_is_refused_before_stripe_is_touched(client, make_user, fake_stripe,
                                                              monkeypatch, value):
    monkeypatch.setenv("PAID_CHECKOUT_PAUSED", value)
    called = []
    monkeypatch.setattr(main, "ensure_stripe_customer_for_user",
                        lambda *a, **k: called.append(1))
    u = make_user("free")
    for plan in ("pro", "business", "premium"):
        r = client.post("/billing/create_checkout_session", headers=u.headers,
                        json={"plan": plan, "billing_cycle": "monthly"})
        assert r.status_code == 503
        assert "paused" in r.json()["detail"]
        assert "support@pixelperfectapi.net" in r.json()["detail"]
    assert called == []                       # no Stripe customer, no session


@pytest.mark.parametrize("value", [None, "", "false", "0", "no"])
def test_checkout_works_when_not_paused(client, make_user, fake_stripe, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PAID_CHECKOUT_PAUSED", raising=False)
    else:
        monkeypatch.setenv("PAID_CHECKOUT_PAUSED", value)
    monkeypatch.setenv("STRIPE_PRO_LOOKUP_KEY_MONTHLY", "pp_pro_monthly")
    fake_stripe.prices["pp_pro_monthly"] = {"id": "price_monthly"}
    u = make_user(stripe_customer_id="cus_C")
    r = client.post("/billing/create_checkout_session", headers=u.headers,
                    json={"plan": "pro", "billing_cycle": "monthly"})
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://checkout.stripe.test/")


def test_pause_still_requires_sign_in(client, monkeypatch):
    monkeypatch.setenv("PAID_CHECKOUT_PAUSED", "true")
    r = client.post("/billing/create_checkout_session", json={"plan": "pro"})
    assert r.status_code == 401


def test_free_captures_keep_working_while_paused(client, make_user, monkeypatch):
    monkeypatch.setenv("PAID_CHECKOUT_PAUSED", "true")
    u = make_user("free")
    assert client.get("/users/me", headers=u.headers).status_code == 200
