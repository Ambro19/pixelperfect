# PixelPerfect: Automated Test Suite

The suite checks the backend in three stages, the same pattern used for Brief!:

1. **Regular tests** (`tests/suite`): unit, API and contract tests, plus known-defect tests.
2. **Bug injection** (`tests/mutation/run_mutations.py`): makes realistic bugs on purpose and checks that the suite catches each one.
3. **Independent review**: a reviewer who did not write the tests checks the code and the suite. Its results are in the findings table below.

No server, internet, Stripe account or test users are needed. Each run uses a throwaway SQLite database. Playwright, Cloudflare R2, Stripe and SMTP are replaced by fakes. Any attempt to open a connection to a non-local address fails the test, so a missed fake can never reach a real service.

## Running it (Windows PowerShell)

```powershell
cd C:\path\to\pixelperfect\backend
.\run_tests.ps1                 # regular suite, about 1.5 minutes
.\run_tests.ps1 -Browser        # also real-Chromium tests (needs: python -m playwright install chromium)
.\run_tests.ps1 -Mutation       # also the bug-injection check, about 30 minutes
.\run_tests.ps1 -Coverage       # also a line-coverage report
.\run_tests.ps1 -Only webhook   # only tests whose name contains "webhook"
```

Or call pytest directly: `python -m pytest`, `python -m pytest --browser`, `python -m pytest tests/suite/api/test_batch_api.py -v`.

## Reading the result

| pytest says | Meaning |
|---|---|
| `passed` | The behaviour is correct. |
| `xfailed` | A **known defect** (F-xx below) is still open. Expected; the run stays green. |
| `FAILED ... [XPASS(strict)]` | A known defect is now **fixed**. Delete that test's `@defect(...)` line; from then on it is an ordinary regression test. |
| `FAILED` (anything else) | Something broke. Read the first failure. |
| `skipped` | Real-browser tests when `--browser` was not given. |

## Layout

```
pytest.ini                     test paths and settings
run_tests.ps1                  Windows runner
tests/suite/conftest.py        environment, fakes (Playwright, R2, Stripe, SMTP), network guard
tests/suite/unit/              pure logic: usage periods, tier policy, hashing/keys, Stripe sync, batch helpers
tests/suite/api/               every public endpoint through FastAPI's TestClient
tests/suite/integration/       real Chromium against a local page (opt-in: --browser)
tests/suite/known_defects/     one strict-xfail test per open finding
tests/mutation/run_mutations.py  bug injection
```

The three older files in `tests/` (`test_api.py`, `test_screenshot.py`, `load_test.py`) need a live server, real Chromium and the internet. They are left as they were and are not part of the default run.

## What is covered

| Area | What the tests prove |
|---|---|
| Usage and quota | Period boundaries (calendar month for Free, Stripe anniversary for paid, rollover after missed webhooks, clamping on the 31st). Deleting never refunds quota on either delete endpoint. 429 at exactly the limit. Premium unlimited. |
| Tier policy | Limits per tier, the feature matrix, env overrides, and the copies in `models.py`, `batch.py` and `routers/screenshot.py` agreeing with each other. |
| Auth | Register and login (username or email, any case), JWT expiry, forged and `alg:none` tokens, API keys (hashed, regenerate kills the old one), password change, profile update, the full forgot and reset flow, no account enumeration. |
| Capture API | Options reach the engine. Every tier gate for every tier. Engine faults map to 503, bad input to 400 or 422. Truncation is reported. R2 failure falls back to local storage. Ownership on get, list, delete and history. |
| Concurrency | 2, 3 and 5 concurrent captures per tier, measured with real overlapping requests. The limit is per user, slots come back after errors and refusals, and an upgrade applies. |
| Batch | JSON and file submit (CSV, TSV, TXT), the background run, partial failure, size cap, period quota, screenshot quota, rebuild after restart, cancel, retry, delete, ownership. |
| Stripe | Webhook signature, idempotency, mapping by customer and email, sync failures that must not downgrade (outage, unresolvable price), the 5-layer tier resolution, overdue re-verification throttling, checkout, portal, cancel, state cache. |
| Account deletion | Every table emptied for that user only, credentials dead, Stripe subscription cancelled, local files removed. |
| Routing contract | Which module serves each public path (this catches a reordered `include_router`), security headers, CORS, health, OpenAPI. |
| Real browser | Real PNG at the requested size, pixel colours, `remove_elements`, dark mode, full page, JPEG, WebP and PDF, overlapping captures. Run with `--browser`, on both screenshot-service modules. |

Bug injection: **52 realistic bugs**, each injected on its own on top of a green baseline run, were **all caught by a failing test**. 34 were in the original suite, 9 came with Release A (billing-safe deletion) 7 with Release B (capture engine) and 2 with Release C (log safety). One proposed mutation in each round was replaced because it could not change behaviour (an equivalent mutation).

## Findings

