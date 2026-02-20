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

