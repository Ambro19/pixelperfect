# tests/suite/unit/test_tier_policy.py
# =============================================================================
# Tier policy lives in several files (models.py, routers/screenshot.py,
# batch.py, main.py). Several past bugs were one file disagreeing with another,
# so these tests pin the values AND check that the copies agree.
# =============================================================================
from types import SimpleNamespace

import pytest

import batch
import main
import models
import routers.screenshot as rs

TIERS = ("free", "pro", "business", "premium")


# --------------------------------------------------------- documented limits
@pytest.mark.parametrize("tier, screenshots, batch_requests", [
    ("free", 100, 0),
    ("pro", 5_000, 50),
    ("business", 50_000, 200),
    ("premium", "unlimited", "unlimited"),
])
def test_default_tier_limits(tier, screenshots, batch_requests):
    limits = models.get_tier_limits(tier)
    assert limits["screenshots"] == screenshots
    assert limits["batch_requests"] == batch_requests


def test_unknown_tier_gets_free_limits():
    assert models.get_tier_limits("gold") == models.get_tier_limits("free")
    assert models.get_tier_limits(None) == models.get_tier_limits("free")


def test_tier_lookup_is_case_insensitive():
    assert models.get_tier_limits("PRO") == models.get_tier_limits("pro")


def test_env_overrides_limit(monkeypatch):
    monkeypatch.setenv("PRO_SCREENSHOTS_LIMIT", "7")
    assert models.get_tier_limits("pro")["screenshots"] == 7


def test_bad_env_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("PRO_SCREENSHOTS_LIMIT", "lots")
    assert models.get_tier_limits("pro")["screenshots"] == 5_000


# ------------------------------------------------------------- feature matrix
EXPECTED_FEATURES = {
    #                  custom_js device element webhooks white_label pdf
    "free":     dict(custom_js=False, device_emulation=False, element_selection=False,
                     webhooks=False, white_label=False, pdf=False),
    "pro":      dict(custom_js=True, device_emulation=True, element_selection=False,
                     webhooks=False, white_label=False, pdf=True),
    "business": dict(custom_js=True, device_emulation=True, element_selection=True,
                     webhooks=True, white_label=False, pdf=True),
    "premium":  dict(custom_js=True, device_emulation=True, element_selection=True,
                     webhooks=True, white_label=True, pdf=True),
}


@pytest.mark.parametrize("tier", TIERS)
def test_feature_matrix(tier):
    user = SimpleNamespace(subscription_tier=tier)
    for feature, expected in EXPECTED_FEATURES[tier].items():
        assert models.has_feature(user, feature) is expected, (tier, feature)


def test_has_feature_unknown_tier_and_feature_are_false():
    assert models.has_feature(SimpleNamespace(subscription_tier="gold"), "pdf") is False
    assert models.has_feature(SimpleNamespace(subscription_tier="premium"), "teleport") is False
    assert models.has_feature(SimpleNamespace(subscription_tier=None), "pdf") is False


# --------------------------------------------------- copies that must agree
def test_max_width_policy_matches_between_single_and_batch():
    assert rs.TIER_MAX_WIDTH == batch.TIER_MAX_WIDTH
    assert rs.DEFAULT_MAX_WIDTH == batch.DEFAULT_MAX_WIDTH


def test_batch_size_limits_agree_with_tier_limits_where_both_exist():
    # Per-batch URL caps in batch.py versus per-period batch counts in
    # models.py. They mean different things, but Free must be 0 in both.
    assert batch.TIER_BATCH_LIMITS["free"] == 0
    assert models.get_tier_limits("free")["batch_requests"] == 0


@pytest.mark.parametrize("raw, normalized", [
    ("starter", "starter"), ("basic", "starter"), ("free", "starter"), ("", "starter"),
    (None, "starter"), ("PRO", "pro"), ("premium", "pro"), ("enterprise", "business"),
    ("business", "business"),
])
def test_concurrency_tier_normalisation(raw, normalized):
    assert main._normalize_tier(raw) == normalized


@pytest.mark.parametrize("tier, limit", [
    ("free", 2), ("pro", 3), ("business", 5),
])
def test_concurrency_limit_for_user(tier, limit):
    assert main._tier_limit_for_user(SimpleNamespace(subscription_tier=tier)) == limit


# ------------------------------------------------- unlimited-safe arithmetic
@pytest.mark.parametrize("limit, expected", [
    ("unlimited", True), (None, True), (float("inf"), True), (100, False), (0, False),
])
def test_is_unlimited(limit, expected):
    assert rs.is_unlimited(limit) is expected


@pytest.mark.parametrize("limit, used, expected", [
    (100, 30, 70), (100, 150, 0), ("unlimited", 10, "unlimited"), ("junk", 1, "unlimited"),
])
def test_remaining_for(limit, used, expected):
    assert rs.remaining_for(limit, used) == expected


def test_check_limit_blocks_at_exactly_the_limit(db, make_user, add_screenshots, monkeypatch):
    monkeypatch.setenv("FREE_SCREENSHOTS_LIMIT", "3")
    u = make_user("free")
    user = db.get(models.User, u.id)
    add_screenshots(u.id, 2)
    assert rs.check_user_screenshot_limit(user, db) == (True, 2, 3)
    add_screenshots(u.id, 1)
    assert rs.check_user_screenshot_limit(user, db) == (False, 3, 3)


def test_check_limit_premium_never_blocks(db, make_user, add_screenshots):
    u = make_user("premium")
    add_screenshots(u.id, 5)
    allowed, current, limit = rs.check_user_screenshot_limit(db.get(models.User, u.id), db)
    assert (allowed, current, limit) == (True, 5, "unlimited")
