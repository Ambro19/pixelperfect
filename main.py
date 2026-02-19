# backend/main.py
# ========================================
# PIXELPERFECT SCREENSHOT API - BACKEND
# ========================================
# Author: OneTechly
# Updated: February 2026 - PRODUCTION READY
#
# Key production fixes in this drop-in:
# ✅ Swagger servers list uses correct public URLs (no stale Render internal hostnames)
# ✅ CORS origins normalized for custom domain deployment
# ✅ Keeps your docs-safe security headers, WebP static, tier concurrency, and Playwright readiness
# ========================================

# =====================================================================
# WINDOWS FIX - MUST BE FIRST!
# =====================================================================
import sys
if sys.platform == "win32":
    import asyncio as _asyncio
    _asyncio.set_event_loop_policy(_asyncio.WindowsProactorEventLoopPolicy())

# =====================================================================
# Imports
# =====================================================================
import os
import time
import logging
import threading
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, AsyncGenerator, List

from dotenv import load_dotenv, find_dotenv
load_dotenv()
load_dotenv(dotenv_path=find_dotenv(".env.local"), override=True)
load_dotenv(dotenv_path=find_dotenv(".env"), override=False)

from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import ORJSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from sqlalchemy.orm import Session
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from pydantic import BaseModel, EmailStr

import jwt
from passlib.context import CryptContext

# Local imports
from email_utils import send_password_reset_email
from auth_utils import get_password_hash, verify_password
from subscription_sync import (
    sync_user_subscription_from_stripe,
    _apply_local_overdue_downgrade_if_possible,
)

from models import (
    User,
    Screenshot,
    Subscription,
    ApiKey,
    get_db,
    initialize_database,
    engine,
    get_tier_limits,
    reset_monthly_usage,
)
from db_migrations import run_startup_migrations
from auth_deps import get_current_user
from webhook_handler import handle_stripe_webhook

from api_key_system import (
    create_api_key_for_user,
    run_api_key_migration,
    validate_api_key,
)

# Screenshot service + endpoints
from screenshot_service import screenshot_service
from screenshot_endpoints import (
    capture_screenshot_endpoint,
    batch_screenshot_endpoint,
    regenerate_api_key_endpoint,
    ScreenshotRequest,
    BatchScreenshotRequest,
)

# History router
from history import router as history_router

# =====================================================================
# ✅ CRITICAL FIX: Custom StaticFiles with WebP Content-Type Support
# =====================================================================
import mimetypes

if ".webp" not in mimetypes.types_map:
    mimetypes.add_type("image/webp", ".webp")

class CustomStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)

        if isinstance(response, FileResponse):
            ext = Path(path).suffix.lower()
            mime_types = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".pdf": "application/pdf",
                ".gif": "image/gif",
                ".svg": "image/svg+xml",
            }
            if ext in mime_types:
                response.headers["Content-Type"] = mime_types[ext]
                response.media_type = mime_types[ext]

        return response

# =====================================================================
# CONFIG
# =====================================================================
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY env var is required.")

RESET_TOKEN_TTL_SECONDS = int(os.getenv("RESET_TOKEN_TTL_SECONDS", "3600"))
serializer = URLSafeTimedSerializer(SECRET_KEY)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./pixelperfect.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg2" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pixelperfect")
logger.setLevel(logging.INFO)

ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
IS_PROD = ENVIRONMENT == "production"

FRONTEND_URL = (os.getenv("FRONTEND_URL", "http://localhost:3000") or "").rstrip("/")
BACKEND_URL = (os.getenv("BACKEND_URL", "http://localhost:8000") or "").rstrip("/")

# Your canonical production API domain (custom domain you just verified)
PROD_API_DOMAIN = "https://api.pixelperfectapi.net"

# =====================================================================
# Stripe init (non-fatal)
# =====================================================================
stripe = None
try:
    import stripe as _stripe
    if os.getenv("STRIPE_SECRET_KEY"):
        _stripe.api_key = os.getenv("STRIPE_SECRET_KEY").strip()
        stripe = _stripe
except Exception as e:
    logger.warning("Stripe init failed (non-fatal): %s", e)
    stripe = None

# =====================================================================
# ✅ Option 2 - Tier concurrency (NO env var duplicates)
# =====================================================================
TIER_CONCURRENCY: Dict[str, int] = {
    "starter": 2,
    "pro": 3,
    "business": 5,
}

CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS = float(
    os.getenv("CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS", "5")
)

def _normalize_tier(raw: Optional[str]) -> str:
    t = (raw or "").strip().lower()
    if t in {"starter", "basic"}:
        return "starter"
    if t in {"pro", "premium"}:
        return "pro"
    if t in {"business", "enterprise"}:
        return "business"
    if t in {"free", ""}:
        return "starter"
    return t

def _tier_limit_for_user(user: User) -> int:
    tier = _normalize_tier(getattr(user, "subscription_tier", None))
    return int(TIER_CONCURRENCY.get(tier, TIER_CONCURRENCY["starter"]))

class _UserLimiter:
    __slots__ = ("sem", "limit", "active", "pending_limit")

    def __init__(self, limit: int):
        self.sem = asyncio.Semaphore(limit)
        self.limit = int(limit)
        self.active = 0
        self.pending_limit: Optional[int] = None

_USER_LIMITERS: Dict[int, _UserLimiter] = {}
_USER_LIMITERS_LOCK = asyncio.Lock()

async def _get_user_limiter(user_id: int, desired_limit: int) -> _UserLimiter:
    async with _USER_LIMITERS_LOCK:
        lim = _USER_LIMITERS.get(user_id)
        if lim is None:
            lim = _UserLimiter(desired_limit)
            _USER_LIMITERS[user_id] = lim
            return lim

        if int(desired_limit) != int(lim.limit):
            if lim.active == 0:
                lim = _UserLimiter(desired_limit)
                _USER_LIMITERS[user_id] = lim
            else:
                lim.pending_limit = int(desired_limit)
        return lim

async def enforce_tier_concurrency(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> AsyncGenerator[None, None]:
    user_id = int(getattr(current_user, "id", 0) or 0)
    if user_id <= 0:
        raise HTTPException(status_code=401, detail="Unauthorized")

    desired = _tier_limit_for_user(current_user)
    limiter = await _get_user_limiter(user_id, desired)

    try:
        await asyncio.wait_for(
            limiter.sem.acquire(),
            timeout=CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        tier = _normalize_tier(getattr(current_user, "subscription_tier", None))
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many concurrent screenshots for your plan (tier={tier}, limit={desired}). "
                f"Please retry in a moment or upgrade for higher concurrency."
            ),
            headers={"Retry-After": "1"},
        )

    limiter.active += 1
    try:
        yield
    finally:
        limiter.active -= 1
        limiter.sem.release()

        if limiter.active == 0 and limiter.pending_limit is not None:
            pending = limiter.pending_limit
            limiter.pending_limit = None
            async with _USER_LIMITERS_LOCK:
                _USER_LIMITERS[user_id] = _UserLimiter(pending)

# =====================================================================
# FastAPI app — servers list (Swagger base URL fix)
# =====================================================================
def _build_servers() -> List[Dict[str, str]]:
    """
    Swagger UI 'servers' should match real reachable public endpoints.
    Avoid hardcoding internal Render hostnames that change.
    """
    servers: List[Dict[str, str]] = []

    if BACKEND_URL:
        servers.append({"url": BACKEND_URL, "description": "Current environment"})

    # Always include your canonical custom production domain
    servers.append({"url": PROD_API_DOMAIN, "description": "Production (Custom Domain)"})

    # Local dev helper
    servers.append({"url": "http://localhost:8000", "description": "Local development"})

    # De-dupe by url, preserve order
    seen = set()
    unique = []
    for s in servers:
        u = (s.get("url") or "").rstrip("/")
        if u and u not in seen:
            seen.add(u)
            unique.append({"url": u, "description": s.get("description", "")})
    return unique

