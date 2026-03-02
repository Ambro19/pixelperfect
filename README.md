# PixelPerfect Screenshot API

Professional website screenshot API built with **FastAPI** + **Playwright**, with authentication, API keys, Stripe subscriptions, usage limits, and optional Cloudflare R2 storage.

**Production API:** `https://api.pixelperfectapi.net`  
**Frontend Dashboard:** `https://pixelperfectapi.net`  
**Interactive Docs:** `https://api.pixelperfectapi.net/docs`

---

## Features

- ✅ High-fidelity screenshots via Playwright (Chromium)
- ✅ Multiple output formats: PNG / JPEG / WebP / PDF
- ✅ Full-page and viewport captures
- ✅ Dark mode support
- ✅ JWT + API key authentication
- ✅ Stripe subscriptions (Free / Pro / Business / Premium)
- ✅ Per-tier usage limits and concurrency control
- ✅ Batch screenshot processing (URL list, CSV/TXT/TSV, or file upload)
- ✅ Screenshot history and static serving
- ✅ Cloudflare R2 cloud storage with retention policy
- ✅ Production deployment on Render (Docker)

---

## Authentication

PixelPerfect supports two authentication methods:

**JWT (Bearer token)** — obtain via `POST /token` or `POST /token_json`:

```bash
curl -H "Authorization: Bearer <your_access_token>" \
  https://api.pixelperfectapi.net/api/v1/screenshot
```

**API Key** — passed via the `X-API-Key` header:

```bash
curl -H "X-API-Key: YOUR_API_KEY" \
  https://api.pixelperfectapi.net/api/v1/screenshot
```

> API keys are stored as secure hashes and cannot be recovered after creation. Regenerating a key immediately deactivates the previous one.

---

## Taking a Screenshot

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

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | string | required | Target website URL |
| `width` | integer | `1920` | Viewport width (320–3840) |
| `height` | integer | `1080` | Viewport height (240–2160) |
| `format` | string | `png` | `png`, `jpeg`, `webp`, `pdf` |
| `full_page` | boolean | `false` | Capture full scrollable page |
| `dark_mode` | boolean | `false` | Enable dark color scheme |

---

## Batch Processing

Available on **Pro** plans and above. Submit multiple URLs in one job and poll for results.

```bash
curl -X POST https://api.pixelperfectapi.net/api/v1/batch/submit \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://example.com", "https://google.com"],
    "format": "png"
  }'
```

Or upload a CSV / TXT / TSV file directly:

```bash
curl -X POST https://api.pixelperfectapi.net/api/v1/batch/submit_file \
  -H "Authorization: Bearer <token>" \
  -F "file=@urls.csv" \
  -F "format=png"
```

Poll for job status:

```bash
curl https://api.pixelperfectapi.net/api/v1/batch/jobs/<job_id> \
  -H "Authorization: Bearer <token>"
```

---

## Subscription Tiers

| Tier | Screenshots / Month | Batch URLs / Job | Concurrency |
|---|---|---|---|
| Free | 100 | Not available | 2 |
| Pro | 5,000 | 50 | 3 |
| Business | 50,000 | 200 | 5 |
| Premium | Unlimited | 1,000 | 5+ |

---

## Health Check

```bash
curl https://api.pixelperfectapi.net/health
```

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

## API Reference

Full interactive API documentation (Swagger UI) is available at:

```
https://api.pixelperfectapi.net/docs
```

OpenAPI schema:

```
https://api.pixelperfectapi.net/openapi.json
```

---

## Deployment

The API is deployed on **Render** using Docker with Playwright (Chromium headless), PostgreSQL, Stripe webhooks, and optional Cloudflare R2 storage.

---

## License

Copyright © 2026 [OneTechly](https://onetechlyambr19.blogspot.com/2024/11/peer-to-peer-peer-to-peer-p2p.html)  
All rights reserved.

This is proprietary software. Unauthorized copying, modification, or distribution is strictly prohibited.