All of these were reproduced against the current code. Each automated one has a test in `tests/suite/known_defects/`, which reports `xfailed` until the defect is fixed. No application code was changed.

| ID | Sev. | Finding | Where |
|---|---|---|---|
| F-01 | ~~High~~ **Fixed** | **Fixed in the capture-engine release (Oct 2026).** The hot path ran Playwright's sync API on a 3-thread pool, so captures landing on a thread that did not own the browser failed with "Cannot switch to a different thread". It now uses one worker thread, like the batch module. | `services/screenshot_service.py` |
| F-14 | ~~High~~ **Fixed** | **Fixed in the capture-engine release (Oct 2026).** `page.evaluate()` had no timeout, so a hostile page or a never-resolving `custom_js` froze the capture thread for good. Now there are three layers (`services/capture_guard.py`, used by both modules): in-page time limits that use timers and Promise functions the page cannot replace; a 20 s budget per page script, enforced from outside the page; and a 180 s limit per capture (running time only). If a limit is exceeded, the driver is killed and the engine restarts. Captures queued behind it still succeed. | `services/capture_guard.py` |
| F-02 | **High** | `POST /api/v1/batch/submit/` (trailing slash) is served by the old wrapper in `main.py`. It does no screenshot-quota check, writes no `BatchJob` row (so the batch count is never used up) and runs up to 50 captures inside one request. | `main.py:1430` |
| F-03 | **High** | SSRF. Capture URLs, batch URLs and `webhook_url` accept `127.0.0.1`, `localhost`, `169.254.169.254`, `10.x`, `192.168.x` and `[::1]`, and the internal page comes back as an image. A real fix must also block hostnames that resolve to private IPs, redirects to them and sub-requests, so the check belongs inside the browser (`page.route`), not only on the input URL. | `routers/screenshot.py`, `batch.py`, both services |
| F-15 | **High** | Stripe sync **adopts any Stripe customer with the same email**, and emails are never verified. Registering with someone's email and calling `/subscription_status?sync=1` can hand over their paid subscription and open the billing portal on their card. Fix: adopt only when `customer.metadata.app_user_id` matches (`ensure_stripe_customer_for_user` already writes it). | `subscription_sync.py:445, 470` |
| F-16 | ~~High~~ **Fixed** | **Fixed in the billing-safe deletion release (Oct 2026).** Deletion used to cancel only `active` subscriptions, and skipped Stripe when the local tier said free. It now cancels every subscription that can still bill, whatever the local tier says. | `main.py` `_cancel_all_billing` |
| F-17 | **High** | An API key alone can change the account email, then reset the password: a full takeover. It can also delete the account. API keys end up in CI systems and logs, so account-level actions should need the JWT plus the password. | `main.py:1113, 1183` |
| F-05 | Med | A password-reset link can be reused within its hour, and still works after the password has changed. | `main.py:1064` |
| F-06 | Med | Tokens are keyed on the username. After a rename or account deletion, an old token logs in as whoever takes the name next, and tokens survive a password change for 24 h. Fix: put the user id (and a token version) in the JWT. | `auth_deps.py`, `main.py:524` |
| F-07 | Med | The webhook is marked "seen" before it is processed, so if processing fails, Stripe's retry is dropped as a duplicate. | `main.py:1447` |
| F-13 | Med | Batch `retry_failed` recaptures without checking the period screenshot quota. Failed items cost nothing up front, so this can be repeated across many jobs. | `batch.py:1087` |
| F-18 | Med | The History-page delete and account deletion leave the image in R2, and R2 objects are served with a one-year CDN cache. | `main.py:1363`, `delete_account` |
| F-19 | Med | 500 responses echo the raw exception text, which can include database or host details. | `routers/screenshot.py:706`, `screenshot_endpoints.py:496`, billing errors |
| F-20 | Med | No rate limit on forgot-password, forgot-username or `/contact`. They all send through one Gmail account, so its daily cap can be used up and password resets stop for everyone. | `main.py` |
| F-22 | Med | Checkout is allowed while already subscribed, which creates a second, double-billed subscription. | `main.py:1500` |
| F-23 | ~~High~~ **Fixed** | **Fixed in Release C (Nov 2026).** Found in the production logs, not by the suite: the startup banner logged `DATABASE_URL` in full, password included. It now goes through `mask_db_url()`, which prints `***` in place of the password (also in `delete_user.py`). A source-level test fails if any log or print call is handed the raw `DATABASE_URL` again. The production database credential was rotated on Render the same week. | `log_safety.py`, `main.py` |
| F-04 | Low | Deleting a batch job gives back the period batch count. Screenshot quota still applies. | `batch.py:1152` |
| F-08 | Low | Register and reset accept any password (even empty), while change-password requires 8 characters. Register accepts an invalid email and a blank username. | `main.py:559, 580` |
| F-09 | Low | Forgot-password matches the email case-sensitively, so a capitalised entry gets no mail (and a half-fix gives a broken link). | `main.py:955` |
| F-11 | Low | The billing-portal `return_url` comes from the caller. Low impact (bearer auth), but simple to pin to `FRONTEND_URL`. | `main.py:1598` |
| F-12 | Low | Quota is checked, then the row is written later, so simultaneous captures can each take the last unit (bounded by the concurrency limit). A real fix needs a row lock on PostgreSQL that SQLite can't show. | `routers/screenshot.py:454` |
| F-21 | Low | `wait_for_selector` is documented as Pro+ but works on Free. | `routers/screenshot.py:467` |
| F-10 | Info | The October `created_by` metadata fix landed in `payment.py` and `routers/payment.py`, which are **not mounted**. Real customers are created by `main.ensure_stripe_customer_for_user`. | `main.py:536` |

