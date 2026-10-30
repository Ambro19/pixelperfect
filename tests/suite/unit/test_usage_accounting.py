# tests/suite/unit/test_usage_accounting.py
# =============================================================================
# usage_accounting: the single source of truth for "how much has this user
# used this period". Enforcement (429s) and the dashboard both read it, so a
# mistake here either over-charges or gives capacity away.
# =============================================================================
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import models
import usage_accounting as ua


def _user(tier="pro", start=None, end=None, uid=1):
    return SimpleNamespace(
        id=uid, subscription_tier=tier,
        subscription_current_period_start=start,
        subscription_current_period_end=end,
    )


# ---------------------------------------------------------------- _add_months
@pytest.mark.parametrize("start, n, expected", [
    (datetime(2026, 1, 31), 1, datetime(2026, 2, 28)),   # clamps to Feb length
    (datetime(2028, 1, 31), 1, datetime(2028, 2, 29)),   # leap year
    (datetime(2026, 3, 31), 1, datetime(2026, 4, 30)),
    (datetime(2026, 12, 15), 1, datetime(2027, 1, 15)),  # year rollover
    (datetime(2026, 11, 30), 3, datetime(2027, 2, 28)),
    (datetime(2026, 5, 10, 14, 30), 0, datetime(2026, 5, 10, 14, 30)),
])
def test_add_months_clamps_like_stripe(start, n, expected):
    assert ua._add_months(start, n) == expected


def test_add_months_does_not_drift_over_a_year():
    # Twelve monthly steps from the 31st land on the 31st a year later; adding
    # 30 days at a time would not. This is the anniversary rule Stripe uses.
    start = datetime(2026, 1, 31)
    assert ua._add_months(start, 12) == datetime(2027, 1, 31)


# ------------------------------------------------------------- period start
def test_free_user_uses_calendar_month():
    now = datetime(2026, 10, 17, 9, 0)
    assert ua.current_period_start(_user("free"), now) == datetime(2026, 10, 1)
    assert ua.next_period_start(_user("free"), now) == datetime(2026, 11, 1)
    assert ua.period_basis(_user("free"), now) == "calendar_month"


def test_free_user_ignores_a_leftover_billing_anchor():
    now = datetime(2026, 10, 17)
    u = _user("free", start=datetime(2026, 10, 5), end=datetime(2026, 11, 5))
    assert ua.current_period_start(u, now) == datetime(2026, 10, 1)


def test_paid_user_inside_synced_period_uses_anchor():
    now = datetime(2026, 10, 17)
    u = _user("pro", start=datetime(2026, 10, 5, 12), end=datetime(2026, 11, 5, 12))
    assert ua.current_period_start(u, now) == datetime(2026, 10, 5, 12)
    assert ua.next_period_start(u, now) == datetime(2026, 11, 5, 12)
    assert ua.period_basis(u, now) == "billing_cycle"


def test_stale_anchor_rolls_forward_by_whole_months():
    # Missed webhooks: Stripe last told us Jul 21 -> Aug 21, it is now Oct 25.
    now = datetime(2026, 10, 25)
    u = _user("business", start=datetime(2026, 7, 21), end=datetime(2026, 8, 21))
    assert ua.current_period_start(u, now) == datetime(2026, 10, 21)
    assert ua.next_period_start(u, now) == datetime(2026, 11, 21)


def test_future_anchor_falls_back_to_calendar():
    now = datetime(2026, 10, 17)
    u = _user("pro", start=datetime(2026, 12, 1), end=datetime(2027, 1, 1))
    assert ua.current_period_start(u, now) == datetime(2026, 10, 1)
    assert ua.period_basis(u, now) == "calendar_month"


def test_paid_user_without_anchor_falls_back_to_calendar():
    now = datetime(2026, 10, 17)
    assert ua.current_period_start(_user("pro"), now) == datetime(2026, 10, 1)


def test_timezone_aware_anchor_is_normalised():
    # Webhook code writes aware datetimes; comparing aware with naive raises.
    now = datetime(2026, 10, 17)
    start = datetime(2026, 10, 5, tzinfo=timezone.utc)
    end = datetime(2026, 11, 5, tzinfo=timezone.utc)
    assert ua.current_period_start(_user("pro", start, end), now) == datetime(2026, 10, 5)


