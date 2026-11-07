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

Bug injection: 34 realistic bugs were injected one at a time, on top of a green baseline run, and **all 34 were caught by a failing test**. The first version caught 25 of 30; the 5 it missed (found by the independent review) now have tests. One proposed mutation was dropped because no tier can reach that code through the API, so it cannot change behaviour.

## Findings

All of these were reproduced against the current code. Each automated one has a test in `tests/suite/known_defects/`, which reports `xfailed` until the defect is fixed. No application code was changed.

| ID | Sev. | Finding | Where |
|---|---|---|---|
| F-01 | **High** | The hot path (every single capture) runs Playwright's sync API on a **3-thread pool**. The browser belongs to one thread, so captures sent to another thread fail with "Cannot switch to a different thread". With real Chromium, 5 of 6 overlapping captures failed. `root screenshot_service.py` was fixed in September; this copy was not. Setting `max_workers=1` made all 18 browser tests pass, three runs in a row. | `services/screenshot_service.py:232` |
| F-14 | **High** | No timeout on `page.evaluate()` (`_JS_TIMEOUT_MS` is defined but never used). A hostile page on a Free full-page capture, or a `custom_js` that never resolves, freezes the capture thread for good. **Fix together with F-01**: with one worker thread, a single stuck capture stops every capture. Manual repro: `python tests/manual/repro_f14_capture_hang.py`. | `services/screenshot_service.py:793, 837`; same in root module |
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
2. **Order of fixes:** F-01 and F-14 ship together, in the capture-engine release. A single worker thread with no evaluate timeouts would turn one hostile page into a full outage.

## Limits of this suite

- The default run uses fakes for Playwright, R2 and Stripe. Fakes can't show behaviour the real service has and the fake lacks. That is why the real-browser tests exist and why F-14 needed real Chromium.
- In the real-browser tests, the hot-path module is marked non-strict `xfail` because of F-01: it passes or fails by luck. Remove those markers once F-01 is fixed.
- SQLite stands in for PostgreSQL, so row locking (F-12) and timezone storage differences aren't tested.
