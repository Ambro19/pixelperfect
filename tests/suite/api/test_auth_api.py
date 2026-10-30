# tests/suite/api/test_auth_api.py
# =============================================================================
# Registration, login, token/API-key authentication, profile and password
# management, and the forgot-password / forgot-username flows.
# =============================================================================
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import jwt
import pytest

import main
import models


def _register(client, username="alice", email="alice@example.com", password="Str0ng-pass"):
    return client.post("/register", json={
        "username": username, "email": email, "password": password,
    })


# --------------------------------------------------------------- registration
def test_register_creates_user_and_returns_api_key_once(client, db):
    r = _register(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account"] == {"username": "alice", "email": "alice@example.com"}
    assert body["api_key"].startswith("pk_")
    user = db.query(models.User).filter_by(username="alice").one()
    assert user.subscription_tier == "free"
    assert user.hashed_password != "Str0ng-pass"


def test_register_normalises_email_case_and_whitespace(client, db):
    r = _register(client, username="  bob  ", email="  Bob@Example.COM ")
    assert r.status_code == 200
    user = db.query(models.User).filter_by(username="bob").one()
    assert user.email == "bob@example.com"


@pytest.mark.parametrize("dup", [
    dict(username="alice", email="other@example.com"),
    dict(username="other", email="ALICE@example.com"),
])
def test_register_rejects_duplicates(client, dup):
    assert _register(client).status_code == 200
    r = _register(client, **dup)
    assert r.status_code == 400


def test_register_missing_field_is_422(client):
    r = client.post("/register", json={"username": "x", "password": "p"})
    assert r.status_code == 422


# ---------------------------------------------------------------------- login
@pytest.mark.parametrize("path, kind", [("/token", "form"), ("/token_json", "json")])
@pytest.mark.parametrize("identifier", ["alice", "alice@example.com", "ALICE@example.com"])
def test_login_by_username_or_email(client, path, kind, identifier):
    _register(client)
    creds = {"username": identifier, "password": "Str0ng-pass"}
    r = client.post(path, data=creds) if kind == "form" else client.post(path, json=creds)
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    me = client.get("/users/me", headers={"Authorization": f"Bearer {tok}"})
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


@pytest.mark.parametrize("creds", [
    {"username": "alice", "password": "wrong"},
    {"username": "nobody", "password": "Str0ng-pass"},
    {"username": "", "password": ""},
])
def test_login_failures_are_401_with_same_message(client, creds):
    _register(client)
    r = client.post("/token_json", json=creds)
    assert r.status_code == 401
    # One message for both "no such user" and "wrong password".
    assert r.json()["detail"] == "Incorrect username/email or password"


# ------------------------------------------------------------ authentication
def test_me_requires_auth(client):
    assert client.get("/users/me").status_code == 401


def test_me_returns_only_public_fields(client, make_user):
    u = make_user("pro", stripe_customer_id="cus_secret")
    body = client.get("/users/me", headers=u.headers).json()
    assert set(body) == {"id", "username", "email", "created_at", "subscription_tier"}


def test_api_key_authenticates(client, make_user):
    u = make_user("pro")
    r = client.get("/users/me", headers=u.key_headers)
    assert r.status_code == 200
    assert r.json()["subscription_tier"] == "pro"


def test_api_key_without_bearer_prefix_also_works(client, make_user):
    u = make_user()
    assert client.get("/users/me", headers={"Authorization": u.api_key}).status_code == 200


@pytest.mark.parametrize("bad", [
    "Bearer pk_" + "0" * 64,
    "Bearer not-a-jwt",
    "Bearer " + jwt.encode({"sub": "alice"}, "wrong-secret", algorithm="HS256"),
    "Bearer " + jwt.encode({"nosub": 1}, main.SECRET_KEY, algorithm="HS256"),
    "Bearer " + jwt.encode({"sub": "ghost"}, main.SECRET_KEY, algorithm="HS256"),
])
def test_bad_credentials_are_401(client, make_user, bad):
    make_user(username="alice")
    assert client.get("/users/me", headers={"Authorization": bad}).status_code == 401


def test_expired_token_is_401(client, make_user):
    make_user(username="alice")
    tok = main.create_access_token({"sub": "alice"}, timedelta(seconds=-5))
    r = client.get("/users/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401
    assert "expired" in r.json()["detail"].lower()


def test_alg_none_token_is_rejected(client, make_user):
    make_user(username="alice")
    tok = jwt.encode({"sub": "alice"}, key=None, algorithm="none")
    assert client.get("/users/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


# --------------------------------------------------------------- API keys
def test_current_key_never_reveals_existing_key(client, make_user):
    u = make_user()
    r = client.get("/api/keys/current", headers=u.headers)
    assert r.status_code == 200
    body = r.json()
    assert "api_key" not in body
    assert body["key_prefix"] == u.api_key[:11] + "..."


def test_current_key_creates_one_when_missing(client, make_user, db):
    u = make_user()
    db.query(models.ApiKey).filter_by(user_id=u.id).delete()
    db.commit()
    r = client.get("/api/keys/current", headers=u.headers)
    assert r.status_code == 200
    assert r.json()["api_key"].startswith("pk_")


def test_regenerate_kills_old_key(client, make_user):
    u = make_user()
    r = client.post("/api/keys/regenerate", headers=u.headers)
    assert r.status_code == 200
    new_key = r.json()["api_key"]
    assert client.get("/users/me", headers=u.key_headers).status_code == 401
    assert client.get("/users/me", headers={"Authorization": f"Bearer {new_key}"}).status_code == 200


# --------------------------------------------------------- change password
def test_change_password_happy_path(client, make_user):
    u = make_user(username="carol")
    r = client.post("/user/change_password", headers=u.headers,
                    json={"current_password": u.password, "new_password": "Brand-new-9"})
    assert r.status_code == 200
    assert client.post("/token_json", json={"username": "carol", "password": u.password}).status_code == 401
    assert client.post("/token_json", json={"username": "carol", "password": "Brand-new-9"}).status_code == 200


@pytest.mark.parametrize("payload, fragment", [
    ({"current_password": "wrong", "new_password": "Brand-new-9"}, "incorrect"),
    ({"current_password": None, "new_password": "short"}, "8 characters"),
    ({"current_password": None, "new_password": None}, "different"),
])
def test_change_password_rejections(client, make_user, payload, fragment):
    u = make_user()
    payload = {k: (u.password if v is None else v) for k, v in payload.items()}
    r = client.post("/user/change_password", headers=u.headers, json=payload)
    assert r.status_code == 400
    assert fragment in r.json()["detail"].lower()


# ------------------------------------------------------------ update profile
def test_update_profile_email_lowercased(client, make_user, db):
    u = make_user()
    r = client.put("/user/update_profile", headers=u.headers, json={"email": " New@Mail.COM "})
    assert r.status_code == 200
    db.expire_all()
    assert db.get(models.User, u.id).email == "new@mail.com"


def test_update_profile_rejects_taken_values(client, make_user):
    a, b = make_user(username="a1"), make_user(username="b1")
    assert client.put("/user/update_profile", headers=a.headers,
                      json={"username": "b1"}).status_code == 400
    assert client.put("/user/update_profile", headers=a.headers,
                      json={"email": b.email.upper()}).status_code == 400


@pytest.mark.parametrize("payload", [{}, {"username": "   "}, {"email": ""}])
def test_update_profile_rejects_empty(client, make_user, payload):
    u = make_user()
    assert client.put("/user/update_profile", headers=u.headers, json=payload).status_code == 400


def test_update_profile_keeps_own_values(client, make_user):
    u = make_user(username="same")
    r = client.put("/user/update_profile", headers=u.headers,
                   json={"username": "same", "email": u.email})
    assert r.status_code == 200


# ------------------------------------------------------------ reset password
def _reset_token(link: str) -> str:
    return parse_qs(urlparse(link).query)["token"][0]


def test_forgot_and_reset_password_flow(client, make_user, sent_mail):
    bystander = make_user(username="aaron", email="aaron@example.com")   # created first
    u = make_user(username="dave", email="dave@example.com")
    r = client.post("/auth/forgot-password", json={"email": "dave@example.com"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    [mail] = sent_mail
    assert mail["to"] == "dave@example.com"
    assert mail["link"].startswith("http://frontend.test/reset?token=")

    token = _reset_token(mail["link"])
    r = client.post("/auth/reset-password", json={"token": token, "new_password": "Reset-pass-7"})
    assert r.status_code == 200
    assert client.post("/token_json", json={"username": "dave", "password": "Reset-pass-7"}).status_code == 200
    # Only Dave's password changed.
    assert client.post("/token_json", json={"username": "aaron",
                                            "password": bystander.password}).status_code == 200


def test_forgot_password_unknown_email_looks_identical(client, sent_mail):
    r = client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert sent_mail == []


def test_forgot_password_mail_failure_is_still_200(client, make_user, monkeypatch):
    make_user(email="eve@example.com")

    def boom(*a, **k):
        raise OSError("SMTP down")

    monkeypatch.setattr(main, "send_password_reset_email", boom)
    assert client.post("/auth/forgot-password", json={"email": "eve@example.com"}).status_code == 200


@pytest.mark.parametrize("token", ["garbage", ""])
def test_reset_with_bad_token(client, token):
    r = client.post("/auth/reset-password", json={"token": token, "new_password": "Whatever-1"})
    assert r.status_code == 400


def test_reset_token_signed_with_other_secret_rejected(client, make_user):
    from itsdangerous import URLSafeTimedSerializer
    make_user(email="frank@example.com")
    forged = URLSafeTimedSerializer("attacker-secret").dumps({"email": "frank@example.com"})
    r = client.post("/auth/reset-password", json={"token": forged, "new_password": "Owned-123"})
    assert r.status_code == 400


def test_reset_token_expires(client, make_user, monkeypatch):
    make_user(email="gina@example.com")
    token = main.serializer.dumps({"email": "gina@example.com"})
    monkeypatch.setattr(main, "RESET_TOKEN_TTL_SECONDS", -1)
    r = client.post("/auth/reset-password", json={"token": token, "new_password": "Late-pass-1"})
    assert r.status_code == 400
    assert "expired" in r.json()["detail"].lower()


# ------------------------------------------------------------ forgot username
def test_forgot_username_sends_reminder(client, make_user, sent_mail):
    make_user(username="hank", email="hank@example.com")
    r = client.post("/auth/forgot-username", json={"email": "HANK@example.com"})
    assert r.status_code == 200
    assert sent_mail == [{"kind": "username", "to": "hank@example.com", "username": "hank"}]


def test_forgot_username_does_not_enumerate(client, sent_mail):
    r = client.post("/auth/forgot-username", json={"email": "ghost@example.com"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert sent_mail == []
