# PixelPerfect Screenshot API

Professional website screenshot API built with **FastAPI** + **Playwright**, with authentication, API keys, Stripe subscriptions, usage limits, and optional Cloudflare R2 storage.

**Production API:** `https://api.pixelperfectapi.net`  
**Frontend Dashboard:** `https://pixelperfectapi.net`

---

## Features

- ✅ High-fidelity screenshots via Playwright (Chromium)
- ✅ Multiple output formats: PNG / JPEG / WebP (via Pillow) / PDF
- ✅ Full-page and viewport captures
- ✅ Dark mode support
- ✅ Auth (JWT) + API key system
- ✅ Stripe subscriptions (Free / Pro / Business / Premium tiers)
- ✅ Per-tier usage limits and **per-user concurrency control**
- ✅ Batch screenshot processing (URL list, CSV/TXT/TSV, or file upload)
- ✅ Batch job management (polling, retry, delete)
- ✅ Screenshot history endpoints + static serving (optional)
- ✅ Cloud storage support (Cloudflare R2) + retention policy
- ✅ Production-ready deployment on Render (Docker recommended)

---

## API Documentation

When the service is running, Swagger UI is available at:

- `/docs` (Swagger UI)
- `/openapi.json` (OpenAPI schema)

Example:

- `https://api.pixelperfectapi.net/docs`

---

## Authentication Overview

PixelPerfect supports:

- **JWT login** (Bearer token)
- **API keys** (for programmatic access / customer integrations)

### Login (JWT)

- `POST /token` (form)
- `POST /token_json` (JSON)

JWT is returned as `access_token` and used like:

```bash
curl -H "Authorization: Bearer <your_access_token>" \
  https://api.pixelperfectapi.net/api/v1/screenshot
```

### API Keys

API keys are generated per user and used for programmatic / server-side integrations.

| Endpoint | Method | Description |
|---|---|---|
| `/api/keys/current` | `GET` | Retrieve your current API key info |
| `/api/keys/generate` | `POST` | Generate a new API key |
| `/api/keys/regenerate` | `POST` | Rotate your existing API key |

API keys are passed via the `X-API-Key` request header:

```bash
curl -H "X-API-Key: YOUR_API_KEY" \
  https://api.pixelperfectapi.net/api/v1/screenshot
```

> **Security note:** API keys are stored as secure hashes. Once generated, store your key safely — it will not be shown again in full. Regenerating a key immediately deactivates the previous one.

---

## Example: Screenshot Request (JWT)

```bash
curl -X POST https://api.pixelperfectapi.net/api/v1/screenshot \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "width": 1920,
    "height": 1080,
    "format": "png",
    "full_page": false,
    "dark_mode": false
  }'
```

## Example: Screenshot Request (API Key)

```bash
curl -X POST https://api.pixelperfectapi.net/api/v1/screenshot \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com"
  }'
```

### Screenshot Request Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | string | required | Target website URL |
| `width` | integer | `1920` | Viewport width (320–3840) |
| `height` | integer | `1080` | Viewport height (240–2160) |
| `format` | string | `png` | Output format: `png`, `jpeg`, `webp`, `pdf` |
| `full_page` | boolean | `false` | Capture full scrollable page |
| `dark_mode` | boolean | `false` | Enable dark color scheme |

> **WebP support** requires Pillow to be installed (`pip install Pillow`). A PNG is captured internally and then converted. All other formats are handled natively by Playwright.

---

## Batch Screenshot Processing

Batch processing is available on **Pro** plans and above. Jobs are processed asynchronously and can be polled for status updates.

### Submit a Batch Job

**Option 1 — Direct URL list:**

```bash
curl -X POST https://api.pixelperfectapi.net/api/v1/batch/submit \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "urls": [
      "https://example.com",
      "https://google.com",
      "https://github.com"
    ],
    "format": "png",
    "width": 1920,
    "height": 1080,
    "full_page": false
  }'
```

**Option 2 — CSV / TXT / TSV text:**

```bash
curl -X POST https://api.pixelperfectapi.net/api/v1/batch/submit \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "csv_text": "https://example.com\nhttps://google.com\nhttps://github.com",
    "format": "png"
  }'
```

**Option 3 — File upload (CSV / TXT / TSV):**

```bash
curl -X POST https://api.pixelperfectapi.net/api/v1/batch/submit_file \
  -H "Authorization: Bearer <token>" \
  -F "file=@urls.csv" \
  -F "format=png" \
  -F "width=1920" \
  -F "height=1080"
```