app = FastAPI(
    title="PixelPerfect Screenshot API",
    version="1.0.0",
    description="Professional Website Screenshot API with Playwright",
    default_response_class=ORJSONResponse,
    servers=_build_servers(),
)

# Include History Router
app.include_router(history_router)

# =====================================================================
# Screenshot Service Readiness
# =====================================================================
SCREENSHOT_READY: bool = False
SCREENSHOT_LAST_ERROR: Optional[str] = None
SCREENSHOT_LAST_ERROR_AT: Optional[str] = None

def _set_screenshot_ready(val: bool, err: Optional[str] = None):
    global SCREENSHOT_READY, SCREENSHOT_LAST_ERROR, SCREENSHOT_LAST_ERROR_AT
    SCREENSHOT_READY = bool(val)
    if err:
        SCREENSHOT_LAST_ERROR = str(err)
        SCREENSHOT_LAST_ERROR_AT = datetime.utcnow().isoformat()
    elif val:
        SCREENSHOT_LAST_ERROR = None
        SCREENSHOT_LAST_ERROR_AT = None

# =====================================================================
# Security headers middleware (docs-safe)
# =====================================================================
_DOCS_PREFIXES = ("/docs", "/redoc")
_DOCS_EXACT = {"/openapi.json"}

def _remove_header(headers, key: str) -> None:
    try:
        del headers[key]
    except KeyError:
        pass

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        *,
        csp: Optional[str] = None,
        hsts: bool = False,
        hsts_max_age: int = 31536000,
        referrer_policy: str = "no-referrer",
        x_frame_options: str = "DENY",
        server_header: Optional[str] = "PixelPerfect",
    ) -> None:
        super().__init__(app)
        self.csp = csp
        self.hsts = hsts
        self.hsts_max_age = int(hsts_max_age)
        self.referrer_policy = referrer_policy
        self.x_frame_options = x_frame_options
        self.server_header = server_header

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        path = request.url.path or "/"
        norm = path.rstrip("/")

        is_docs_path = (
            norm in _DOCS_EXACT
            or norm in _DOCS_PREFIXES
            or path.startswith(_DOCS_PREFIXES)
            or path.startswith("/docs/oauth2-redirect")
        )

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers["Referrer-Policy"] = self.referrer_policy
        response.headers.setdefault("X-XSS-Protection", "0")
        if self.server_header is not None:
            response.headers["Server"] = self.server_header

        if self.hsts and request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                f"max-age={self.hsts_max_age}; includeSubDomains"
            )

        if is_docs_path:
            _remove_header(response.headers, "Content-Security-Policy")
            _remove_header(response.headers, "X-Frame-Options")
            _remove_header(response.headers, "Cross-Origin-Opener-Policy")
            _remove_header(response.headers, "Cross-Origin-Embedder-Policy")
            _remove_header(response.headers, "Cross-Origin-Resource-Policy")
            return response

        response.headers["X-Frame-Options"] = self.x_frame_options
        if self.csp:
            response.headers["Content-Security-Policy"] = self.csp

        return response

DEV_CSP = None
PROD_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self' https://api.stripe.com; "
    "script-src 'self' https://js.stripe.com; "
    "frame-src https://js.stripe.com https://checkout.stripe.com; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
)

app.add_middleware(
    SecurityHeadersMiddleware,
    csp=(PROD_CSP if IS_PROD else DEV_CSP),
    hsts=IS_PROD,
    hsts_max_age=63072000,
    referrer_policy="no-referrer",
    x_frame_options="DENY",
    server_header="PixelPerfect",
)

# =====================================================================
# CORS
# =====================================================================
PUBLIC_ORIGINS = [
    "https://pixelperfectapi.net",
    "https://www.pixelperfectapi.net",
]
DEV_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://192.168.1.185:3000",
]

extra = (os.getenv("CORS_ORIGINS") or "").strip()
extra_list = [x.strip() for x in extra.split(",") if x.strip()]

# Also include FRONTEND_URL automatically (if set)
auto_frontend = [FRONTEND_URL] if FRONTEND_URL else []

allow_origins = list(dict.fromkeys(PUBLIC_ORIGINS + DEV_ORIGINS + extra_list + auto_frontend))

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "HEAD"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Type", "Content-Length"],
    max_age=3600,
)
logger.info("✅ CORS enabled for: %s (credentials: True)", allow_origins)

# =====================================================================
# Static screenshots mount
# =====================================================================
SCREENSHOTS_DIR = Path(__file__).resolve().parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)
app.mount("/screenshots", CustomStaticFiles(directory=str(SCREENSHOTS_DIR)), name="screenshots")
logger.info("✅ Screenshot static files mounted with WebP support")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)

# =====================================================================
# Auth helpers
# =====================================================================
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = dict(data)
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def canonical_account(user: User) -> Dict[str, Any]:
    return {
        "username": (user.username or "").strip(),
        "email": (user.email or "").strip().lower(),
    }

def ensure_stripe_customer_for_user(user: User, db: Session) -> None:
    if not stripe or not os.getenv("STRIPE_SECRET_KEY"):
        return
    if getattr(user, "stripe_customer_id", None):
        return
    email = (user.email or "").strip().lower()
    if not email:
        return
    try:
        created = stripe.Customer.create(
            email=email,
            name=(user.username or "").strip() or None,
            metadata={"app_user_id": str(user.id)},
        )
        user.stripe_customer_id = created["id"]
        db.commit()
        db.refresh(user)
    except Exception as e:
        logger.warning("Stripe customer creation skipped (non-fatal): %s", e)

# =====================================================================
# Pydantic models
# =====================================================================
class UserCreate(BaseModel):
    username: str
    email: str
    password: str

class UserResponse(BaseModel):
    id: int
    username: Optional[str] = None
    email: str
    created_at: Optional[datetime] = None
    class Config:
        from_attributes = True

class LoginJSON(BaseModel):
    username: str
    password: str

class ForgotPasswordIn(BaseModel):
    email: EmailStr

class ResetPasswordIn(BaseModel):
    token: str
    new_password: str

class BillingCheckoutIn(BaseModel):
    plan: str
    billing_cycle: str = "monthly"

# =====================================================================
# Startup & Shutdown
# =====================================================================
@app.on_event("startup")
async def on_startup():
    initialize_database()
    run_startup_migrations(engine)
    run_api_key_migration(engine)

    try:
        await screenshot_service.initialize()
        _set_screenshot_ready(True)
    except Exception as e:
        _set_screenshot_ready(False, err=e)
        # In production, do NOT crash the whole API if Playwright fails.
        if not IS_PROD:
            raise
        logger.exception("⚠️ Screenshot service init failed (non-fatal in production).")

    logger.info("============================================================")
    logger.info("PixelPerfect starting - ENV=%s DB=%s", ENVIRONMENT, DATABASE_URL)
    logger.info("Frontend URL: %s", FRONTEND_URL or "(unset)")
    logger.info("Backend URL: %s", BACKEND_URL or "(unset)")
    logger.info("Custom API Domain: %s", PROD_API_DOMAIN)
    logger.info("Stripe configured: %s", bool(stripe and os.getenv("STRIPE_SECRET_KEY")))
    logger.info("✅ API key system initialized")
    logger.info("📸 Screenshot service ready: %s", SCREENSHOT_READY)
    if SCREENSHOT_LAST_ERROR:
        logger.info("📸 Screenshot last error: %s", SCREENSHOT_LAST_ERROR)
    logger.info("✅ Tier concurrency enabled: %s", TIER_CONCURRENCY)
    logger.info("============================================================")

@app.on_event("shutdown")
async def on_shutdown():
    try:
        await screenshot_service.close()
    except Exception:
        logger.exception("Screenshot service close failed (ignored).")
    logger.info("✅ Screenshot service closed gracefully")

# =====================================================================
# Core routes
# =====================================================================
@app.get("/")
def root():
    return {
        "message": "PixelPerfect Screenshot API",
        "status": "running",
        "version": "1.0.0",
        "docs": "/docs",
        "ready": SCREENSHOT_READY,
    }