Reported by the review but not pinned as tests (design or infrastructure):

- **Stripe sync errors inside a webhook still return 200**, so Stripe never retries and a user who just paid can stay on Free until a dashboard sync. Decide this together with F-07.
- `?sync=1` and `subscription_state?fresh=1` call Stripe without a throttle.
- Synchronous database and boto3 calls inside async handlers stall every request during an upload.
- `User.is_active` is never checked when authenticating.
- HSTS is never sent behind Render's proxy (uvicorn needs `--proxy-headers --forwarded-allow-ips`).
- Production CORS still includes `localhost` and LAN IPs.
- The in-memory `JOBS` dict never shrinks.
- The main cancel path returns `cancel_at: null` on new Stripe API versions, because the period end now lives on the subscription item.

## Decisions made

1. **Deleting an account when Stripe is unreachable** (decided Oct 2026): **refuse until Stripe confirms.**
   - Before any data is deleted, every subscription that can still bill (active, trialing, past_due, unpaid, incomplete, paused) is cancelled. This covers the stored Stripe customer and any other customer tagged with this account's id (`metadata.app_user_id`). A customer that merely shares the email is never touched, because emails are not verified.
   - Stripe unreachable (outage, timeout, rate limit) → **503** "your account was not deleted, try again in a minute". The account stays intact, and retrying is safe.
   - Stripe unusable from the server (missing, mistyped or revoked key) → **503** "problem on our side", logged as CRITICAL, with no invitation to retry.
   - A customer that no longer exists in Stripe has nothing to bill, so the deletion proceeds.
   - Stripe is called off the event loop, so a slow Stripe never freezes other requests.
   - Pinned by `tests/suite/api/test_account_deletion.py` and mutations M19, M33, M35–M37 and M43–M46.
   - Not covered: open invoices on cancelled `past_due` subscriptions are not voided, and the Stripe customer itself is kept (deleting it would also stop a checkout completed in another tab after deletion, but would remove the customer record from Stripe). Those are product decisions for a later release.
2. **Order of fixes:** F-01 and F-14 shipped together in the capture-engine release, after Release A.

## Capture-engine behaviour customers may notice

- `custom_js` now has a **10 s** limit (`CUSTOM_JS_TIMEOUT_MS`). A script that runs longer is skipped with a `js_warning`, and the capture still completes. Before, there was no limit. Every form that worked before still works: statements, arrow functions, `function () {}`, `async function () {}` and named functions. All of them are tested in a real browser.
- When `custom_js` is used, the page's Content-Security-Policy is bypassed so the script can run. Resources that CSP would block may then load.
- A page that keeps a capture running past a limit returns **504** "did not finish rendering", with no retry hint. A capture caught up in a restart, or one that waits too long in the queue, returns **503** with `Retry-After`.
- All limits can be changed with environment variables: `CAPTURE_HARD_TIMEOUT_S`, `CAPTURE_EVALUATE_BUDGET_S`, `CAPTURE_QUEUE_TIMEOUT_S`, `CUSTOM_JS_TIMEOUT_MS` and `CAPTURE_SCRIPT_TIMEOUT_MS`.

Known limits that remain:

- Captures run one at a time per module. A page that freezes itself costs about 20 s of engine time per request. Per-user concurrency limits and quotas bound how often any one account can cause that.
- Each forced restart leaves about 14 file descriptors open. Restarts are rare, and every deploy replaces the process.
- `kill_driver` relies on private Playwright attributes (pinned 1.49.1). `test_kill_driver_unblocks_a_real_driver` fails if an upgrade moves them.
- Killing the driver was verified on Linux, which is what Render runs. Whether Chromium child processes exit when the driver is killed on Windows is unverified, and that only affects local development.

## Limits of this suite

- The default run uses fakes for Playwright, R2 and Stripe. Fakes can't show behaviour the real service has and the fake lacks. That is why the real-browser tests exist and why F-14 needed real Chromium.
- SQLite stands in for PostgreSQL, so row locking (F-12) and timezone storage differences aren't tested.
