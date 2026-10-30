#!/usr/bin/env python
# tests/mutation/run_mutations.py
# =============================================================================
# Bug-injection check: does the test suite actually catch bugs?
#
# For each mutation below, this script
#   1. copies the backend to a temporary folder (your files are never touched),
#   2. makes ONE small, realistic code change - a bug,
#   3. runs the suite there, stopping at the first failure,
#   4. records CAUGHT (some test failed) or MISSED (everything still passed).
#
# A MISSED mutation means a bug of that kind could ship with a green suite:
# either add a test for it, or decide consciously that it does not matter.
#
# Usage (from the backend folder):
#     python tests/mutation/run_mutations.py              # all mutations
#     python tests/mutation/run_mutations.py M03 M10      # just these
#
# Takes roughly 15-60 s per mutation.
# =============================================================================
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]

# (id, file, original text, mutated text, what the bug means)
# The original text must appear exactly once on a non-comment line.
MUTATIONS = [
    ("M01", "usage_accounting.py", "return live + deleted", "return live",
     "Deleting screenshots refunds quota again (tombstones ignored)"),
    ("M02", "usage_accounting.py",
     "day = min(dt.day, calendar.monthrange(year, month)[1])", "day = dt.day",
     "Billing anniversary not clamped: Jan 31 + 1 month crashes"),
    ("M03", "routers/screenshot.py",
     "return current < int(limit), current, int(limit)",
     "return current <= int(limit), current, int(limit)",
     "Off-by-one: one capture more than the plan allows"),
    ("M04", "models.py",
     'return TIER_FEATURES.get(tier, {}).get(feature_name, False)',
     'return TIER_FEATURES.get(tier, {}).get(feature_name, True)',
     "Unknown features default to allowed"),
    ("M05", "routers/screenshot.py",
     'if request.format == "pdf" and not has_feature(current_user, "pdf"):',
     'if False and request.format == "pdf":',
     "PDF tier gate removed from single capture"),
    ("M06", "main.py", "        limiter.sem.release()", "        pass",
     "Concurrency slot never released: user locked out after N captures"),
    ("M07", "auth_deps.py",
     "payload = jwt.decode(token_or_key, SECRET_KEY, algorithms=[ALGORITHM])",
     'payload = jwt.decode(token_or_key, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_exp": False})',
     "Expired JWTs accepted"),
    ("M08", "api_key_system.py",
     "        ApiKey.key_hash == key_hash,\n        ApiKey.is_active == True\n    ).first()",
     "        ApiKey.key_hash == key_hash,\n    ).first()",
     "Revoked / regenerated API keys keep working"),
    ("M09", "main.py", 'email    = (user.email    or "").strip().lower()',
     'email    = (user.email    or "").strip()',
     "Emails stored with capitals: duplicates and failed logins"),
    ("M10", "main.py", "    record_screenshot_deletion(db, record)\n",
     "    pass\n",
     "History-page delete stops writing the usage tombstone"),
    ("M11", "routers/screenshot.py", "    record_screenshot_deletion(db, screenshot)\n",
     "    pass\n",
     "API delete stops writing the usage tombstone"),
    ("M12", "subscription_sync.py", "if not lookup_ok:", "if False:",
     "Stripe outage downgrades paying customers"),
    ("M13", "main.py", "if event_id in _IDEMP_STORE:", "if False:",
     "Duplicate Stripe webhooks processed twice"),
    ("M14", "batch.py", "if len(urls) > remaining:", "if False:",
     "Batch walks past the remaining screenshot quota"),
    ("M15", "batch.py", 'if job and job["user_id"] == user_id:', "if job:",
     "Any user can read / cancel / delete another user's batch job"),
    ("M16", "main.py",
     "data  = serializer.loads(payload.token, max_age=RESET_TOKEN_TTL_SECONDS)",
     "data  = serializer.loads(payload.token)",
     "Password-reset links never expire"),
    ("M17", "subscription_sync.py",
     "        _clear_billing_period(user)\n        _stamp_verified(user)",
     "        _stamp_verified(user)",
     "Downgraded users keep a stale billing anchor"),
    ("M18", "usage_accounting.py", "Screenshot.created_at >= period_start,",
     "Screenshot.created_at > period_start,",
     "A capture at the exact period start is not counted"),
    ("M19", "main.py", 'stripe.Subscription.delete(sub["id"])', "pass",
     "Deleting an account leaves the Stripe subscription billing"),
    ("M20", "routers/screenshot.py",
     'if request.webhook_url and not has_feature(current_user, "webhooks"):',
     "if False:",
     "Webhooks available below Business"),
    ("M21", "batch.py", '    "free":     0,\n    "pro":      50,',
     '    "free":     50,\n    "pro":      50,',
     "Free tier allowed 50-URL batches (per-batch cap)"),
    ("M22", "routers/screenshot.py",
     ".filter(Screenshot.id == screenshot_id, Screenshot.user_id == current_user.id)\n        .first()\n    )\n    if not screenshot:\n        raise HTTPException(status_code=404, detail=\"Screenshot not found\")\n\n    return {",
     ".filter(Screenshot.id == screenshot_id)\n        .first()\n    )\n    if not screenshot:\n        raise HTTPException(status_code=404, detail=\"Screenshot not found\")\n\n    return {",
     "Any user can read another user's screenshot metadata"),
    ("M23", "main.py", '    if plan not in {"pro", "business", "premium"}:',
     '    if plan not in {"pro", "business", "premium", "free"}:',
     "Checkout accepts plan=free"),
    ("M24", "routers/screenshot.py", '    "free":     1920,\n    "pro":      3840,',
     '    "free":     3840,\n    "pro":      3840,',
     "Free tier gets 4K widths (single capture only)"),
    ("M25", "main.py",
     '        created_at=datetime.utcnow(),\n        subscription_tier="free",',
     '        created_at=datetime.utcnow(),\n        subscription_tier="pro",',
     "New accounts start on Pro"),
    # Added after the independent review (its X-series). X07 (batch PDF/width
    # gates removed) is deliberately absent: no tier can reach those gates
    # through the API, so it cannot change behaviour.
    ("M26", "main.py",
     '        raise HTTPException(status_code=400, detail="Invalid signature")',
     '        import json as _j; event = _j.loads(payload)',
     "Stripe webhook signature failures ignored"),
    ("M27", "main.py", '@app.get("/users/me", response_model=UserResponse)', '@app.get("/users/me")',
     "/users/me returns hashed_password and Stripe ids"),
    ("M28", "main.py",
     '    if not verify_password(request.current_password, current_user.hashed_password):',
     '    if False:',
     "change_password skips the current-password check"),
    ("M29", "routers/screenshot.py",
     '    if request.custom_js and not has_feature(current_user, "custom_js"):',
     '    if False:',
     "custom_js available on Free"),
    ("M30", "routers/screenshot.py",
     '            await storage_service.delete_screenshot(screenshot.storage_key)',
     '            pass',
     "API delete leaves the R2 object"),
    ("M31", "main.py",
     '    user = db.query(User).filter(User.email == email).first()\n    if not user:\n        raise HTTPException(status_code=400, detail="Reset link invalid")',
     '    user = db.query(User).first()\n    if not user:\n        raise HTTPException(status_code=400, detail="Reset link invalid")',
     "Password reset changes the first user in the table"),
    ("M32", "batch.py", '            BatchJob.user_id == user_id,\n        ).first()', '        ).first()',
     "Batch job readable by anyone after a restart (DB fallback)"),
    ("M33", "main.py",
     '    if stripe and getattr(current_user, "stripe_customer_id", None) and user_tier != "free":',
     '    if False:',
     "Account deletion never cancels Stripe"),
    ("M34", "routers/screenshot.py",
     '    can_use, current, limit = check_user_screenshot_limit(current_user, db)\n    if not can_use:',
     '    can_use, current, limit = check_user_screenshot_limit(current_user, db)\n    if False:',
     "Single-capture quota removed"),
]