@app.get("/health")
def health():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "environment": ENVIRONMENT,
        "services": {
            "stripe": "configured" if os.getenv("STRIPE_SECRET_KEY") else "not_configured",
            "screenshot_service": "ready" if SCREENSHOT_READY else "not_ready",
        },
        "screenshot_service_error": SCREENSHOT_LAST_ERROR,
        "screenshot_service_error_at": SCREENSHOT_LAST_ERROR_AT,
        "tier_concurrency": TIER_CONCURRENCY,
    }

@app.head("/health")
def health_head():
    return Response(status_code=200)

@app.options("/{path:path}")
async def options_handler(path: str):
    return Response(status_code=200)

# =====================================================================
# Auth routes (UNCHANGED)
# =====================================================================
@app.post("/register")
def register(user: UserCreate, db: Session = Depends(get_db)):
    username = (user.username or "").strip()
    email = (user.email or "").strip().lower()

    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Username already exists.")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="Email already exists.")

    obj = User(
        username=username,
        email=email,
        hashed_password=get_password_hash(user.password),
        created_at=datetime.utcnow(),
        subscription_tier="free",
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)

    api_key = None
    try:
        api_key, _ = create_api_key_for_user(db, obj.id, "Default API Key")
        logger.info("✅ Created API key for new user %s", obj.id)
    except Exception as e:
        logger.warning("API key creation skipped: %s", e)

    try:
        ensure_stripe_customer_for_user(obj, db)
    except Exception:
        pass

    out = {"message": "User registered successfully.", "account": canonical_account(obj)}
    if api_key:
        out["api_key"] = api_key
    if getattr(obj, "stripe_customer_id", None):
        out["stripe_customer_id"] = obj.stripe_customer_id
    return out

