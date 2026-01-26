#!/usr/bin/env python3
"""
PixelPerfect Screenshot API - Production-Safe Runner
=====================================================
- Correct Windows event loop policy for Playwright subprocess support
- Render + local friendly
- Optional dev reload mode

Usage:
    python run.py              # Production-ish (no reload unless ENV != production)
    python run.py --reload     # Dev mode (reload)

Env:
    ENVIRONMENT=production|development
    PORT=8000
    LOG_LEVEL=INFO
"""

import os
import sys
import logging
from pathlib import Path

# ============================================================================
# CRITICAL: WINDOWS EVENT LOOP POLICY - MUST BE FIRST!
# Playwright needs subprocess support on Windows.
# SelectorPolicy breaks subprocesses -> NotImplementedError
# ============================================================================
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    print("✅ Windows event loop policy set (Proactor) - Playwright subprocess support enabled")

# ----------------------------------------------------------------------------
# Ensure project root is importable
# ----------------------------------------------------------------------------
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------
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
    env = os.getenv("ENVIRONMENT", "production").lower()
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

# =========================================

# #!/usr/bin/env python3
# """
# PixelPerfect Screenshot API - Production-Safe Runner
# =====================================================
# Combines:
# - Windows event loop fix (for Playwright subprocess support)
# - Production deployment logic (for Render.com)
# - Development auto-reload support

# Usage:
#     python run.py              # Production mode
#     python run.py --reload     # Development mode with auto-reload
    
# Environment Variables:
#     ENVIRONMENT: production | development (default: production)
#     PORT: Server port (default: 8000)
#     LOG_LEVEL: Logging level (default: INFO)
#     DATABASE_URL: Database connection string
# """

# import os
# import sys
# import logging
# from pathlib import Path

# # ============================================================================
# # CRITICAL: WINDOWS EVENT LOOP FIX - MUST BE FIRST!
# # ============================================================================
# # This MUST run before any asyncio/uvicorn imports
# # Required for Playwright on Windows (subprocess support)
# # ============================================================================
# if sys.platform == 'win32':
#     import asyncio
#     asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
#     print("✅ Windows event loop policy set (Playwright subprocess support enabled)")

# # ============================================================================
# # NOW we can import everything else
# # ============================================================================

# # Ensure project root is importable
# ROOT = Path(__file__).parent
# sys.path.insert(0, str(ROOT))

# # Setup logging
# logging.basicConfig(
#     level=os.getenv("LOG_LEVEL", "INFO"),
#     format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
# )
# log = logging.getLogger("pixelperfect")


# def _db_driver_from_env() -> str:
#     """Detect database driver from DATABASE_URL"""
#     url = (os.getenv("DATABASE_URL") or "").lower()
#     if url.startswith("postgres://") or url.startswith("postgresql://"):
#         return "postgres"
#     if url.startswith("sqlite://"):
#         return "sqlite"
#     return "unknown"


# def _local_ip_hint() -> str:
#     """Get local IP for LAN access"""
#     import socket
#     try:
#         s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
#         s.connect(("8.8.8.8", 80))
#         ip = s.getsockname()[0]
#         s.close()
#         return ip
#     except Exception:
#         return "127.0.0.1"


# def _check_files():
#     """Verify required files exist"""
#     required = [
#         "main.py",
#         "models.py",
#         "screenshot_service.py",
#         "screenshot_endpoints.py",
#     ]
    
#     for p in required:
#         if (ROOT / p).exists():
#             log.info("✅ Found %s", p)
#         else:
#             log.warning("⚠️  Missing %s (may be optional)", p)
    
#     # Check critical files
#     critical = ["main.py", "models.py"]
#     missing = [p for p in critical if not (ROOT / p).exists()]
    
#     if missing:
#         log.error("❌ Missing required files: %s", missing)
#         sys.exit(1)


# def main():
#     """Main entry point"""
#     # Get configuration from environment
#     env = os.getenv("ENVIRONMENT", "production").lower()
#     port = int(os.getenv("PORT", "8000"))
    
#     # Check if --reload flag is present in command line
#     reload_arg = "--reload" in sys.argv or "-r" in sys.argv
    
#     # Only use reload in development OR if explicitly requested
#     reload = (env != "production") or reload_arg
    
#     # Banner
#     print("=" * 80)
#     print("🚀 Starting PixelPerfect Screenshot API")
#     print(f"🔧 Environment: {env}")
#     print(f"🔧 Mode: {'Development (reload enabled)' if reload else 'Production'}")
#     print(f"🗄️  Database: {_db_driver_from_env()}")
#     print(f"🪟 Platform: {sys.platform}")
#     if sys.platform == 'win32':
#         print("✅ Windows subprocess support: Enabled")
#     print("=" * 80)
    
#     # Verify required files
#     _check_files()
    
#     # Try to import app (validates configuration)
#     try:
#         from main import app  # noqa: F401
#         log.info("✅ Application imported successfully")
#     except Exception:
#         log.exception("❌ Failed to import FastAPI application")
#         log.error("💡 Hint: Check your DATABASE_URL and SECRET_KEY environment variables")
#         sys.exit(1)
    
#     # Import uvicorn
#     try:
#         import uvicorn
#     except ImportError:
#         log.error("❌ uvicorn not installed. Run: pip install uvicorn")
#         sys.exit(1)
    
#     # Server info
#     local_ip = _local_ip_hint()
#     log.info("📡 Server: http://0.0.0.0:%d (Render-compatible)", port)
#     log.info("📱 Local: http://localhost:%d", port)
#     log.info("🌐 LAN: http://%s:%d", local_ip, port)
    
#     # Start server
#     uvicorn.run(
#         "main:app",
#         host="0.0.0.0",
#         port=port,
#         reload=reload,
#         log_level=os.getenv("UVICORN_LOG_LEVEL", "info"),
#     )


# if __name__ == "__main__":
#     main()
