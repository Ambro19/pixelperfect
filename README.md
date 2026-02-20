# PixelPerfect Screenshot API

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

This project is proprietary software unless otherwise stated. Unauthorized copying, modification, or distribution of this software, in whole or in part, is strictly prohibited.