@app.post("/token")
def token_login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    identifier = (form.username or "").strip()
    password_input = form.password or ""

    logger.info("🔐 Login attempt: username=%s", identifier)

    user = (
        db.query(User)
        .filter((User.username == identifier) | (User.email == identifier.lower()))
        .first()
    )

    if not user:
        logger.warning("❌ Login failed: user not found (username=%s)", identifier)
        raise HTTPException(status_code=401, detail="Incorrect username/email or password")

    if not verify_password(password_input, user.hashed_password):
        logger.warning("❌ Login failed: wrong password (username=%s)", identifier)
        raise HTTPException(status_code=401, detail="Incorrect username/email or password")

    try:
        ensure_stripe_customer_for_user(user, db)
    except Exception:
        pass

    token = create_access_token({"sub": user.username}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    logger.info("✅ Login successful: user=%s (%s)", user.username, user.email)

    return {"access_token": token, "token_type": "bearer", "user": canonical_account(user)}

@app.post("/token_json")
def token_login_json(req: LoginJSON, db: Session = Depends(get_db)):
    identifier = (req.username or "").strip()
    password_input = req.password or ""

    logger.info("🔐 JSON login attempt: username=%s", identifier)

    user = (
        db.query(User)
        .filter((User.username == identifier) | (User.email == identifier.lower()))
        .first()
    )

    if not user:
        logger.warning("❌ JSON login failed: user not found (username=%s)", identifier)
        raise HTTPException(status_code=401, detail="Incorrect username/email or password")

    if not verify_password(password_input, user.hashed_password):
        logger.warning("❌ JSON login failed: wrong password (username=%s)", identifier)
        raise HTTPException(status_code=401, detail="Incorrect username/email or password")

    try:
        ensure_stripe_customer_for_user(user, db)
    except Exception:
        pass

    token = create_access_token({"sub": user.username}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    logger.info("✅ JSON login successful: user=%s (%s)", user.username, user.email)

    return {"access_token": token, "token_type": "bearer", "user": canonical_account(user)}

@app.get("/users/me", response_model=UserResponse)
def read_users_me(current_user: User = Depends(get_current_user)):
    return current_user

@app.post("/auth/forgot-password")
def forgot_password(payload: ForgotPasswordIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if user:
        token = serializer.dumps({"email": payload.email})
        reset_link = f"{FRONTEND_URL}/reset?token={token}"
        try:
            send_password_reset_email(payload.email, reset_link)
        except Exception:
            logger.exception("Failed to send reset email")
    return {"ok": True}

@app.post("/auth/reset-password")
def reset_password(payload: ResetPasswordIn, db: Session = Depends(get_db)):
    try:
        data = serializer.loads(payload.token, max_age=RESET_TOKEN_TTL_SECONDS)
        email = data.get("email")
    except SignatureExpired:
        raise HTTPException(status_code=400, detail="Reset link expired")
    except BadSignature:
        raise HTTPException(status_code=400, detail="Reset link invalid")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=400, detail="Reset link invalid")

    user.hashed_password = get_password_hash(payload.new_password)
    db.commit()
    return {"ok": True}

# =====================================================================
# API Key Management
# =====================================================================
@app.get("/api/keys/current")
async def get_current_api_key(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    api_key_record = db.query(ApiKey).filter(
        ApiKey.user_id == current_user.id,
        ApiKey.is_active == True
    ).first()

    if not api_key_record:
        try:
            api_key, api_key_record = create_api_key_for_user(
                db=db,
                user_id=current_user.id,
                name="Default API Key"
            )
            logger.info("✅ Created API key for user %s", current_user.id)
            return {
                "api_key": api_key,
                "key_prefix": api_key_record.key_prefix,
                "created_at": api_key_record.created_at.isoformat() if api_key_record.created_at else None,
                "last_used_at": api_key_record.last_used_at.isoformat() if api_key_record.last_used_at else None,
                "message": "⚠️ Save this key securely. It won't be shown again!"
            }
        except Exception as e:
            logger.error("❌ API key creation failed for user %s: %s", current_user.id, e)
            raise HTTPException(status_code=500, detail="Failed to create API key")

    return {
        "key_prefix": api_key_record.key_prefix,
        "created_at": api_key_record.created_at.isoformat() if api_key_record.created_at else None,
        "last_used_at": api_key_record.last_used_at.isoformat() if api_key_record.last_used_at else None,
        "name": api_key_record.name,
        "message": "API key already exists. For security, the full key cannot be displayed."
    }

@app.post("/api/keys/regenerate")
async def regenerate_api_key(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await regenerate_api_key_endpoint(current_user, db)

# =====================================================================
# Screenshot API Endpoints
# ✅ NOW ENFORCED BY TIER CONCURRENCY
# =====================================================================
@app.post("/api/v1/screenshot")
async def capture_screenshot(
    request: ScreenshotRequest,
    _guard: None = Depends(enforce_tier_concurrency),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await capture_screenshot_endpoint(request, current_user, db)

@app.post("/api/v1/batch/submit")
async def batch_screenshot(
    request: BatchScreenshotRequest,
    _guard: None = Depends(enforce_tier_concurrency),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await batch_screenshot_endpoint(request, current_user, db)

# =====================================================================
# Stripe webhook + billing + subscription_status (UNCHANGED)
# =====================================================================
_IDEMP_STORE: Dict[str, float] = {}
_IDEMP_TTL_SEC = 24 * 3600
_IDEMP_LOCK = threading.Lock()

def _idemp_seen(event_id: str) -> bool:
    now = time.time()
    with _IDEMP_LOCK:
        for k, ts in list(_IDEMP_STORE.items()):
            if now - ts > _IDEMP_TTL_SEC:
                _IDEMP_STORE.pop(k, None)
        if event_id in _IDEMP_STORE:
            return True
        _IDEMP_STORE[event_id] = now
        return False

@app.post("/webhook/stripe")
async def stripe_webhook_endpoint(request: Request):
    if not stripe or not os.getenv("STRIPE_SECRET_KEY"):
        raise HTTPException(status_code=503, detail="Stripe is not configured")

    secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    payload = await request.body()
    sig = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig, secret=secret)
    except Exception as e:
        logger.warning("Stripe webhook signature verification failed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid signature")

    if not event or not event.get("id"):
        raise HTTPException(status_code=400, detail="Invalid event payload")

    if _idemp_seen(event["id"]):
        return {"status": "ok", "duplicate": True}

    request.state.verified_event = event
    return await handle_stripe_webhook(request)

def _lookup_key(plan: str, billing_cycle: str) -> Optional[str]:
    plan = (plan or "").lower().strip()
    billing_cycle = (billing_cycle or "monthly").lower().strip()

    if billing_cycle == "yearly":
        k = os.getenv(f"STRIPE_{plan.upper()}_LOOKUP_KEY_YEARLY")
        if k:
            return k.strip()

    k = os.getenv(f"STRIPE_{plan.upper()}_LOOKUP_KEY_MONTHLY") or os.getenv(f"STRIPE_{plan.upper()}_LOOKUP_KEY")
    return k.strip() if k else None

@app.post("/billing/create_checkout_session")
def create_checkout_session(
    payload: BillingCheckoutIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not stripe or not os.getenv("STRIPE_SECRET_KEY"):
        raise HTTPException(status_code=503, detail="Stripe is not configured")

    plan = (payload.plan or "").lower().strip()
    if plan not in {"pro", "business", "premium"}:
        raise HTTPException(status_code=400, detail="Invalid plan. Must be: pro, business, or premium")

    billing_cycle = (payload.billing_cycle or "monthly").lower().strip()
    if billing_cycle not in {"monthly", "yearly"}:
        raise HTTPException(status_code=400, detail="Invalid billing_cycle. Must be: monthly or yearly")

    ensure_stripe_customer_for_user(current_user, db)
    customer_id = getattr(current_user, "stripe_customer_id", None)
    if not customer_id:
        raise HTTPException(status_code=400, detail="User missing Stripe customer ID. Please contact support.")

    lookup_key = _lookup_key(plan, billing_cycle)
    if not lookup_key:
        logger.error("Missing Stripe lookup key for %s (%s)", plan, billing_cycle)
        raise HTTPException(
            status_code=500,
            detail=f"Missing Stripe configuration for {plan} ({billing_cycle}). "
                   f"Please set STRIPE_{plan.upper()}_LOOKUP_KEY_MONTHLY and optionally _YEARLY in environment.",
        )

    try:
        prices = stripe.Price.list(lookup_keys=[lookup_key], limit=1)
        if not prices.data:
            logger.error("No Stripe Price found for lookup_key=%s", lookup_key)
            raise HTTPException(
                status_code=500,
                detail=f"No Stripe Price found for lookup_key={lookup_key}. Please check Stripe Dashboard."
            )

        price_id = prices.data[0].id
        logger.info("✅ Found Stripe Price: %s for %s (%s)", price_id, plan, billing_cycle)

        success_url = f"{FRONTEND_URL}/dashboard?checkout=success"
        cancel_url = f"{FRONTEND_URL}/pricing?checkout=cancel"

        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            allow_promotion_codes=True,
            client_reference_id=str(current_user.id),
            metadata={
                "app_user_id": str(current_user.id),
                "plan": plan,
                "billing_cycle": billing_cycle,
            },
        )

        logger.info("✅ Stripe Checkout Session created: %s for user %s", session.id, current_user.id)
        return {"url": session.url, "id": session.id}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("❌ Checkout session create failed for user %s", current_user.id)
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

@app.get("/subscription_status")
def subscription_status(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        _apply_local_overdue_downgrade_if_possible(current_user, db)
    except Exception as e:
        logger.warning("Local downgrade check failed: %s", e)

    if request.query_params.get("sync") == "1":
        try:
            sync_user_subscription_from_stripe(current_user, db)
        except Exception as e:
            logger.warning("Stripe sync failed: %s", e)

    tier = (getattr(current_user, "subscription_tier", "free") or "free").lower()
    tier_limits = get_tier_limits(tier)

    usage = {
        "screenshots": getattr(current_user, "usage_screenshots", 0) or 0,
        "batch_requests": getattr(current_user, "usage_batch_requests", 0) or 0,
        "api_calls": getattr(current_user, "usage_api_calls", 0) or 0,
    }

    next_reset = getattr(current_user, "usage_reset_at", None)

    response = {
        "tier": tier,
        "usage": usage,
        "limits": tier_limits,
        "account": canonical_account(current_user),
        "tier_concurrency_limit": _tier_limit_for_user(current_user),
    }

    if next_reset:
        response["next_reset"] = next_reset.isoformat() if isinstance(next_reset, datetime) else next_reset

    return response

# =====================================================================
# Optional SPA mount
# =====================================================================
FRONTEND_BUILD = Path(__file__).resolve().parents[1] / "frontend" / "build"
if FRONTEND_BUILD.exists():
    app.mount("/_spa", StaticFiles(directory=str(FRONTEND_BUILD), html=True), name="spa")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_catch_all(full_path: str):
        if full_path.startswith(("api/", "health", "token", "register", "webhook/", "screenshots/")):
            raise HTTPException(status_code=404, detail="Not found")

        index_file = FRONTEND_BUILD / "index.html"
        if index_file.exists():
            return HTMLResponse(index_file.read_text(encoding="utf-8"))

        raise HTTPException(status_code=404, detail="Frontend not built")

# =====================================================================
# Entry point (local dev only)
# =====================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)


# # ===============================================
# # backend/main.py
# # ========================================
# # PIXELPERFECT SCREENSHOT API - BACKEND
# # ========================================
# # Author: OneTechly
# # Updated: February 2026 - PRODUCTION READY
# #
# # ✅ FIXES APPLIED (existing):
# # - WebP Content-Type header fix (custom StaticFiles)
# # - CORS credentials for Firefox
# # - Better login error messages
# # - Added `servers` to FastAPI → Swagger UI uses correct public base URL
# # - Docs paths forcibly strip CSP/XFO/COOP/COEP (Swagger always renders)
# # - Starlette MutableHeaders safe header delete helper
# #
# # ✅ NEW (Option 2 Tier Concurrency):
# # - Per-user concurrency limiter using asyncio semaphores
# # - Starter=2, Pro=3, Business=5 (no env var duplicates needed)
# # - Implemented as a yield dependency on screenshot routes
# # ========================================

# # =====================================================================
# # WINDOWS FIX - MUST BE FIRST!
# # =====================================================================
# import sys
# if sys.platform == "win32":
#     import asyncio as _asyncio
#     _asyncio.set_event_loop_policy(_asyncio.WindowsProactorEventLoopPolicy())

# # =====================================================================
# # Imports
# # =====================================================================
# import os
# import time
# import logging
# import threading
# import asyncio
# from pathlib import Path
# from datetime import datetime, timedelta
# from typing import Optional, Dict, Any, AsyncGenerator

# from dotenv import load_dotenv, find_dotenv
# load_dotenv()
# load_dotenv(dotenv_path=find_dotenv(".env.local"), override=True)
# load_dotenv(dotenv_path=find_dotenv(".env"), override=False)

# from fastapi import FastAPI, HTTPException, Depends, Request, Response
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
# from fastapi.responses import ORJSONResponse, HTMLResponse, FileResponse
# from fastapi.staticfiles import StaticFiles
# from starlette.middleware.base import BaseHTTPMiddleware
# from starlette.types import ASGIApp

# from sqlalchemy.orm import Session
# from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
# from pydantic import BaseModel, EmailStr

# import jwt
# from passlib.context import CryptContext

# # Local imports
# from email_utils import send_password_reset_email
# from auth_utils import get_password_hash, verify_password
# from subscription_sync import sync_user_subscription_from_stripe, _apply_local_overdue_downgrade_if_possible

# from models import (
#     User,
#     Screenshot,
#     Subscription,
#     ApiKey,
#     get_db,
#     initialize_database,
#     engine,
#     get_tier_limits,
#     reset_monthly_usage,
# )
# from db_migrations import run_startup_migrations
# from auth_deps import get_current_user
# from webhook_handler import handle_stripe_webhook

# from api_key_system import (
#     create_api_key_for_user,
#     run_api_key_migration,
#     validate_api_key,
# )

# # Screenshot service + endpoints
# from screenshot_service import screenshot_service
# from screenshot_endpoints import (
#     capture_screenshot_endpoint,
#     batch_screenshot_endpoint,
#     regenerate_api_key_endpoint,
#     ScreenshotRequest,
#     BatchScreenshotRequest,
# )

# # History router
# from history import router as history_router

# # =====================================================================
# # ✅ CRITICAL FIX: Custom StaticFiles with WebP Content-Type Support
# # =====================================================================
# import mimetypes

# if ".webp" not in mimetypes.types_map:
#     mimetypes.add_type("image/webp", ".webp")
#     logging.info("✅ Registered .webp MIME type: image/webp")

# class CustomStaticFiles(StaticFiles):
#     async def get_response(self, path: str, scope):
#         response = await super().get_response(path, scope)

#         if isinstance(response, FileResponse):
#             ext = Path(path).suffix.lower()
#             mime_types = {
#                 ".png": "image/png",
#                 ".jpg": "image/jpeg",
#                 ".jpeg": "image/jpeg",
#                 ".webp": "image/webp",
#                 ".pdf": "application/pdf",
#                 ".gif": "image/gif",
#                 ".svg": "image/svg+xml",
#             }
#             if ext in mime_types:
#                 response.headers["Content-Type"] = mime_types[ext]
#                 response.media_type = mime_types[ext]

#         return response

# # =====================================================================
# # CONFIG
# # =====================================================================
# SECRET_KEY = os.getenv("SECRET_KEY")
# if not SECRET_KEY:
#     raise RuntimeError("SECRET_KEY env var is required.")

# RESET_TOKEN_TTL_SECONDS = int(os.getenv("RESET_TOKEN_TTL_SECONDS", "3600"))
# serializer = URLSafeTimedSerializer(SECRET_KEY)

# DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./pixelperfect.db")
# if DATABASE_URL.startswith("postgres://"):
#     DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
# elif DATABASE_URL.startswith("postgresql://") and "+psycopg2" not in DATABASE_URL:
#     DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

# logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger("pixelperfect")
# logger.setLevel(logging.INFO)

# ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
# IS_PROD = ENVIRONMENT == "production"
# FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
# BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")

# # =====================================================================
# # Stripe init (non-fatal)
# # =====================================================================
# stripe = None
# try:
#     import stripe as _stripe
#     if os.getenv("STRIPE_SECRET_KEY"):
#         _stripe.api_key = os.getenv("STRIPE_SECRET_KEY").strip()
#         stripe = _stripe
# except Exception as e:
#     logger.warning("Stripe init failed (non-fatal): %s", e)
#     stripe = None

# # =====================================================================
# # ✅ Option 2 - Tier concurrency (NO env var duplicates)
# # =====================================================================
# # You can tweak these numbers safely anytime.
# TIER_CONCURRENCY: Dict[str, int] = {
#     "starter": 2,
#     "pro": 3,
#     "business": 5,
# }

# # How long a request will wait to acquire a slot before returning 429.
# # (This is a global behavior knob; NOT a tier setting.)
# CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS = float(os.getenv("CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS", "5"))

# def _normalize_tier(raw: Optional[str]) -> str:
#     t = (raw or "").strip().lower()
#     # Common aliases
#     if t in {"starter", "basic"}:
#         return "starter"
#     if t in {"pro", "premium"}:
#         return "pro"
#     if t in {"business", "enterprise"}:
#         return "business"
#     # Many systems use "free" in DB — treat as starter unless you want free=1
#     if t in {"free", ""}:
#         return "starter"
#     return t

# def _tier_limit_for_user(user: User) -> int:
#     tier = _normalize_tier(getattr(user, "subscription_tier", None))
#     return int(TIER_CONCURRENCY.get(tier, TIER_CONCURRENCY["starter"]))

# class _UserLimiter:
#     """
#     Per-user semaphore with safe resizing when tier changes.
#     """
#     __slots__ = ("sem", "limit", "active", "pending_limit")

#     def __init__(self, limit: int):
#         self.sem = asyncio.Semaphore(limit)
#         self.limit = int(limit)
#         self.active = 0
#         self.pending_limit: Optional[int] = None

# # Global in-process store (works best with a single Uvicorn worker)
# _USER_LIMITERS: Dict[int, _UserLimiter] = {}
# _USER_LIMITERS_LOCK = asyncio.Lock()

# async def _get_user_limiter(user_id: int, desired_limit: int) -> _UserLimiter:
#     async with _USER_LIMITERS_LOCK:
#         lim = _USER_LIMITERS.get(user_id)
#         if lim is None:
#             lim = _UserLimiter(desired_limit)
#             _USER_LIMITERS[user_id] = lim
#             return lim

#         # If tier changed, only resize safely when no active jobs
#         if int(desired_limit) != int(lim.limit):
#             if lim.active == 0:
#                 lim = _UserLimiter(desired_limit)
#                 _USER_LIMITERS[user_id] = lim
#             else:
#                 lim.pending_limit = int(desired_limit)
#         return lim

# async def enforce_tier_concurrency(
#     request: Request,
#     current_user: User = Depends(get_current_user),
# ) -> AsyncGenerator[None, None]:
#     """
#     Yield dependency:
#     - Acquire user's tier semaphore
#     - Proceed
#     - Always release (even on error)
#     """
#     user_id = int(getattr(current_user, "id", 0) or 0)
#     if user_id <= 0:
#         # Should never happen if auth is correct
#         raise HTTPException(status_code=401, detail="Unauthorized")

#     desired = _tier_limit_for_user(current_user)
#     limiter = await _get_user_limiter(user_id, desired)

#     # Acquire a slot with timeout
#     try:
#         await asyncio.wait_for(limiter.sem.acquire(), timeout=CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS)
#     except asyncio.TimeoutError:
#         tier = _normalize_tier(getattr(current_user, "subscription_tier", None))
#         raise HTTPException(
#             status_code=429,
#             detail=(
#                 f"Too many concurrent screenshots for your plan (tier={tier}, limit={desired}). "
#                 f"Please retry in a moment or upgrade for higher concurrency."
#             ),
#             headers={"Retry-After": "1"},
#         )

#     limiter.active += 1
#     try:
#         yield
#     finally:
#         # Always release
#         limiter.active -= 1
#         limiter.sem.release()

#         # Apply pending tier resize only when user becomes idle
#         if limiter.active == 0 and limiter.pending_limit is not None:
#             pending = limiter.pending_limit
#             limiter.pending_limit = None
#             async with _USER_LIMITERS_LOCK:
#                 # Replace limiter with new size
#                 _USER_LIMITERS[user_id] = _UserLimiter(pending)

# # =====================================================================
# # FastAPI app — servers list (Swagger base URL fix)
# # =====================================================================
# app = FastAPI(
#     title="PixelPerfect Screenshot API",
#     version="1.0.0",
#     description="Professional Website Screenshot API with Playwright",
#     default_response_class=ORJSONResponse,
#     servers=[
#         {"url": BACKEND_URL, "description": "Current environment"},
#         {"url": "https://pixelperfect-api-mi7t.onrender.com", "description": "Production (Render)"},
#         {"url": "https://api.pixelperfectapi.net", "description": "Production (Custom Domain)"},
#         {"url": "http://localhost:8000", "description": "Local development"},
#     ],
# )

# # Include History Router
# app.include_router(history_router)

# # =====================================================================
# # Screenshot Service Readiness
# # =====================================================================
# SCREENSHOT_READY: bool = False
# SCREENSHOT_LAST_ERROR: Optional[str] = None
# SCREENSHOT_LAST_ERROR_AT: Optional[str] = None

# def _set_screenshot_ready(val: bool, err: Optional[str] = None):
#     global SCREENSHOT_READY, SCREENSHOT_LAST_ERROR, SCREENSHOT_LAST_ERROR_AT
#     SCREENSHOT_READY = bool(val)
#     if err:
#         SCREENSHOT_LAST_ERROR = str(err)
#         SCREENSHOT_LAST_ERROR_AT = datetime.utcnow().isoformat()
#     elif val:
#         SCREENSHOT_LAST_ERROR = None
#         SCREENSHOT_LAST_ERROR_AT = None

# # =====================================================================
# # Security headers middleware (docs-safe)
# # =====================================================================
# _DOCS_PREFIXES = ("/docs", "/redoc")
# _DOCS_EXACT = {"/openapi.json"}

# def _remove_header(headers, key: str) -> None:
#     try:
#         del headers[key]
#     except KeyError:
#         pass

# class SecurityHeadersMiddleware(BaseHTTPMiddleware):
#     def __init__(
#         self,
#         app: ASGIApp,
#         *,
#         csp: Optional[str] = None,
#         hsts: bool = False,
#         hsts_max_age: int = 31536000,
#         referrer_policy: str = "no-referrer",
#         x_frame_options: str = "DENY",
#         server_header: Optional[str] = "PixelPerfect",
#     ) -> None:
#         super().__init__(app)
#         self.csp = csp
#         self.hsts = hsts
#         self.hsts_max_age = int(hsts_max_age)
#         self.referrer_policy = referrer_policy
#         self.x_frame_options = x_frame_options
#         self.server_header = server_header

#     async def dispatch(self, request: Request, call_next):
#         response = await call_next(request)

#         path = request.url.path or "/"
#         norm = path.rstrip("/")

#         is_docs_path = (
#             norm in _DOCS_EXACT
#             or norm in _DOCS_PREFIXES
#             or path.startswith(_DOCS_PREFIXES)
#             or path.startswith("/docs/oauth2-redirect")
#         )

#         response.headers.setdefault("X-Content-Type-Options", "nosniff")
#         response.headers["Referrer-Policy"] = self.referrer_policy
#         response.headers.setdefault("X-XSS-Protection", "0")
#         if self.server_header is not None:
#             response.headers["Server"] = self.server_header

#         if self.hsts and request.url.scheme == "https":
#             response.headers["Strict-Transport-Security"] = (
#                 f"max-age={self.hsts_max_age}; includeSubDomains"
#             )

#         if is_docs_path:
#             _remove_header(response.headers, "Content-Security-Policy")
#             _remove_header(response.headers, "X-Frame-Options")
#             _remove_header(response.headers, "Cross-Origin-Opener-Policy")
#             _remove_header(response.headers, "Cross-Origin-Embedder-Policy")
#             _remove_header(response.headers, "Cross-Origin-Resource-Policy")
#             return response

#         response.headers["X-Frame-Options"] = self.x_frame_options
#         if self.csp:
#             response.headers["Content-Security-Policy"] = self.csp

#         return response

# DEV_CSP = None
# PROD_CSP = (
#     "default-src 'self'; "
#     "img-src 'self' data: blob:; "
#     "style-src 'self' 'unsafe-inline'; "
#     "connect-src 'self' https://api.stripe.com; "
#     "script-src 'self' https://js.stripe.com; "
#     "frame-src https://js.stripe.com https://checkout.stripe.com; "
#     "frame-ancestors 'none'; "
#     "base-uri 'none'; "
# )

# app.add_middleware(
#     SecurityHeadersMiddleware,
#     csp=(PROD_CSP if IS_PROD else DEV_CSP),
#     hsts=IS_PROD,
#     hsts_max_age=63072000,
#     referrer_policy="no-referrer",
#     x_frame_options="DENY",
#     server_header="PixelPerfect",
# )

# # =====================================================================
# # CORS
# # =====================================================================
# PUBLIC_ORIGINS = [
#     "https://pixelperfectapi.net",
#     "https://www.pixelperfectapi.net",
# ]
# DEV_ORIGINS = [
#     "http://localhost:3000",
#     "http://127.0.0.1:3000",
#     "http://192.168.1.185:3000",
# ]

# extra = (os.getenv("CORS_ORIGINS") or "").strip()
# extra_list = [x.strip() for x in extra.split(",") if x.strip()]
# allow_origins = list(dict.fromkeys(
#     PUBLIC_ORIGINS + DEV_ORIGINS + extra_list + ([FRONTEND_URL] if FRONTEND_URL else [])
# ))

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=allow_origins,
#     allow_credentials=True,
#     allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "HEAD"],
#     allow_headers=["*"],
#     expose_headers=["Content-Disposition", "Content-Type", "Content-Length"],
#     max_age=3600,
# )
# logger.info("✅ CORS enabled for: %s (credentials: True)", allow_origins)

# # =====================================================================
# # Static screenshots mount
# # =====================================================================
# SCREENSHOTS_DIR = Path(__file__).resolve().parent / "screenshots"
# SCREENSHOTS_DIR.mkdir(exist_ok=True)
# app.mount("/screenshots", CustomStaticFiles(directory=str(SCREENSHOTS_DIR)), name="screenshots")
# logger.info("✅ Screenshot static files mounted with WebP support")

# @app.get("/favicon.ico", include_in_schema=False)
# def favicon():
#     return Response(status_code=204)

# # =====================================================================
# # Auth helpers
# # =====================================================================
# ALGORITHM = os.getenv("ALGORITHM", "HS256")
# ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
# oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
# pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
#     to_encode = dict(data)
#     expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
#     to_encode.update({"exp": expire})
#     return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# def canonical_account(user: User) -> Dict[str, Any]:
#     return {
#         "username": (user.username or "").strip(),
#         "email": (user.email or "").strip().lower(),
#     }

# def ensure_stripe_customer_for_user(user: User, db: Session) -> None:
#     if not stripe or not os.getenv("STRIPE_SECRET_KEY"):
#         return
#     if getattr(user, "stripe_customer_id", None):
#         return
#     email = (user.email or "").strip().lower()
#     if not email:
#         return
#     try:
#         created = stripe.Customer.create(
#             email=email,
#             name=(user.username or "").strip() or None,
#             metadata={"app_user_id": str(user.id)},
#         )
#         user.stripe_customer_id = created["id"]
#         db.commit()
#         db.refresh(user)
#     except Exception as e:
#         logger.warning("Stripe customer creation skipped (non-fatal): %s", e)

# # =====================================================================
# # Pydantic models
# # =====================================================================
# class UserCreate(BaseModel):
#     username: str
#     email: str
#     password: str

# class UserResponse(BaseModel):
#     id: int
#     username: Optional[str] = None
#     email: str
#     created_at: Optional[datetime] = None
#     class Config:
#         from_attributes = True

# class LoginJSON(BaseModel):
#     username: str
#     password: str

# class ForgotPasswordIn(BaseModel):
#     email: EmailStr

# class ResetPasswordIn(BaseModel):
#     token: str
#     new_password: str

# class BillingCheckoutIn(BaseModel):
#     plan: str
#     billing_cycle: str = "monthly"

# # =====================================================================
# # Startup & Shutdown
# # =====================================================================
# @app.on_event("startup")
# async def on_startup():
#     initialize_database()
#     run_startup_migrations(engine)
#     run_api_key_migration(engine)

#     try:
#         await screenshot_service.initialize()
#         _set_screenshot_ready(True)
#     except Exception as e:
#         _set_screenshot_ready(False, err=e)
#         if not IS_PROD:
#             raise
#         logger.exception("⚠️ Screenshot service init failed (non-fatal in production).")

#     logger.info("============================================================")
#     logger.info("PixelPerfect starting - ENV=%s DB=%s", ENVIRONMENT, DATABASE_URL)
#     logger.info("Stripe configured: %s", bool(stripe and os.getenv("STRIPE_SECRET_KEY")))
#     logger.info("✅ API key system initialized")
#     logger.info("📸 Screenshot service ready: %s", SCREENSHOT_READY)
#     if SCREENSHOT_LAST_ERROR:
#         logger.info("📸 Screenshot last error: %s", SCREENSHOT_LAST_ERROR)
#     logger.info("✅ Tier concurrency enabled: %s", TIER_CONCURRENCY)
#     logger.info("============================================================")

# @app.on_event("shutdown")
# async def on_shutdown():
#     try:
#         await screenshot_service.close()
#     except Exception:
#         logger.exception("Screenshot service close failed (ignored).")
#     logger.info("✅ Screenshot service closed gracefully")

# # =====================================================================
# # Core routes
# # =====================================================================
# @app.get("/")
# def root():
#     return {"message": "PixelPerfect Screenshot API", "status": "running", "version": "1.0.0"}

# @app.get("/health")
# def health():
#     return {
#         "status": "healthy",
#         "timestamp": datetime.utcnow().isoformat(),
#         "environment": ENVIRONMENT,
#         "services": {
#             "stripe": "configured" if os.getenv("STRIPE_SECRET_KEY") else "not_configured",
#             "screenshot_service": "ready" if SCREENSHOT_READY else "not_ready",
#         },
#         "screenshot_service_error": SCREENSHOT_LAST_ERROR,
#         "screenshot_service_error_at": SCREENSHOT_LAST_ERROR_AT,
#         "tier_concurrency": TIER_CONCURRENCY,
#     }

# @app.head("/health")
# def health_head():
#     return Response(status_code=200)

# @app.options("/{path:path}")
# async def options_handler(path: str):
#     return Response(status_code=200)

# # =====================================================================
# # Auth routes (UNCHANGED)
# # =====================================================================
# @app.post("/register")
# def register(user: UserCreate, db: Session = Depends(get_db)):
#     username = (user.username or "").strip()
#     email = (user.email or "").strip().lower()

#     if db.query(User).filter(User.username == username).first():
#         raise HTTPException(status_code=400, detail="Username already exists.")
#     if db.query(User).filter(User.email == email).first():
#         raise HTTPException(status_code=400, detail="Email already exists.")

#     obj = User(
#         username=username,
#         email=email,
#         hashed_password=get_password_hash(user.password),
#         created_at=datetime.utcnow(),
#         subscription_tier="free",
#     )
#     db.add(obj)
#     db.commit()
#     db.refresh(obj)

#     api_key = None
#     try:
#         api_key, _ = create_api_key_for_user(db, obj.id, "Default API Key")
#         logger.info("✅ Created API key for new user %s", obj.id)
#     except Exception as e:
#         logger.warning("API key creation skipped: %s", e)

#     try:
#         ensure_stripe_customer_for_user(obj, db)
#     except Exception:
#         pass

#     out = {"message": "User registered successfully.", "account": canonical_account(obj)}
#     if api_key:
#         out["api_key"] = api_key
#     if getattr(obj, "stripe_customer_id", None):
#         out["stripe_customer_id"] = obj.stripe_customer_id
#     return out

# @app.post("/token")
# def token_login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
#     identifier = (form.username or "").strip()
#     password_input = form.password or ""

#     logger.info("🔐 Login attempt: username=%s", identifier)

#     user = (
#         db.query(User)
#         .filter((User.username == identifier) | (User.email == identifier.lower()))
#         .first()
#     )

#     if not user:
#         logger.warning("❌ Login failed: user not found (username=%s)", identifier)
#         raise HTTPException(status_code=401, detail="Incorrect username/email or password")

#     if not verify_password(password_input, user.hashed_password):
#         logger.warning("❌ Login failed: wrong password (username=%s)", identifier)
#         raise HTTPException(status_code=401, detail="Incorrect username/email or password")

#     try:
#         ensure_stripe_customer_for_user(user, db)
#     except Exception:
#         pass

#     token = create_access_token({"sub": user.username}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
#     logger.info("✅ Login successful: user=%s (%s)", user.username, user.email)

#     return {"access_token": token, "token_type": "bearer", "user": canonical_account(user)}

# @app.post("/token_json")
# def token_login_json(req: LoginJSON, db: Session = Depends(get_db)):
#     identifier = (req.username or "").strip()
#     password_input = req.password or ""

#     logger.info("🔐 JSON login attempt: username=%s", identifier)

#     user = (
#         db.query(User)
#         .filter((User.username == identifier) | (User.email == identifier.lower()))
#         .first()
#     )

#     if not user:
#         logger.warning("❌ JSON login failed: user not found (username=%s)", identifier)
#         raise HTTPException(status_code=401, detail="Incorrect username/email or password")

#     if not verify_password(password_input, user.hashed_password):
#         logger.warning("❌ JSON login failed: wrong password (username=%s)", identifier)
#         raise HTTPException(status_code=401, detail="Incorrect username/email or password")

#     try:
#         ensure_stripe_customer_for_user(user, db)
#     except Exception:
#         pass

#     token = create_access_token({"sub": user.username}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
#     logger.info("✅ JSON login successful: user=%s (%s)", user.username, user.email)

#     return {"access_token": token, "token_type": "bearer", "user": canonical_account(user)}

# @app.get("/users/me", response_model=UserResponse)
# def read_users_me(current_user: User = Depends(get_current_user)):
#     return current_user

# @app.post("/auth/forgot-password")
# def forgot_password(payload: ForgotPasswordIn, db: Session = Depends(get_db)):
#     user = db.query(User).filter(User.email == payload.email).first()
#     if user:
#         token = serializer.dumps({"email": payload.email})
#         reset_link = f"{FRONTEND_URL}/reset?token={token}"
#         try:
#             send_password_reset_email(payload.email, reset_link)
#         except Exception:
#             logger.exception("Failed to send reset email")
#     return {"ok": True}

# @app.post("/auth/reset-password")
# def reset_password(payload: ResetPasswordIn, db: Session = Depends(get_db)):
#     try:
#         data = serializer.loads(payload.token, max_age=RESET_TOKEN_TTL_SECONDS)
#         email = data.get("email")
#     except SignatureExpired:
#         raise HTTPException(status_code=400, detail="Reset link expired")
#     except BadSignature:
#         raise HTTPException(status_code=400, detail="Reset link invalid")

#     user = db.query(User).filter(User.email == email).first()
#     if not user:
#         raise HTTPException(status_code=400, detail="Reset link invalid")

#     user.hashed_password = get_password_hash(payload.new_password)
#     db.commit()
#     return {"ok": True}

# # =====================================================================
# # API Key Management
# # =====================================================================
# @app.get("/api/keys/current")
# async def get_current_api_key(
#     db: Session = Depends(get_db),
#     current_user: User = Depends(get_current_user)
# ):
#     api_key_record = db.query(ApiKey).filter(
#         ApiKey.user_id == current_user.id,
#         ApiKey.is_active == True
#     ).first()

#     if not api_key_record:
#         try:
#             api_key, api_key_record = create_api_key_for_user(
#                 db=db,
#                 user_id=current_user.id,
#                 name="Default API Key"
#             )
#             logger.info("✅ Created API key for user %s", current_user.id)
#             return {
#                 "api_key": api_key,
#                 "key_prefix": api_key_record.key_prefix,
#                 "created_at": api_key_record.created_at.isoformat() if api_key_record.created_at else None,
#                 "last_used_at": api_key_record.last_used_at.isoformat() if api_key_record.last_used_at else None,
#                 "message": "⚠️ Save this key securely. It won't be shown again!"
#             }
#         except Exception as e:
#             logger.error("❌ API key creation failed for user %s: %s", current_user.id, e)
#             raise HTTPException(status_code=500, detail="Failed to create API key")

#     return {
#         "key_prefix": api_key_record.key_prefix,
#         "created_at": api_key_record.created_at.isoformat() if api_key_record.created_at else None,
#         "last_used_at": api_key_record.last_used_at.isoformat() if api_key_record.last_used_at else None,
#         "name": api_key_record.name,
#         "message": "API key already exists. For security, the full key cannot be displayed."
#     }

# @app.post("/api/keys/regenerate")
# async def regenerate_api_key(
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     return await regenerate_api_key_endpoint(current_user, db)

# # =====================================================================
# # Screenshot API Endpoints
# # ✅ NOW ENFORCED BY TIER CONCURRENCY
# # =====================================================================
# @app.post("/api/v1/screenshot")
# async def capture_screenshot(
#     request: ScreenshotRequest,
#     _guard: None = Depends(enforce_tier_concurrency),  # ✅ concurrency guard
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     return await capture_screenshot_endpoint(request, current_user, db)

# @app.post("/api/v1/batch/submit")
# async def batch_screenshot(
#     request: BatchScreenshotRequest,
#     _guard: None = Depends(enforce_tier_concurrency),  # ✅ concurrency guard
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     return await batch_screenshot_endpoint(request, current_user, db)

# # =====================================================================
# # Stripe webhook + billing + subscription_status (UNCHANGED)
# # =====================================================================
# _IDEMP_STORE: Dict[str, float] = {}
# _IDEMP_TTL_SEC = 24 * 3600
# _IDEMP_LOCK = threading.Lock()

# def _idemp_seen(event_id: str) -> bool:
#     now = time.time()
#     with _IDEMP_LOCK:
#         for k, ts in list(_IDEMP_STORE.items()):
#             if now - ts > _IDEMP_TTL_SEC:
#                 _IDEMP_STORE.pop(k, None)
#         if event_id in _IDEMP_STORE:
#             return True
#         _IDEMP_STORE[event_id] = now
#         return False

# @app.post("/webhook/stripe")
# async def stripe_webhook_endpoint(request: Request):
#     if not stripe or not os.getenv("STRIPE_SECRET_KEY"):
#         raise HTTPException(status_code=503, detail="Stripe is not configured")

#     secret = os.getenv("STRIPE_WEBHOOK_SECRET")
#     if not secret:
#         raise HTTPException(status_code=500, detail="Webhook secret not configured")

#     payload = await request.body()
#     sig = request.headers.get("stripe-signature")

#     try:
#         event = stripe.Webhook.construct_event(payload=payload, sig_header=sig, secret=secret)
#     except Exception as e:
#         logger.warning("Stripe webhook signature verification failed: %s", e)
#         raise HTTPException(status_code=400, detail="Invalid signature")

#     if not event or not event.get("id"):
#         raise HTTPException(status_code=400, detail="Invalid event payload")

#     if _idemp_seen(event["id"]):
#         return {"status": "ok", "duplicate": True}

#     request.state.verified_event = event
#     return await handle_stripe_webhook(request)

# def _lookup_key(plan: str, billing_cycle: str) -> Optional[str]:
#     plan = (plan or "").lower().strip()
#     billing_cycle = (billing_cycle or "monthly").lower().strip()

#     if billing_cycle == "yearly":
#         k = os.getenv(f"STRIPE_{plan.upper()}_LOOKUP_KEY_YEARLY")
#         if k:
#             return k.strip()

#     k = os.getenv(f"STRIPE_{plan.upper()}_LOOKUP_KEY_MONTHLY") or os.getenv(f"STRIPE_{plan.upper()}_LOOKUP_KEY")
#     return k.strip() if k else None

# @app.post("/billing/create_checkout_session")
# def create_checkout_session(
#     payload: BillingCheckoutIn,
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     if not stripe or not os.getenv("STRIPE_SECRET_KEY"):
#         raise HTTPException(status_code=503, detail="Stripe is not configured")

#     plan = (payload.plan or "").lower().strip()
#     if plan not in {"pro", "business", "premium"}:
#         raise HTTPException(status_code=400, detail="Invalid plan. Must be: pro, business, or premium")

#     billing_cycle = (payload.billing_cycle or "monthly").lower().strip()
#     if billing_cycle not in {"monthly", "yearly"}:
#         raise HTTPException(status_code=400, detail="Invalid billing_cycle. Must be: monthly or yearly")

#     ensure_stripe_customer_for_user(current_user, db)
#     customer_id = getattr(current_user, "stripe_customer_id", None)
#     if not customer_id:
#         raise HTTPException(status_code=400, detail="User missing Stripe customer ID. Please contact support.")

#     lookup_key = _lookup_key(plan, billing_cycle)
#     if not lookup_key:
#         logger.error("Missing Stripe lookup key for %s (%s)", plan, billing_cycle)
#         raise HTTPException(
#             status_code=500,
#             detail=f"Missing Stripe configuration for {plan} ({billing_cycle}). "
#                    f"Please set STRIPE_{plan.upper()}_LOOKUP_KEY_MONTHLY and optionally _YEARLY in environment.",
#         )

#     try:
#         prices = stripe.Price.list(lookup_keys=[lookup_key], limit=1)
#         if not prices.data:
#             logger.error("No Stripe Price found for lookup_key=%s", lookup_key)
#             raise HTTPException(
#                 status_code=500,
#                 detail=f"No Stripe Price found for lookup_key={lookup_key}. Please check Stripe Dashboard."
#             )

#         price_id = prices.data[0].id
#         logger.info("✅ Found Stripe Price: %s for %s (%s)", price_id, plan, billing_cycle)

#         success_url = f"{FRONTEND_URL}/dashboard?checkout=success"
#         cancel_url = f"{FRONTEND_URL}/pricing?checkout=cancel"

#         session = stripe.checkout.Session.create(
#             mode="subscription",
#             customer=customer_id,
#             line_items=[{"price": price_id, "quantity": 1}],
#             success_url=success_url,
#             cancel_url=cancel_url,
#             allow_promotion_codes=True,
#             client_reference_id=str(current_user.id),
#             metadata={
#                 "app_user_id": str(current_user.id),
#                 "plan": plan,
#                 "billing_cycle": billing_cycle,
#             },
#         )

#         logger.info("✅ Stripe Checkout Session created: %s for user %s", session.id, current_user.id)
#         return {"url": session.url, "id": session.id}

#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.exception("❌ Checkout session create failed for user %s", current_user.id)
#         raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

# @app.get("/subscription_status")
# def subscription_status(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
#     try:
#         _apply_local_overdue_downgrade_if_possible(current_user, db)
#     except Exception as e:
#         logger.warning("Local downgrade check failed: %s", e)

#     if request.query_params.get("sync") == "1":
#         try:
#             sync_user_subscription_from_stripe(current_user, db)
#         except Exception as e:
#             logger.warning("Stripe sync failed: %s", e)

#     tier = (getattr(current_user, "subscription_tier", "free") or "free").lower()
#     tier_limits = get_tier_limits(tier)

#     usage = {
#         "screenshots": getattr(current_user, "usage_screenshots", 0) or 0,
#         "batch_requests": getattr(current_user, "usage_batch_requests", 0) or 0,
#         "api_calls": getattr(current_user, "usage_api_calls", 0) or 0,
#     }

#     next_reset = getattr(current_user, "usage_reset_at", None)

#     response = {
#         "tier": tier,
#         "usage": usage,
#         "limits": tier_limits,
#         "account": canonical_account(current_user),
#         "tier_concurrency_limit": _tier_limit_for_user(current_user),
#     }

#     if next_reset:
#         response["next_reset"] = next_reset.isoformat() if isinstance(next_reset, datetime) else next_reset

#     return response

# # =====================================================================
# # Optional SPA mount
# # =====================================================================
# FRONTEND_BUILD = Path(__file__).resolve().parents[1] / "frontend" / "build"
# if FRONTEND_BUILD.exists():
#     app.mount("/_spa", StaticFiles(directory=str(FRONTEND_BUILD), html=True), name="spa")

#     @app.get("/{full_path:path}", include_in_schema=False)
#     def spa_catch_all(full_path: str):
#         if full_path.startswith(("api/", "health", "token", "register", "webhook/", "screenshots/")):
#             raise HTTPException(status_code=404, detail="Not found")

#         index_file = FRONTEND_BUILD / "index.html"
#         if index_file.exists():
#             return HTMLResponse(index_file.read_text(encoding="utf-8"))

#         raise HTTPException(status_code=404, detail="Frontend not built")

# # =====================================================================
# # Entry point (local dev only)
# # =====================================================================
# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)

# # ----------------------------------------------------------------------------
# # END of main.py
# # ----------------------------------------------------------------------------