Accepted file formats: `.csv`, `.txt`, `.tsv`. The parser auto-detects comma, tab, or newline-separated values. Duplicate URLs are automatically deduplicated before the job is created.

### Batch Request Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `urls` | array | — | Direct list of URLs |
| `csv_text` | string | — | CSV / TXT / TSV formatted URL content |
| `format` | string | `png` | Output format: `png`, `jpeg`, `webp`, `pdf` |
| `width` | integer | `1920` | Viewport width |
| `height` | integer | `1080` | Viewport height |
| `full_page` | boolean | `false` | Full-page capture |
| `quality` | integer | — | JPEG / WebP quality (1–100) |

### Manage Batch Jobs

| Endpoint | Method | Description |
|---|---|---|
| `/api/v1/batch/jobs` | `GET` | List all your batch jobs (newest first) |
| `/api/v1/batch/jobs/{job_id}` | `GET` | Get full details and per-item status |
| `/api/v1/batch/jobs/{job_id}/retry_failed` | `POST` | Requeue all failed items without re-running successful ones |
| `/api/v1/batch/jobs/{job_id}` | `DELETE` | Delete a batch job |

### Job Status Lifecycle

A batch job progresses through the following statuses:

```
queued → processing → completed
                    → partial   (some succeeded, some failed)
                    → failed    (all items failed)
```

Each item within the job independently tracks: `queued`, `processing`, `completed`, or `failed`. The `retry_failed` endpoint requeues only failed items, leaving successful results intact.

### Example: Polling a Batch Job

```bash
curl https://api.pixelperfectapi.net/api/v1/batch/jobs/<job_id> \
  -H "Authorization: Bearer <token>"
```

**Example response:**

```json
{
  "id": "a3f9e1b2c4d5e6f7",
  "status": "completed",
  "format": "png",
  "total": 3,
  "completed": 3,
  "failed": 0,
  "queued": 0,
  "processing": 0,
  "created_at": "2026-02-20T10:00:00",
  "items": [
    {
      "idx": 0,
      "url": "https://example.com",
      "status": "completed",
      "screenshot_url": "/screenshots/screenshot_20260220_100001_abc123.png",
      "file_size": 245760,
      "processing_time": 2.31
    }
  ]
}
```

---

## Subscription Tiers

PixelPerfect supports tier-based usage limits and concurrency controls.

| Tier | Screenshots / Month | Batch URLs / Job | Concurrency |
|---|---|---|---|
| Free | 100 | Not available | 2 |
| Pro | 5,000 | 50 | 3 |
| Business | 50,000 | 200 | 5 |
| Premium | Unlimited | 1,000 | 5+ |

Stripe lookup keys are mapped to internal tiers via environment variables. Batch processing is unavailable on the Free tier — an HTTP `403` is returned if attempted.

---

## Concurrency Model

Concurrency is handled **per-user**, not via multiple Uvicorn workers.

For maximum stability with Playwright:

```env
WEB_CONCURRENCY=1
```

Tier-based concurrency limits:

```yaml
starter:  2
pro:      3
business: 5
```

If a user exceeds their concurrency limit, the API returns HTTP `429` with a `Retry-After: 1` header. The client should wait briefly and retry. This prevents browser launch race conditions, Playwright instability, and memory overuse from multi-worker duplication.

---

## Output Formats

| Format | Notes |
|---|---|
| `png` | Lossless. Default. Best for accuracy and text clarity. |
| `jpeg` | Compressed, quality 90 by default. Smaller file size. |
| `webp` | Requires Pillow (`pip install Pillow`). PNG captured first, then converted. |
| `pdf` | A4 format, print background enabled. |

---

## Storage Options

PixelPerfect supports two storage modes:

### 1️⃣ Local Storage

Screenshots saved to the `/screenshots` directory, served via static file middleware at `/screenshots/<filename>`. Suitable for development or low-volume deployments. WebP Content-Type is handled via a custom static files middleware to ensure correct browser rendering.

### 2️⃣ Cloudflare R2 (Recommended)

Configured via environment variables:

```env
STORAGE_TYPE=r2
R2_ENDPOINT_URL=...
R2_BUCKET_NAME=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
FILE_RETENTION_DAYS=7
```

Benefits: scalable, CDN-friendly, no local disk limitations, and automatic file retention / expiry policies.

---

## Screenshot History

Authenticated users can retrieve their screenshot history.

