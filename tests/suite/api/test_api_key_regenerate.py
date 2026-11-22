# tests/suite/api/test_api_key_regenerate.py
# =============================================================================
# Release K (F-26): "Regenerate Key" must leave exactly one working key.
#
# Found in production (Nov 21, 2026): after regenerating, an older key still
# returned 200 on /users/me. The account had two active keys, and regenerate
# switched off only the first one the database returned.
# =============================================================================
from datetime import datetime, timedelta

import models
from api_key_system import create_api_key_for_user


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


def _add_key(db, user_id, created_at):
    key, rec = create_api_key_for_user(db=db, user_id=user_id, name="Default API Key")
    rec.created_at = created_at
    db.commit()
    return key


def test_regenerate_switches_off_every_older_key(client, make_user, db):
    u = make_user("free")
    first = u.key_headers["Authorization"].split()[-1]
    second = _add_key(db, u.id, datetime.utcnow() + timedelta(minutes=1))
    assert client.get("/users/me", headers=_bearer(first)).status_code == 200
    assert client.get("/users/me", headers=_bearer(second)).status_code == 200

    r = client.post("/api/keys/regenerate", headers=u.headers)
    assert r.status_code == 200, r.text
    new = r.json()["api_key"]

    assert client.get("/users/me", headers=_bearer(first)).status_code == 401
    assert client.get("/users/me", headers=_bearer(second)).status_code == 401
    assert client.get("/users/me", headers=_bearer(new)).status_code == 200
    db.expire_all()
    active = db.query(models.ApiKey).filter_by(user_id=u.id, is_active=True).count()
    assert active == 1


def test_regenerate_does_not_touch_other_users_keys(client, make_user):
    me, other = make_user("free"), make_user("free")
    assert client.post("/api/keys/regenerate", headers=me.headers).status_code == 200
    assert client.get("/users/me", headers=other.key_headers).status_code == 200


def test_dashboard_shows_the_newest_active_key(client, make_user, db):
    u = make_user("free")
    # A second active key, added after the first and dated later: the row
    # the database returns first is the OLDER key, so only real ordering
    # picks the right one.
    _add_key(db, u.id, datetime.utcnow() + timedelta(days=1))
    newest = db.query(models.ApiKey).filter_by(user_id=u.id, is_active=True) \
        .order_by(models.ApiKey.created_at.desc()).first()
    r = client.get("/api/keys/current", headers=u.headers)
    assert r.status_code == 200
    assert r.json()["key_prefix"] == newest.key_prefix
