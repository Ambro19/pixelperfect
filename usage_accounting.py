# ============================================================================
# USAGE ACCOUNTING - PixelPerfect Screenshot API
# File: backend/usage_accounting.py
# Author: OneTechly
# Created: August 2026
# ============================================================================
# SINGLE SOURCE OF TRUTH for "how much has this user consumed this period".
#
# THE BUG THIS FIXES
# ------------------
# /subscription_status counted live rows in the screenshots table. Deleting a
# screenshot from the History page refunded the user's quota:
#
#     Screenshots Used: 8 -> delete -> 7 -> delete -> 6
#     API Calls:        8 -> delete -> 7 -> delete -> 6
#
# Both counters moved because api_calls = screenshots + batch, all derived
# from the same query. A user could capture their full monthly allowance,
# delete everything, and capture it again — indefinitely, on any tier.
#
# THE RULE
# --------
# Usage measures CONSUMPTION, not RETENTION. Once Playwright has launched
# and Chromium has rendered the page, the cost is spent. Removing the
# artifact afterwards does not un-spend it. Note that the screenshots which
# triggered this report were already marked "Image expired" — R2 had
# lifecycle-deleted the file days earlier, so the capture cost was fully
# incurred and the artifact was already gone.
#
# HOW IT WORKS
# ------------
#     period usage = live screenshots captured in period
#                  + tombstoned screenshots captured in period
#
# The tombstone (models.ScreenshotDeletion) stores original_created_at — the
# CAPTURE time — so a screenshot taken in August and deleted in September
# still counts against August and against nothing in September.
#
# IMPORTANT — USE THIS MODULE EVERYWHERE
# --------------------------------------
# If quota is ALSO enforced somewhere else with its own COUNT(*) query
# (check routers/screenshot.py and batch.py), that call site must be changed
# to use screenshots_used_this_period() too. Fixing only the dashboard
# display while leaving the enforcement path counting live rows would be
# WORSE than the current state: the bypass would still work, but would no
# longer be visible in the UI.
# ============================================================================

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Any

from sqlalchemy.orm import Session

from models import Screenshot, ScreenshotDeletion, User

logger = logging.getLogger("pixelperfect.usage")


# ---------------------------------------------------------------------------
# Period boundaries
# ---------------------------------------------------------------------------

def current_period_start(now: datetime | None = None) -> datetime:
    """
    First moment of the current usage period (calendar month, UTC, naive).

    NOTE: this is the CALENDAR month, matching the existing behaviour of
    /subscription_status and the "Usage resets on <1st of next month>" label
    on the dashboard. It is deliberately NOT the Stripe billing anniversary.
    A user who subscribes on the 20th gets a usage reset on the 1st, which is
    generous rather than harmful, but it is a known divergence — see
    RESET_LOGIC.md. Changing it is a separate piece of work; do not change it
    here without also changing the dashboard copy.
    """
    now = now or datetime.utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def first_of_next_month(now: datetime | None = None) -> datetime:
    """First day of next calendar month, 00:00 UTC (naive, matches DB)."""
    now = now or datetime.utcnow()
    if now.month == 12:
        return datetime(now.year + 1, 1, 1)
    return datetime(now.year, now.month + 1, 1)


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

def screenshots_used_this_period(
    db: Session,
    user_id: int,
    period_start: datetime | None = None,
) -> int:
    """
    Screenshots CONSUMED in the current period.

    live rows captured in period + tombstoned rows captured in period.
    Deleting a screenshot does not reduce this number.
    """
    period_start = period_start or current_period_start()

    live = (
        db.query(Screenshot)
        .filter(
            Screenshot.user_id == user_id,
            Screenshot.created_at >= period_start,
        )
        .count()
    )

    # Tombstones are filtered on original_created_at (capture time), so
    # usage lands in the period the screenshot was actually taken in.
    deleted = (
        db.query(ScreenshotDeletion)
        .filter(
            ScreenshotDeletion.user_id == user_id,
            ScreenshotDeletion.original_created_at >= period_start,
        )
        .count()
    )

    return live + deleted


def batch_used_this_period(
    db: Session,
    user: User,
    period_start: datetime | None = None,
) -> int:
    """
    Batch jobs submitted in the current period.

    Batch jobs are not user-deletable today, so a live count is still
    correct. If you ever add batch deletion, give BatchJob the same
    tombstone treatment rather than counting live rows.
    """
    period_start = period_start or current_period_start()
    try:
        from models import BatchJob
        return (
            db.query(BatchJob)
            .filter(
                BatchJob.user_id == user.id,
                BatchJob.created_at >= period_start,
            )
            .count()
        )
    except Exception:
        return int(getattr(user, "usage_batch_requests", 0) or 0)


def usage_summary(db: Session, user: User) -> Dict[str, Any]:
    """
    Full usage block for /subscription_status.

    Returns both the canonical field names and the legacy aliases the
    frontend falls back through (see DashboardPage.js), so no frontend
    change is needed.
    """
    period_start = current_period_start()

    screenshots = screenshots_used_this_period(db, user.id, period_start)
    batch       = batch_used_this_period(db, user, period_start)
    api_calls   = screenshots + batch

    return {
        "screenshots":           screenshots,
        "batch_requests":        batch,
        "api_calls":             api_calls,
        # Legacy aliases — DashboardPage.js reads through several names.
        "screenshots_used":      screenshots,
        "batch_jobs":            batch,
        "api_calls_this_month":  api_calls,
    }


# ---------------------------------------------------------------------------
# Tombstone writer
# ---------------------------------------------------------------------------

def record_screenshot_deletion(db: Session, screenshot: Screenshot) -> None:
    """
    Write the usage tombstone for a screenshot that is about to be deleted.

    MUST be called BEFORE db.delete(screenshot) — it reads created_at off the
    row. Does NOT commit; the caller's existing commit persists both the
    tombstone insert and the row delete in one transaction, so they can never
    diverge.

    Failure here is deliberately non-fatal: a user must always be able to
    delete their own data (GDPR erasure). If the tombstone write fails we log
    loudly and let the deletion proceed — under-counting one screenshot is a
    far smaller problem than a delete button that refuses to work.
    """
    try:
        tombstone = ScreenshotDeletion(
            user_id=screenshot.user_id,
            screenshot_id=str(screenshot.id),
            original_created_at=getattr(screenshot, "created_at", None) or datetime.utcnow(),
            deleted_at=datetime.utcnow(),
            url=getattr(screenshot, "url", None),
            format=getattr(screenshot, "format", None),
            from_batch=bool(getattr(screenshot, "batch_job_id", None)),
        )
        db.add(tombstone)
    except Exception:
        logger.exception(
            "⚠️ Failed to record usage tombstone for screenshot %s (user %s). "
            "Deletion will proceed; this period's usage may under-count by 1.",
            getattr(screenshot, "id", "?"),
            getattr(screenshot, "user_id", "?"),
        )


def purge_user_deletion_records(db: Session, user_id: int) -> int:
    """
    Remove all tombstones for a user. Call ONLY from account deletion —
    at that point there is no quota left to protect, and leaving orphaned
    rows behind would violate the erasure promise in the privacy policy.
    """
    try:
        n = (
            db.query(ScreenshotDeletion)
            .filter(ScreenshotDeletion.user_id == user_id)
            .delete(synchronize_session=False)
        )
        return int(n or 0)
    except Exception:
        logger.exception("Failed to purge deletion records for user %s", user_id)
        return 0