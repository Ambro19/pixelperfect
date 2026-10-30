# tests/suite/unit/test_auth_primitives.py
# =============================================================================
# Password hashing and API-key primitives. Small functions, but every login
# and every API call depends on them.
# =============================================================================
import hashlib
import re
from datetime import timedelta

import jwt
import pytest

import api_key_system as aks
import main
import models
from auth_utils import get_password_hash, verify_password


# ------------------------------------------------------------------ passwords
def test_hash_is_salted_and_verifies():
    h1, h2 = get_password_hash("hunter2-long"), get_password_hash("hunter2-long")
    assert h1 != h2
    assert h1.startswith("$2")          # bcrypt
    assert verify_password("hunter2-long", h1)
    assert not verify_password("hunter2-lonG", h1)


def test_password_longer_than_bcrypt_limit_is_not_truncated():
    # bcrypt silently ignores bytes past 72. auth_utils pre-hashes long input,
    # so two long passwords sharing their first 72 bytes must NOT both verify.
    base = "a" * 72
    h = get_password_hash(base + "X")
    assert verify_password(base + "X", h)
    assert not verify_password(base + "Y", h)


def test_multibyte_password_round_trips():
    pw = "pässwörd-ключ-密码-🔑" * 4   # > 72 bytes once encoded
    assert verify_password(pw, get_password_hash(pw))


# ------------------------------------------------------------------- API keys
def test_api_key_format():
    key = aks.generate_api_key()
    # 32 random bytes as hex, after the pk_ prefix.
    assert re.fullmatch(r"pk_[0-9a-f]{64}", key)


def test_api_keys_are_unique():
    assert len({aks.generate_api_key() for _ in range(200)}) == 200


def test_key_is_stored_hashed_not_plain(db, make_user):
    u = make_user()
    rows = db.query(models.ApiKey).filter_by(user_id=u.id).all()
    assert len(rows) == 1
    assert rows[0].key_hash == hashlib.sha256(u.api_key.encode()).hexdigest()
    assert u.api_key not in rows[0].key_prefix   # only a prefix is kept
    assert rows[0].key_prefix == u.api_key[:11] + "..."


def test_validate_key_records_last_use(db, make_user):
    u = make_user()
    user = aks.validate_api_key(db, u.api_key)
    assert user.id == u.id
    row = db.query(models.ApiKey).filter_by(user_id=u.id).one()
    assert row.last_used_at is not None


def test_validate_rejects_unknown_and_inactive_keys(db, make_user):
    u = make_user()
    assert aks.validate_api_key(db, "pk_" + "0" * 64) is None
    row = db.query(models.ApiKey).filter_by(user_id=u.id).one()
    row.is_active = False
    db.commit()
    assert aks.validate_api_key(db, u.api_key) is None


def test_regenerate_deactivates_old_key(db, make_user):
    u = make_user()
    new_key, _ = aks.regenerate_api_key(db, u.id)
    assert new_key != u.api_key
    assert aks.validate_api_key(db, u.api_key) is None
    assert aks.validate_api_key(db, new_key).id == u.id


def test_revoke_only_touches_own_keys(db, make_user):
    a, b = make_user(), make_user()
    b_key_id = db.query(models.ApiKey).filter_by(user_id=b.id).one().id
    assert aks.revoke_api_key(db, a.id, b_key_id) is False
    assert aks.validate_api_key(db, b.api_key) is not None


# ------------------------------------------------------------------------ JWT
def test_access_token_carries_subject_and_expiry():
    tok = main.create_access_token({"sub": "alice"}, timedelta(minutes=5))
    payload = jwt.decode(tok, main.SECRET_KEY, algorithms=[main.ALGORITHM])
    assert payload["sub"] == "alice"
    assert "exp" in payload


def test_access_token_default_expiry_is_short():
    tok = main.create_access_token({"sub": "alice"})
    payload = jwt.decode(tok, main.SECRET_KEY, algorithms=[main.ALGORITHM])
    import time
    assert payload["exp"] - time.time() <= 15 * 60 + 5