TIMEOUT_S = 900


def _non_comment_count(text: str, needle: str) -> int:
    count, start = 0, 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            return count
        line_start = text.rfind("\n", 0, i) + 1
        if not text[line_start:i].lstrip().startswith("#"):
            count += 1
        start = i + 1


def _apply(root: Path, rel: str, old: str, new: str) -> None:
    path = root / rel
    text = path.read_text(encoding="utf-8")
    n = _non_comment_count(text, old)
    if n != 1:
        raise SystemExit(f"{rel}: expected 1 live occurrence of {old!r}, found {n}")
    idx, start = -1, 0
    while True:
        idx = text.find(old, start)
        line_start = text.rfind("\n", 0, idx) + 1
        if not text[line_start:idx].lstrip().startswith("#"):
            break
        start = idx + 1
    path.write_text(text[:idx] + new + text[idx + len(old):], encoding="utf-8")


def _copy_backend(dest: Path) -> None:
    ignore = shutil.ignore_patterns(
        "venv", ".venv", ".git", "__pycache__", "*.db", "*.db-*", "screenshots",
        "stripe-cli", "tools", "*.zip", "node_modules", ".env", ".env.*",
    )
    shutil.copytree(BACKEND, dest, ignore=ignore)


def _run_suite(cwd: Path):
    """Return (verdict, first_failing_test, seconds).
    verdict: PASS (exit 0), FAIL (exit 1 with a FAILED test line),
             TIMEOUT, or BROKEN (anything else: collection/import errors...)."""
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "tests/suite"],
            cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT", "", time.time() - t0
    secs = time.time() - t0
    first = ""
    for line in proc.stdout.splitlines():
        if line.startswith("FAILED tests/"):
            first = line.split(" - ")[0].split(" ", 1)[1]
            break
    if proc.returncode == 0:
        return "PASS", "", secs
    if proc.returncode == 1 and first:
        return "FAIL", first, secs
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-5:])
    return "BROKEN", tail, secs