| Endpoint | Method | Description |
|---|---|---|
| `/api/history` | `GET` | List all screenshots for the current user |
| `/api/history/{id}` | `GET` | Get metadata for a specific screenshot |
| `/api/history/{id}` | `DELETE` | Delete a screenshot record |

---

## Rate Limiting

Optional request rate limiting to prevent abuse:

```env
RATE_LIMIT_ENABLED=true
RATE_LIMIT_FREE_TIER=120
```

Rate limits are applied per user per minute. Designed to protect infrastructure and ensure fair usage across all subscription tiers.

---

## Health Check

```
GET /health
```

Returns real-time service status, Stripe configuration state, screenshot service readiness, and tier concurrency configuration.

**Example response:**

```json
{
  "status": "healthy",
  "timestamp": "2026-02-20T10:00:00",
  "environment": "production",
  "services": {
    "stripe": "configured",
    "screenshot_service": "ready"
  },
  "tier_concurrency": {
    "starter": 2,
    "pro": 3,
    "business": 5
  }
}
```

---

## Deployment Notes

### Stack

Production uses the following components:

- **Docker** — containerized runtime
- **Playwright** (Chromium headless) — screenshot engine
- **Pillow** — WebP conversion support
- **PostgreSQL** — persistent data store
- **Stripe Webhooks** — subscription lifecycle management
- **Cloudflare R2** — cloud screenshot storage (optional)

### Custom Domain

```
https://api.pixelperfectapi.net
```

### Pre-deployment Checklist

- [ ] Docker image installs Playwright browsers at build time
- [ ] Pillow installed for WebP support (`pip install Pillow`)
- [ ] Stripe webhooks are registered and secret is configured
- [ ] `ENVIRONMENT=production` is set
- [ ] `DEBUG=false` is set
- [ ] `WEB_CONCURRENCY=1` is set (required for Playwright stability)
- [ ] All required environment variables are present (see below)

### Playwright Browser Installation

If using **Render (non-Docker)**, add to your Build Command:

```bash
python -m playwright install --with-deps chromium
```

If using **Docker**, add to your Dockerfile:

```dockerfile
RUN python -m playwright install --with-deps chromium
```

Failure to install browsers will result in HTTP `503` responses from all screenshot endpoints.

### Required Environment Variables

```env
# Application
SECRET_KEY=
ENVIRONMENT=production
DEBUG=false
FRONTEND_URL=https://pixelperfectapi.net
BACKEND_URL=https://api.pixelperfectapi.net
CUSTOM_API_DOMAIN=https://api.pixelperfectapi.net

# Database
DATABASE_URL=

# Stripe
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_PRO_LOOKUP_KEY_MONTHLY=
STRIPE_PRO_LOOKUP_KEY_YEARLY=
STRIPE_BUSINESS_LOOKUP_KEY_MONTHLY=
STRIPE_BUSINESS_LOOKUP_KEY_YEARLY=
STRIPE_PREMIUM_LOOKUP_KEY_MONTHLY=
STRIPE_PREMIUM_LOOKUP_KEY_YEARLY=

# Storage (optional R2)
STORAGE_TYPE=r2
R2_ENDPOINT_URL=
R2_BUCKET_NAME=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
FILE_RETENTION_DAYS=7

# Rate Limiting (optional)
RATE_LIMIT_ENABLED=true
RATE_LIMIT_FREE_TIER=120

# Concurrency (required for Playwright stability)
WEB_CONCURRENCY=1

# Token settings (optional)
ACCESS_TOKEN_EXPIRE_MINUTES=1440
CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS=5
```

---

## Security

- JWT-based authentication with configurable token expiry (default 24h)
- API key hashing — keys are never stored in plain text
- Environment-based secrets management
- CORS protection with an explicit origin allowlist
- Stripe webhook signature validation on all incoming events
- Security headers middleware: CSP, HSTS, X-Frame-Options, Referrer-Policy
- Docs paths (`/docs`, `/redoc`, `/openapi.json`) are exempt from restrictive CSP to keep Swagger UI fully functional

**Never commit the following to version control:**

- `SECRET_KEY`
- `STRIPE_SECRET_KEY`
- Database credentials
- Cloudflare R2 secrets

---

## Project Structure

