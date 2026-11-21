# tests/suite/api/test_session_only.py
# =============================================================================
# Release J (F-17): an API key is for screenshots, not for running the account.
#
# A leaked API key (in a script, a CI log, a public repo) used to be enough to
# change the account email, then reset the password and take the account over,
# or simply delete it. Account and billing endpoints now need the token the
# dashboard gets at sign-in; an API key gets 403 and changes nothing.
# =============================================================================
import pytest

import models
from auth_deps import SESSION_REQUIRED_MESSAGE, is_api_key_credential

URL = "https://example.com"

# (method, path, json body) for every account / billing action.
SESSION_ONLY = [
    ("post",   "/user/change_password", {"current_password": "x", "new_password": "Brand-new-9"}),
    ("put",    "/user/update_profile",  {"email": "thief@example.com"}),
    ("delete", "/user/delete_account",  None),
    ("get",    "/api/keys/current",     None),
    ("post",   "/api/keys/regenerate",  None),
    ("post",   "/billing/create_checkout_session", {"plan": "pro"}),
    ("post",   "/billing/create_portal_session",   {}),
    ("post",   "/billing/cancel_subscription",     None),
]


def _call(client, method, path, headers, body):
    fn = getattr(client, method)
    return fn(path, headers=headers, json=body) if body is not None else fn(path, headers=headers)


@pytest.mark.parametrize("method, path, body", SESSION_ONLY, ids=[p for _, p, _ in SESSION_ONLY])
def test_api_key_is_refused_on_account_endpoints(client, make_user, method, path, body):
    u = make_user("pro", stripe_customer_id="cus_F17")
    r = _call(client, method, path, u.key_headers, body)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == SESSION_REQUIRED_MESSAGE


@pytest.mark.parametrize("header", ["Bearer {k}", "bearer {k}", "{k}", "  Bearer   {k}  "])
def test_every_way_of_sending_the_key_is_refused(client, make_user, header):
    u = make_user("pro")
    key = u.key_headers["Authorization"].split()[-1]
    r = client.delete("/user/delete_account", headers={"Authorization": header.format(k=key)})
    assert r.status_code == 403


def test_refused_key_changes_nothing(client, make_user, db):
    u = make_user("pro", email="owner@example.com")
    client.put("/user/update_profile", headers=u.key_headers, json={"email": "thief@example.com"})
    client.delete("/user/delete_account", headers=u.key_headers)
    client.post("/api/keys/regenerate", headers=u.key_headers)
    db.expire_all()
    user = db.get(models.User, u.id)
    assert user is not None and user.email == "owner@example.com"
    # The key was not rotated by the refused call, so it still works.
    assert client.get("/users/me", headers=u.key_headers).status_code == 200


def test_signed_in_session_still_manages_the_account(client, make_user, db):
    u = make_user("free", email="me@example.com")
    r = client.put("/user/update_profile", headers=u.headers, json={"email": "me2@example.com"})
    assert r.status_code == 200, r.text
    assert client.post("/api/keys/regenerate", headers=u.headers).status_code == 200
    assert client.delete("/user/delete_account", headers=u.headers).status_code == 200


def test_api_key_still_works_for_screenshots_and_reads(client, make_user):
    u = make_user("pro")
    assert client.post("/api/v1/screenshot", headers=u.key_headers, json={"url": URL}).status_code == 200
    assert client.get("/users/me", headers=u.key_headers).status_code == 200
    assert client.get("/subscription_status", headers=u.key_headers).status_code == 200


@pytest.mark.parametrize("value, expected", [
    ("Bearer pk_abc", True), ("pk_abc", True), ("bearer pk_abc", True),
    ("Bearer eyJhbGciOi.x.y", False), ("", False), (None, False), ("Bearer", False),
])
def test_is_api_key_credential(value, expected):
    assert is_api_key_credential(value) is expected