def main(selected: list[str]) -> int:
    todo = [m for m in MUTATIONS if not selected or m[0] in selected]
    results = []
    with tempfile.TemporaryDirectory(prefix="pp-mutation-", ignore_cleanup_errors=True) as tmp:
        pristine = Path(tmp) / "pristine"
        _copy_backend(pristine)

        # The suite must be green on unmodified code, or every "CAUGHT" below
        # would mean nothing.
        verdict, detail, secs = _run_suite(pristine)
        print(f"Baseline (no mutation): {verdict} ({secs:.0f}s)", flush=True)
        if verdict != "PASS":
            print(detail)
            print("Baseline is not green - fix the suite before trusting mutation results.")
            return 2

        for mid, rel, old, new, meaning in todo:
            work = Path(tmp) / mid
            shutil.copytree(pristine, work)
            _apply(work, rel, old, new)
            verdict, detail, secs = _run_suite(work)
            label = {"FAIL": "CAUGHT", "PASS": "MISSED"}.get(verdict, verdict)
            results.append((mid, label))
            print(f"{mid} {label:7} ({secs:4.0f}s)  {meaning}"
                  + (f"\n      by {detail}" if detail else ""), flush=True)
            shutil.rmtree(work, ignore_errors=True)

    caught = sum(1 for _, label in results if label == "CAUGHT")
    print(f"\n{caught}/{len(results)} mutations caught by a failing test")
    others = [f"{m} {lab}" for m, lab in results if lab not in ("CAUGHT",)]
    if others:
        print("Not caught / needs a look: " + ", ".join(others))
    return 0 if caught == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