@pytest.mark.parametrize("now, expected", [
    (datetime(2026, 1, 15), datetime(2026, 2, 1)),
    (datetime(2026, 12, 31, 23, 59), datetime(2027, 1, 1)),
])
def test_first_of_next_month(now, expected):
    assert ua.first_of_next_month(now) == expected


# ------------------------------------------------------- counting (with DB)
def test_count_includes_live_and_tombstoned_rows(db, make_user, add_screenshots):
    u = make_user("free")
    ids = add_screenshots(u.id, 5)
    shot = db.get(models.Screenshot, ids[0])
    ua.record_screenshot_deletion(db, shot)
    db.delete(shot)
    db.commit()

    user = db.get(models.User, u.id)
    assert db.query(models.Screenshot).filter_by(user_id=u.id).count() == 4
    assert ua.screenshots_used_this_period(db, user) == 5


def test_count_ignores_rows_from_previous_period(db, make_user, add_screenshots):
    u = make_user("free")
    last_month = ua._calendar_period_start() - timedelta(days=3)
    add_screenshots(u.id, 7, created_at=last_month)
    add_screenshots(u.id, 2)
    assert ua.screenshots_used_this_period(db, u.id) == 2


def test_capture_exactly_at_period_start_counts(db, make_user, add_screenshots):
    # Boundary: the period is [start, next_start). A capture at 00:00:00 on
    # the 1st belongs to the new period, not to nobody.
    u = make_user("free")
    start = ua._calendar_period_start()
    add_screenshots(u.id, 1, created_at=start)
    add_screenshots(u.id, 1, created_at=start - timedelta(microseconds=1))
    assert ua.screenshots_used_this_period(db, u.id) == 1


def test_tombstone_counts_against_capture_period_not_deletion_period(
        db, make_user, add_screenshots):
    u = make_user("free")
    last_month = ua._calendar_period_start() - timedelta(days=3)
    [sid] = add_screenshots(u.id, 1, created_at=last_month)
    shot = db.get(models.Screenshot, sid)
    ua.record_screenshot_deletion(db, shot)
    db.delete(shot)
    db.commit()
    # Captured last month, deleted today: costs nothing this month.
    assert ua.screenshots_used_this_period(db, u.id) == 0


def test_counts_are_per_user(db, make_user, add_screenshots):
    a, b = make_user("free"), make_user("free")
    add_screenshots(a.id, 3)
    add_screenshots(b.id, 1)
    assert ua.screenshots_used_this_period(db, a.id) == 3
    assert ua.screenshots_used_this_period(db, b.id) == 1


def test_unknown_user_id_counts_zero_without_error(db):
    assert ua.screenshots_used_this_period(db, 999_999) == 0


def test_usage_summary_shape_and_legacy_aliases(db, make_user, add_screenshots):
    u = make_user("pro")
    add_screenshots(u.id, 3)
    db.add(models.BatchJob(id="job1", user_id=u.id, total_urls=2))
    db.commit()
    user = db.get(models.User, u.id)
    s = ua.usage_summary(db, user)
    assert s["screenshots"] == s["screenshots_used"] == 3
    assert s["batch_requests"] == s["batch_jobs"] == 1
    assert s["api_calls"] == s["api_calls_this_month"] == 4
    assert s["period_basis"] == "calendar_month"
    datetime.fromisoformat(s["period_start"])
    datetime.fromisoformat(s["next_reset"])


def test_purge_removes_only_that_users_tombstones(db, make_user, add_screenshots):
    a, b = make_user("free"), make_user("free")
    for uid in (a.id, b.id):
        [sid] = add_screenshots(uid, 1)
        shot = db.get(models.Screenshot, sid)
        ua.record_screenshot_deletion(db, shot)
        db.delete(shot)
    db.commit()
    assert ua.purge_user_deletion_records(db, a.id) == 1
    db.commit()
    assert db.query(models.ScreenshotDeletion).filter_by(user_id=b.id).count() == 1