```
pixelperfect/
├── alembic/                    # Database migrations
├── routers/                    # FastAPI route handlers
│   ├── auth.py
│   ├── batch.py                # Batch screenshot processing
│   ├── keys.py
│   ├── screenshots.py
│   ├── subscriptions.py
│   └── webhooks.py
├── services/                   # Business logic layer
│   ├── storage_service.py
│   └── subscription_service.py
├── models.py                   # SQLAlchemy models
├── main.py                     # Application entrypoint
├── screenshot_service.py       # Playwright screenshot engine (sync + async bridge)
├── screenshot_endpoints.py     # Single + batch screenshot endpoints
├── history.py                  # Screenshot history router
├── auth_deps.py                # JWT authentication dependencies
├── auth_utils.py               # Password hashing utilities
├── api_key_system.py           # API key generation and validation
├── webhook_handler.py          # Stripe webhook event processor
├── subscription_sync.py        # Stripe subscription sync utilities
├── db_migrations.py            # Startup migration runner
├── email_utils.py              # Password reset email delivery
├── Dockerfile
└── requirements.txt
```

---

## Future Roadmap

- [ ] Screenshot caching layer
- [ ] Webhook events for completed screenshot jobs
- [ ] Async job queue with optional Redis backend (persistent batch jobs)
- [ ] Team-based API key management
- [ ] Usage analytics dashboard
- [ ] Scheduled / recurring screenshot jobs
- [ ] Visual diff / comparison between snapshots

---

## License

