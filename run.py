#!/usr/bin/env python3
"""
PixelPerfect Screenshot API - Production-Safe Runner
=====================================================
Fixes:
✅ Loads .env / .env.production BEFORE reading ENVIRONMENT (prevents env mismatch)
✅ Windows event loop policy for Playwright subprocess support
✅ Local + Render friendly
✅ Optional dev reload mode

Usage:
    python run.py              # Uses ENVIRONMENT from env files / OS env
    python run.py --reload     # Forces reload
    python run.py --prod       # Forces production env file load (.env.production)
"""

import os
import sys
import logging
from pathlib import Path

# =====================================================================
# CRITICAL: WINDOWS EVENT LOOP POLICY - MUST BE FIRST!
# =====================================================================
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    print("✅ Windows event loop policy set (Proactor) - Playwright subprocess support enabled")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------
# Load environment variables EARLY (fixes ENV mismatch)
# ---------------------------------------------------------------------
def _load_env_files():
    """
    Load .env files in the correct order:
    1) OS env vars always win (Render sets these)
    2) Local dev: load .env if present
    3) If forced prod OR ENVIRONMENT/APP_ENV says production, load .env.production (override=True)
    """
    env_file = ROOT / ".env"
    prod_file = ROOT / ".env.production"

    # If python-dotenv isn't installed, we won't crash — but we'll warn.
    try:
        from dotenv import load_dotenv
    except Exception:
        print("⚠️ python-dotenv not installed. Env files won't auto-load.")
        print("   Fix: pip install python-dotenv")
        return

    # Load base .env (local dev defaults)
    if env_file.exists():
        load_dotenv(env_file, override=False)

    # Decide if we should load production file
    forced_prod = ("--prod" in sys.argv) or ("-p" in sys.argv)
    env_hint = (os.getenv("ENVIRONMENT") or os.getenv("APP_ENV") or "").strip().lower()
    should_load_prod = forced_prod or (env_hint == "production")

    if should_load_prod and prod_file.exists():
        # Override because prod file should win over .env for prod runs
        load_dotenv(prod_file, override=True)

_load_env_files()

# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("pixelperfect")


def _db_driver_from_env() -> str:
    url = (os.getenv("DATABASE_URL") or "").lower()
    if url.startswith("postgres://") or url.startswith("postgresql://"):
        return "postgres"
    if url.startswith("sqlite://"):
        return "sqlite"
    return "unknown"


def _local_ip_hint() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _check_files():
    required = ["main.py", "models.py"]
    optional = ["screenshot_service.py", "screenshot_endpoints.py"]

    for p in required + optional:
        if (ROOT / p).exists():
            log.info("✅ Found %s", p)
        else:
            log.warning("⚠️  Missing %s", p)

    missing = [p for p in required if not (ROOT / p).exists()]
    if missing:
        log.error("❌ Missing required files: %s", missing)
        sys.exit(1)


def main():
    # Prefer ENVIRONMENT as the single source of truth
    env = (os.getenv("ENVIRONMENT") or os.getenv("APP_ENV") or "development").lower()

    # Normalize: if you set APP_ENV only, ensure ENVIRONMENT matches
    os.environ["ENVIRONMENT"] = env

    port = int(os.getenv("PORT", "8000"))

    reload_arg = ("--reload" in sys.argv) or ("-r" in sys.argv)
    reload = (env != "production") or reload_arg

    print("=" * 80)
    print("🚀 Starting PixelPerfect Screenshot API")
    print(f"🔧 Environment: {env}")
    print(f"🔧 Mode: {'Development (reload enabled)' if reload else 'Production'}")
    print(f"🗄️  Database: {_db_driver_from_env()}")
    print(f"🪟 Platform: {sys.platform}")
    print("=" * 80)

    _check_files()

    # Validate import early for nicer errors
    try:
        from main import app  # noqa: F401
        log.info("✅ Application imported successfully")
    except Exception:
        log.exception("❌ Failed to import FastAPI application")
        log.error("💡 Hint: Check SECRET_KEY / DATABASE_URL / imports")
        sys.exit(1)

    try:
        import uvicorn
    except ImportError:
        log.error("❌ uvicorn not installed. Run: pip install uvicorn")
        sys.exit(1)

    local_ip = _local_ip_hint()
    log.info("📡 Server: http://0.0.0.0:%d", port)
    log.info("📱 Local:  http://localhost:%d", port)
    log.info("🌐 LAN:    http://%s:%d", local_ip, port)

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=reload,
        log_level=os.getenv("UVICORN_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()