Copyright © 2026 [OneTechly](https://onetechly.com)  
All rights reserved.

This project is proprietary software unless otherwise stated. Unauthorized copying, modification, or distribution of this software, in whole or in part, is strictly prohibited.




<!-- # PixelPerfect Screenshot API

Professional website screenshot API built with **FastAPI** + **Playwright**, with authentication, API keys, Stripe subscriptions, usage limits, and optional Cloudflare R2 storage.

**Production API:** `https://api.pixelperfectapi.net`  
**Frontend Dashboard:** `https://pixelperfectapi.net`

---

## Features

- ✅ High-fidelity screenshots via Playwright (Chromium)
- ✅ Multiple output formats: PNG / JPG / WebP (config dependent)
- ✅ Full-page and viewport captures
- ✅ Auth (JWT) + API key system
- ✅ Stripe subscriptions (Free / Pro / Business / Premium tiers)
- ✅ Per-tier usage limits and **per-user concurrency control**
- ✅ Screenshot history endpoints + static serving (optional)
- ✅ Cloud storage support (Cloudflare R2) + retention policy
- ✅ Production-ready deployment on Render (Docker recommended)

---

## API Documentation

When the service is running, Swagger UI is available at:

- `/docs` (Swagger UI)
- `/openapi.json` (OpenAPI schema)

Example:

- `https://api.pixelperfectapi.net/docs`

---

## Authentication Overview

PixelPerfect supports:

- **JWT login** (Bearer token)
- **API keys** (for programmatic access / customer integrations)

### Login (JWT)

- `POST /token` (form)
- `POST /token_json` (JSON)

JWT is returned as `access_token` and used like:

```bash
curl -H "Authorization: Bearer <your_access_token>" \
  https://api.pixelperfectapi.net/api/v1/screenshot
```

### API Keys

API keys are generated per user and used for programmatic / server-side integrations.

| Endpoint | Method | Description |
|---|---|---|
| `/api/keys` | `GET` | Retrieve your current API key |
| `/api/keys/generate` | `POST` | Generate a new API key |
| `/api/keys/regenerate` | `POST` | Rotate your existing API key |

API keys are passed via the `X-API-Key` request header:

```bash
curl -H "X-API-Key: YOUR_API_KEY" \
  https://api.pixelperfectapi.net/api/v1/screenshot
```

> **Security note:** API keys are stored as secure hashes. Once generated, store your key safely — it will not be shown again in full.

---

## Example: Screenshot Request (JWT)

```bash
curl -X POST https://api.pixelperfectapi.net/api/v1/screenshot \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "width": 1920,
    "height": 1080,
    "format": "png",
    "full_page": false,
    "dark_mode": false
  }'
```

## Example: Screenshot Request (API Key)

```bash
curl -X POST https://api.pixelperfectapi.net/api/v1/screenshot \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com"
  }'
```

---

## Subscription Tiers

PixelPerfect supports tier-based usage limits and concurrency controls.

| Tier | Screenshots / Month | Batch Limit | Concurrency |
|---|---|---|---|
| Free | 100 | 0 | 2 |
| Pro | 5,000 | 50 | 3 |
| Business | 50,000 | 100 | 5 |
| Premium | Unlimited | Unlimited | 5+ |

Stripe lookup keys are mapped to internal tiers via environment variables.

---

## Concurrency Model

Concurrency is handled **per-user**, not via multiple Uvicorn workers.

For maximum stability with Playwright:

```env
WEB_CONCURRENCY=1
```

Tier-based concurrency example:

```yaml
starter:  2
pro:      3
business: 5
```

This prevents:

- Browser launch race conditions
- Playwright instability
- Memory overuse from multi-worker duplication

---

## Storage Options

PixelPerfect supports two storage modes:

### 1️⃣ Local Storage

Screenshots saved to `/screenshots` directory. Suitable for development or low-volume deployments.

### 2️⃣ Cloudflare R2 (Recommended)

Configured via environment variables:

```env
STORAGE_TYPE=r2
R2_ENDPOINT_URL=...
R2_BUCKET_NAME=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
FILE_RETENTION_DAYS=7
```

**Benefits:**

- Scalable and cost-effective
- CDN-friendly delivery
- No local disk limitations
- Automatic file retention / expiry policies

---

## Rate Limiting

Optional request rate limiting to prevent abuse:

```env
RATE_LIMIT_ENABLED=true
RATE_LIMIT_FREE_TIER=120
```

Rate limits are applied per user per minute. Designed to protect infrastructure and ensure fair usage across all subscription tiers.

---

## Health Check

```
GET /health
```

Returns real-time service status, Stripe configuration state, and screenshot service readiness.

**Example response:**

```json
{
  "status": "healthy",
  "environment": "production",
  "services": {
    "stripe": "configured",
    "screenshot_service": "ready"
  }
}
```

---

## Deployment Notes

### Stack

Production uses the following components:

- **Docker** — containerized runtime
- **Playwright** (Chromium headless) — screenshot engine
- **PostgreSQL** — persistent data store
- **Stripe Webhooks** — subscription lifecycle management
- **Cloudflare R2** — cloud screenshot storage (optional)

### Custom Domain

```
https://api.pixelperfectapi.net
```

### Pre-deployment Checklist

- [ ] Docker image installs Playwright browsers at build time
- [ ] Stripe webhooks are registered and secret is configured
- [ ] `ENVIRONMENT=production` is set
- [ ] `DEBUG=false` is set
- [ ] All required environment variables are present (see below)

### Required Environment Variables

```env
# Application
SECRET_KEY=
ENVIRONMENT=production
DEBUG=false

# Database
DATABASE_URL=

# Stripe
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_PRICE_FREE=
STRIPE_PRICE_PRO=
STRIPE_PRICE_BUSINESS=
STRIPE_PRICE_PREMIUM=

# Storage (optional R2)
STORAGE_TYPE=r2
R2_ENDPOINT_URL=
R2_BUCKET_NAME=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
FILE_RETENTION_DAYS=7

# Rate Limiting (optional)
RATE_LIMIT_ENABLED=true
RATE_LIMIT_FREE_TIER=120

# Concurrency
WEB_CONCURRENCY=1
```

---

## Security

- JWT-based authentication with short-lived tokens
- API key hashing (keys are never stored in plain text)
- Environment-based secrets management
- CORS protection
- Stripe webhook signature validation on all incoming events

**Never commit the following to version control:**

- `SECRET_KEY`
- `STRIPE_SECRET_KEY`
- Database credentials
- Cloudflare R2 secrets

---

## Project Structure

```
pixelperfect/
├── alembic/                  # Database migrations
├── routers/                  # FastAPI route handlers
│   ├── auth.py
│   ├── keys.py
│   ├── screenshots.py
│   ├── subscriptions.py
│   └── webhooks.py
├── services/                 # Business logic layer
│   ├── storage_service.py
│   └── subscription_service.py
├── models.py                 # SQLAlchemy models
├── main.py                   # Application entrypoint
├── screenshot_service.py     # Playwright screenshot engine
├── screenshot_endpoints.py   # Screenshot API endpoints
├── Dockerfile
└── requirements.txt
```

---

## Future Roadmap

- [ ] Screenshot caching layer
- [ ] Webhook events for completed screenshot jobs
- [ ] Async job queue with optional Redis backend
- [ ] Team-based API key management
- [ ] Usage analytics dashboard

---

## License

Copyright © 2026 [OneTechly](https://pixelperfectapi.net)  
All rights reserved.

This project is proprietary software unless otherwise stated. Unauthorized copying, modification, or distribution of this software, in whole or in part, is strictly prohibited. -->