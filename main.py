# ========================================
# main.py - PixelPerfect Screenshot API
# ========================================
# Production-ready FastAPI application
# Author: OneTechly
# Created: January 2026
# Updated: January 2026

from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple
import re, time, socket, logging, jwt, threading, os
from collections import defaultdict, deque

from dotenv import load_dotenv, find_dotenv
load_dotenv()
load_dotenv(dotenv_path=find_dotenv(".env.local"), override=True)
load_dotenv(dotenv_path=find_dotenv(".env"), override=False)

from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import ORJSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from passlib.context import CryptContext
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from sqlalchemy.orm import Session
from sqlalchemy import delete as sqla_delete
from sqlalchemy.exc import OperationalError
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from pydantic import BaseModel, EmailStr

# Local imports
from email_utils import send_password_reset_email
from auth_utils import get_password_hash, verify_password
from subscription_sync import sync_user_subscription_from_stripe, _apply_local_overdue_downgrade_if_possible

# ============================================================================
# CONFIGURATION
# ============================================================================

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY env var is required. "
        'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
    )

RESET_TOKEN_TTL_SECONDS = int(os.getenv("RESET_TOKEN_TTL_SECONDS", "3600"))
serializer = URLSafeTimedSerializer(SECRET_KEY)

# Database URL
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./pixelperfect.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg2" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pixelperfect")
logger.setLevel(logging.INFO)

driver = "sqlite" if DATABASE_URL.startswith("sqlite") else "postgres"
logger.info(f"✅ Config OK — using database driver: {driver}")

# Environment
APP_ENV = os.getenv("APP_ENV", os.getenv("ENV", "development")).lower()
IS_PROD = APP_ENV == "production"
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
FILE_RETENTION_DAYS = int(os.getenv("FILE_RETENTION_DAYS", "30"))

# ============================================================================
# DATABASE & MODELS
# ============================================================================

from models import (
    User,
    Screenshot,
    Subscription,
    get_db,
    initialize_database,
    engine,
    get_tier_limits,
    check_usage_limits,
    increment_usage,
    reset_monthly_usage,
)
from db_migrations import run_startup_migrations
from auth_deps import get_current_user
from webhook_handler import handle_stripe_webhook

# ============================================================================
# API KEY SYSTEM IMPORTS
# ============================================================================

from api_key_system import create_api_key_for_user, run_api_key_migration

# ============================================================================
# FILE STORAGE - DEFINED EARLY FOR ROUTER IMPORTS
# ============================================================================

SCREENSHOTS_DIR = Path(__file__).resolve().parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)
logger.info("📁 Screenshots directory: %s", SCREENSHOTS_DIR)

# ============================================================================
# IMPORT ROUTERS
# ============================================================================

# Payment router
from payment import router as payment_router

# Screenshot router
try:
    from routers.screenshot import router as screenshot_router
    logger.info("✅ Screenshot router imported successfully")
except ImportError as e:
    logger.error(f"❌ Screenshot router import failed: {e}")
    screenshot_router = None

# Pricing router
try:
    from routers.pricing import router as pricing_router
    logger.info("✅ Pricing router imported successfully")
except ImportError as e:
    logger.warning(f"⚠️ Pricing router not found: {e}")
    pricing_router = None

# Batch router
try:
    from routers.batch import router as batch_router
    logger.info("✅ Batch router imported successfully")
except ImportError as e:
    logger.warning(f"⚠️ Batch router not found: {e}")
    batch_router = None

# Activity router
try:
    from routers.activity import router as activity_router
    logger.info("✅ Activity router imported successfully")
except ImportError as e:
    logger.warning(f"⚠️ Activity router not found: {e}")
    activity_router = None

# API Keys router
try:
    from routers.api_keys import router as api_keys_router
    logger.info("✅ API Keys router imported successfully")
except ImportError as e:
    logger.warning(f"⚠️ API Keys router not found: {e}")
    api_keys_router = None

# ============================================================================
# STRIPE CONFIGURATION
# ============================================================================

stripe = None
try:
    import stripe as _stripe

    if os.getenv("STRIPE_SECRET_KEY"):
        _stripe.api_key = os.getenv("STRIPE_SECRET_KEY").strip()
        stripe = _stripe
        
        # Exclude Stripe from proxy
        os.environ.setdefault("NO_PROXY", "")
        current_no_proxy = os.environ.get("NO_PROXY", "")
        stripe_domains = "api.stripe.com,files.stripe.com,checkout.stripe.com"
        
        if stripe_domains not in current_no_proxy:
            os.environ["NO_PROXY"] = f"{current_no_proxy},{stripe_domains}" if current_no_proxy else stripe_domains
            logger.info("✅ Stripe domains excluded from proxy: %s", stripe_domains)
            
except Exception as e:
    logger.warning("⚠️ Stripe initialization issue: %s", e)
    stripe = None

# ============================================================================
# FASTAPI APP
# ============================================================================

app = FastAPI(
    title="PixelPerfect Screenshot API",
    version="1.0.0",
    description="Professional Website Screenshot API with Playwright",
    default_response_class=ORJSONResponse,
)

# ============================================================================
# MIDDLEWARE - Security Headers
# ============================================================================

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
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers["X-Frame-Options"] = self.x_frame_options
        response.headers["Referrer-Policy"] = self.referrer_policy
        response.headers.setdefault("X-XSS-Protection", "0")
        
        if self.server_header is not None:
            response.headers["Server"] = self.server_header
            
        if self.hsts and request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = f"max-age={self.hsts_max_age}; includeSubDomains"
            
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
    "frame-src https://js.stripe.com; "
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

# ============================================================================
# MIDDLEWARE - Rate Limiting
# ============================================================================

class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self.now = time.time
        self.buckets: Dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()
        self.enabled = (os.getenv("RATE_LIMIT_ENABLED", "true").lower() in {"1", "true", "yes", "on"})
        self.default_window = 60
        self.default_max = int(os.getenv("RATE_LIMIT_FREE_TIER", "120"))
        self.auth_window = 60
        self.auth_max = 10

    def key_for(self, request: Request) -> tuple[str, int, int]:
        ip = (request.client.host if request.client else "unknown")
        path = request.url.path
        
        if path.startswith(("/token", "/register", "/webhook/stripe")):
            return (f"AUTH:{ip}", self.auth_window, self.auth_max)
        
        return (f"GEN:{ip}", self.default_window, self.default_max)

    async def dispatch(self, request: Request, call_next):
        if not self.enabled:
            return await call_next(request)
            
        key, window, limit = self.key_for(request)
        now = self.now()
        
        with self.lock:
            q = self.buckets[key]
            while q and now - q[0] > window:
                q.popleft()
                
            if len(q) >= limit:
                retry_after = max(1, int(window - (now - q[0])))
                return ORJSONResponse(
                    {"detail": "Too Many Requests"},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
            q.append(now)
            
        return await call_next(request)


app.add_middleware(RateLimitMiddleware)

# ============================================================================
# MIDDLEWARE - CORS
# ============================================================================

PUBLIC_ORIGINS = [
    "https://pixelperfectapi.net",
    "https://www.pixelperfectapi.net",
    "https://api.pixelperfectapi.net",
]

DEV_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://192.168.1.185:3000",
]

if ENVIRONMENT != "production":
    allow_origins = PUBLIC_ORIGINS + DEV_ORIGINS + ([FRONTEND_URL] if FRONTEND_URL else [])
else:
    allow_origins = PUBLIC_ORIGINS + ([FRONTEND_URL] if FRONTEND_URL else [])

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in allow_origins if o],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Type", "Content-Length"],
)

logger.info("✅ CORS enabled for origins: %s", allow_origins)

# ============================================================================
# MOUNT STATIC FILES - SCREENSHOTS
# ============================================================================

# Mount screenshots directory for public access
app.mount("/screenshots", StaticFiles(directory=str(SCREENSHOTS_DIR)), name="screenshots")
logger.info("✅ Mounted /screenshots static directory")

# ============================================================================
# AUTH HELPERS
# ============================================================================

ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = dict(data)
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_user(db: Session, username: str) -> Optional[User]:
    return db.query(User).filter(User.username == username).first()


def canonical_account(user: User) -> Dict[str, Any]:
    return {
        "username": (user.username or "").strip(),
        "email": (user.email or "").strip().lower(),
    }


def check_internet() -> bool:
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=3)
        return True
    except OSError:
        return False

# ============================================================================
# STRIPE HELPERS
# ============================================================================

def ensure_stripe_customer_for_user(user: User, db: Session) -> None:
    """Ensure user has Stripe customer ID (graceful - doesn't fail if Stripe is down)"""
    if not stripe or not os.getenv("STRIPE_SECRET_KEY"):
        return

    if getattr(user, "stripe_customer_id", None):
        return

    email = (user.email or "").strip().lower()
    username = (user.username or "").strip() or None

    if not email:
        return

    try:
        # Try to find existing customer
        customer = None
        try:
            found = stripe.Customer.search(query=f"email:'{email}'", limit=1)
            if getattr(found, "data", []):
                customer = found.data[0]
        except Exception:
            pass

        if customer:
            user.stripe_customer_id = customer["id"]
            db.commit()
            db.refresh(user)
            logger.info("🔄 Linked existing Stripe customer %s", customer["id"])
            return

        # Create new customer
        created = stripe.Customer.create(
            email=email,
            name=username,
            metadata={"app_user_id": str(user.id)},
        )
        user.stripe_customer_id = created["id"]
        db.commit()
        db.refresh(user)
        logger.info("✅ Created Stripe customer %s", created["id"])

    except Exception as e:
        logger.warning("⚠️ Stripe customer creation skipped (non-fatal): %s", e)


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

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


class Token(BaseModel):
    access_token: str
    token_type: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class LoginJSON(BaseModel):
    username: str
    password: str


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    token: str
    new_password: str


class CancelRequest(BaseModel):
    at_period_end: Optional[bool] = True

# ============================================================================
# STRIPE WEBHOOK IDEMPOTENCY
# ============================================================================

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

# ============================================================================
# STARTUP TASKS
# ============================================================================

@app.on_event("startup")
async def on_startup():
    """Application startup tasks"""
    initialize_database()
    run_startup_migrations(engine)
    
    # ✅ NEW: Run API key migration
    run_api_key_migration(engine)

    logger.info("=" * 60)
    logger.info("🚀 PixelPerfect Screenshot API Starting")
    logger.info("=" * 60)
    logger.info("📝 Environment: %s", ENVIRONMENT)
    logger.info("📁 Screenshots directory: %s", SCREENSHOTS_DIR)
    logger.info("🗄️ Database: %s", "PostgreSQL" if "postgres" in DATABASE_URL else "SQLite")
    logger.info("💳 Stripe configured: %s", bool(stripe and os.getenv("STRIPE_SECRET_KEY")))
    logger.info("=" * 60)
    logger.info("✅ Backend started successfully")
    logger.info("=" * 60)

# ============================================================================
# ROUTES - Core
# ============================================================================

@app.get("/")
def root():
    return {
        "message": "PixelPerfect Screenshot API",
        "status": "running",
        "version": "1.0.0",
        "features": ["screenshots", "batch", "webhooks", "payments"],
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "environment": ENVIRONMENT,
        "services": {
            "stripe": "configured" if os.getenv("STRIPE_SECRET_KEY") else "not_configured",
            "playwright": "available",
        },
    }


@app.head("/health")
def health_head():
    return Response(status_code=200)

# ============================================================================
# ROUTES - Authentication
# ============================================================================

@app.post("/register")
def register(user: UserCreate, db: Session = Depends(get_db)):
    logger.info(f"🔵 REGISTRATION - Username: {user.username}, Email: {user.email}")

    username = (user.username or "").strip()
    email = (user.email or "").strip().lower()

    # Validation
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Username already exists.")

    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="Email already exists.")

    # Create user
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
    
    # ✅ NEW: Create API key for new user
    api_key = None
    try:
        api_key, _ = create_api_key_for_user(db, obj.id, "Default API Key")
        logger.info(f"🔑 API key created for user {username}")
    except Exception as e:
        logger.warning(f"⚠️ API key creation skipped: {e}")
    
    logger.info(f"✅ User created: {username} (ID: {obj.id})")

    # Try to create Stripe customer (graceful)
    stripe_customer_id = None
    if stripe:
        try:
            ensure_stripe_customer_for_user(obj, db)
            stripe_customer_id = obj.stripe_customer_id
        except Exception as e:
            logger.warning(f"⚠️ Stripe customer creation skipped: {e}")

    response_data = {
        "message": "User registered successfully.",
        "account": canonical_account(obj),
        "stripe_customer_id": stripe_customer_id,
    }
    
    # ✅ NEW: Return API key ONCE on registration
    if api_key:
        response_data["api_key"] = api_key
    
    return response_data


@app.post("/token")
def token_login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    username_input = (form.username or "").strip()
    password_input = form.password or ""

    user = db.query(User).filter(User.username == username_input).first()
    if not user or not verify_password(password_input, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    # Try to ensure Stripe customer (graceful)
    try:
        ensure_stripe_customer_for_user(user, db)
    except Exception as e:
        logger.warning(f"⚠️ Stripe customer link skipped: {e}")

    token = create_access_token(
        {"sub": user.username},
        timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": canonical_account(user),
        "must_change_password": bool(getattr(user, "must_change_password", False)),
    }


@app.post("/token_json")
def token_login_json(req: LoginJSON, db: Session = Depends(get_db)):
    username_input = (req.username or "").strip()
    password_input = req.password or ""

    user = db.query(User).filter(User.username == username_input).first()
    if not user or not verify_password(password_input, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    try:
        ensure_stripe_customer_for_user(user, db)
    except Exception as e:
        logger.warning(f"⚠️ Stripe customer link skipped: {e}")

    token = create_access_token(
        {"sub": user.username},
        timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": canonical_account(user),
        "must_change_password": bool(getattr(user, "must_change_password", False)),
    }


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
        except Exception as e:
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


@app.post("/user/change_password")
def change_password(
    req: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(req.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    
    if not req.new_password or len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    
    current_user.hashed_password = get_password_hash(req.new_password)
    try:
        current_user.must_change_password = False
    except Exception:
        pass
    db.commit()
    db.refresh(current_user)
    logger.info("🔑 Password changed for user %s", current_user.username)
    return {"status": "ok"}


@app.delete("/user/delete-account")
def delete_account(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = int(current_user.id)
    email = (current_user.email or "unknown@unknown.com")

    # Cancel Stripe subscriptions
    try:
        if stripe and getattr(current_user, "stripe_customer_id", None):
            subs = stripe.Subscription.list(customer=current_user.stripe_customer_id, limit=100)
            for sub in getattr(subs, "data", []):
                try:
                    stripe.Subscription.delete(sub.id)
                except Exception:
                    pass
    except Exception:
        pass

    # Delete database records
    try:
        db.execute(sqla_delete(Screenshot).where(Screenshot.user_id == uid))
        db.execute(sqla_delete(Subscription).where(Subscription.user_id == uid))
        db.execute(sqla_delete(User).where(User.id == uid))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"DB delete failed for user {uid}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete account")

    # Delete user files
    try:
        for p in SCREENSHOTS_DIR.glob(f"*{uid}*"):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass

    return {
        "message": "Account deleted successfully.",
        "deleted_at": datetime.utcnow().isoformat(),
        "user_email": email,
    }

# ============================================================================
# ROUTES - Stripe Webhook
# ============================================================================

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
        logger.warning(f"Stripe webhook signature verification failed: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    if not event or not event.get("id"):
        raise HTTPException(status_code=400, detail="Invalid event payload")

    if _idemp_seen(event["id"]):
        logger.info(f"Stripe webhook duplicate event {event['id']} ignored")
        return {"status": "ok", "duplicate": True}

    request.state.verified_event = event
    result = await handle_stripe_webhook(request) if hasattr(handle_stripe_webhook, "__call__") else {"status": "ok"}
    return result

# ============================================================================
# ROUTES - Subscription Management
# ============================================================================

def _latest_subscription(db: Session, user_id: int) -> Optional[Subscription]:
    try:
        return (
            db.query(Subscription)
            .filter(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc())
            .first()
        )
    except Exception:
        return None


@app.post("/subscription/cancel")
def cancel_subscription(
    req: CancelRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if (current_user.subscription_tier or "free") == "free":
        raise HTTPException(status_code=400, detail="No active subscription to cancel.")

    sub = _latest_subscription(db, current_user.id)
    at_period_end = True if req.at_period_end is None else bool(req.at_period_end)

    stripe_updated = False
    if stripe and sub and hasattr(sub, "stripe_subscription_id"):
        stripe_sub_id = getattr(sub, "stripe_subscription_id", None)
        if stripe_sub_id:
            try:
                if at_period_end:
                    stripe.Subscription.modify(stripe_sub_id, cancel_at_period_end=True)
                    stripe_updated = True
                else:
                    stripe.Subscription.delete(stripe_sub_id)
                    stripe_updated = True
            except Exception as e:
                logger.warning("Stripe cancel failed: %s", e)

    if at_period_end:
        if sub:
            note = f"cancel_at_period_end=true; updated={datetime.utcnow().isoformat()}"
            sub.extra_data = ((sub.extra_data or "") + ("\n" if sub.extra_data else "") + note)
        result = {"status": "scheduled_cancellation", "at_period_end": True}
    else:
        if sub:
            sub.status = "cancelled"
            sub.cancelled_at = datetime.utcnow()
        current_user.subscription_tier = "free"
        reset_monthly_usage(current_user, db)
        result = {"status": "cancelled", "at_period_end": False, "tier": "free"}

    try:
        db.commit()
        db.refresh(current_user)
        if sub:
            db.refresh(sub)
    except Exception:
        db.rollback()

    result.update({"stripe_updated": stripe_updated})
    return result


@app.get("/subscription_status")
def subscription_status(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Apply local tier enforcement
    try:
        _apply_local_overdue_downgrade_if_possible(current_user, db)
    except Exception as e:
        logger.warning(f"Local downgrade check failed: {e}")

    # Optional Stripe sync
    if request.query_params.get("sync") == "1":
        try:
            sync_user_subscription_from_stripe(current_user, db)
        except Exception as e:
            logger.warning(f"Stripe sync failed: {e}")

    tier = (getattr(current_user, "subscription_tier", "free") or "free").lower()
    tier_limits = get_tier_limits(tier)

    usage = {
        "screenshots": getattr(current_user, "usage_screenshots", 0) or 0,
        "batch_requests": getattr(current_user, "usage_batch_requests", 0) or 0,
        "api_calls": getattr(current_user, "usage_api_calls", 0) or 0,
    }

    return {
        "tier": tier,
        "status": "active" if tier != "free" else "inactive",
        "usage": usage,
        "limits": tier_limits,
        "account": canonical_account(current_user),
    }

# ============================================================================
# INCLUDE ROUTERS - Clean inclusion without double-tagging
# ============================================================================

# Screenshot router (primary feature)
if screenshot_router:
    app.include_router(screenshot_router)
    logger.info("✅ Screenshot router loaded")

# Pricing router (public pricing info)
if pricing_router:
    app.include_router(pricing_router)
    logger.info("✅ Pricing router loaded")

# Batch router (batch operations)
if batch_router:
    app.include_router(batch_router)
    logger.info("✅ Batch router loaded")

# Activity router (user activity tracking)
if activity_router:
    app.include_router(activity_router)
    logger.info("✅ Activity router loaded")

# Payment router (Stripe integration)
app.include_router(payment_router)
logger.info("✅ Payment router loaded")

# API Keys router (API key management)
if api_keys_router:
    app.include_router(api_keys_router)
    logger.info("✅ API Keys router loaded")

# ============================================================================
# FRONTEND SPA (Optional)
# ============================================================================

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

# ============================================================================
# UVICORN ENTRYPOINT
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    print(f"Starting PixelPerfect Screenshot API on 0.0.0.0:8000")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

# # =======================================================================    

# # ========================================
# # main.py - PixelPerfect Screenshot API
# # ========================================
# # Production-ready FastAPI application
# # Author: OneTechly
# # Created: January 2026
# # Updated: January 2026

# from pathlib import Path
# from datetime import datetime, timedelta, timezone
# from typing import Optional, Dict, Any, Tuple
# import re, time, socket, logging, jwt, threading, os
# from collections import defaultdict, deque

# from dotenv import load_dotenv, find_dotenv
# load_dotenv()
# load_dotenv(dotenv_path=find_dotenv(".env.local"), override=True)
# load_dotenv(dotenv_path=find_dotenv(".env"), override=False)

# from fastapi import FastAPI, HTTPException, Depends, Request, Response
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
# from fastapi.responses import ORJSONResponse, HTMLResponse
# from fastapi.staticfiles import StaticFiles
# from passlib.context import CryptContext
# from starlette.middleware.base import BaseHTTPMiddleware
# from starlette.types import ASGIApp
# from sqlalchemy.orm import Session
# from sqlalchemy import delete as sqla_delete
# from sqlalchemy.exc import OperationalError
# from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
# from pydantic import BaseModel, EmailStr

# # Local imports
# from email_utils import send_password_reset_email
# from auth_utils import get_password_hash, verify_password
# from subscription_sync import sync_user_subscription_from_stripe, _apply_local_overdue_downgrade_if_possible

# # File: backend/routers/api_keys.py
# from routers.api_keys import router as api_keys_router
# app.include_router(api_keys_router) # pyright: ignore[reportUndefinedVariable]
# logger.info("✅ API Keys router loaded")  # pyright: ignore[reportUndefinedVariable]

# # ============================================================================
# # CONFIGURATION
# # ============================================================================

# SECRET_KEY = os.getenv("SECRET_KEY")
# if not SECRET_KEY:
#     raise RuntimeError(
#         "SECRET_KEY env var is required. "
#         'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
#     )

# RESET_TOKEN_TTL_SECONDS = int(os.getenv("RESET_TOKEN_TTL_SECONDS", "3600"))
# serializer = URLSafeTimedSerializer(SECRET_KEY)

# # Database URL
# DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./pixelperfect.db")
# if DATABASE_URL.startswith("postgres://"):
#     DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
# elif DATABASE_URL.startswith("postgresql://") and "+psycopg2" not in DATABASE_URL:
#     DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

# # Logging
# logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger("pixelperfect")
# logger.setLevel(logging.INFO)

# driver = "sqlite" if DATABASE_URL.startswith("sqlite") else "postgres"
# logger.info(f"✅ Config OK — using database driver: {driver}")

# # Environment
# APP_ENV = os.getenv("APP_ENV", os.getenv("ENV", "development")).lower()
# IS_PROD = APP_ENV == "production"
# ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
# FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
# FILE_RETENTION_DAYS = int(os.getenv("FILE_RETENTION_DAYS", "30"))

# # ============================================================================
# # DATABASE & MODELS
# # ============================================================================

# from models import (
#     User,
#     Screenshot,
#     Subscription,
#     get_db,
#     initialize_database,
#     engine,
#     get_tier_limits,
#     check_usage_limits,
#     increment_usage,
#     reset_monthly_usage,
# )
# from db_migrations import run_startup_migrations
# from auth_deps import get_current_user
# from webhook_handler import handle_stripe_webhook

# # ============================================================================
# # FILE STORAGE - DEFINED EARLY FOR ROUTER IMPORTS
# # ============================================================================

# SCREENSHOTS_DIR = Path(__file__).resolve().parent / "screenshots"
# SCREENSHOTS_DIR.mkdir(exist_ok=True)
# logger.info("📁 Screenshots directory: %s", SCREENSHOTS_DIR)

# # ============================================================================
# # IMPORT ROUTERS
# # ============================================================================

# # Payment router
# from payment import router as payment_router

# # Screenshot router
# try:
#     from routers.screenshot import router as screenshot_router
#     logger.info("✅ Screenshot router imported successfully")
# except ImportError as e:
#     logger.error(f"❌ Screenshot router import failed: {e}")
#     screenshot_router = None

# # Pricing router
# try:
#     from routers.pricing import router as pricing_router
#     logger.info("✅ Pricing router imported successfully")
# except ImportError as e:
#     logger.warning(f"⚠️ Pricing router not found: {e}")
#     pricing_router = None

# # Batch router
# try:
#     from routers.batch import router as batch_router
#     logger.info("✅ Batch router imported successfully")
# except ImportError as e:
#     logger.warning(f"⚠️ Batch router not found: {e}")
#     batch_router = None

# # Activity router
# try:
#     from routers.activity import router as activity_router
#     logger.info("✅ Activity router imported successfully")
# except ImportError as e:
#     logger.warning(f"⚠️ Activity router not found: {e}")
#     activity_router = None

# # ============================================================================
# # STRIPE CONFIGURATION
# # ============================================================================

# stripe = None
# try:
#     import stripe as _stripe

#     if os.getenv("STRIPE_SECRET_KEY"):
#         _stripe.api_key = os.getenv("STRIPE_SECRET_KEY").strip()
#         stripe = _stripe
        
#         # Exclude Stripe from proxy
#         os.environ.setdefault("NO_PROXY", "")
#         current_no_proxy = os.environ.get("NO_PROXY", "")
#         stripe_domains = "api.stripe.com,files.stripe.com,checkout.stripe.com"
        
#         if stripe_domains not in current_no_proxy:
#             os.environ["NO_PROXY"] = f"{current_no_proxy},{stripe_domains}" if current_no_proxy else stripe_domains
#             logger.info("✅ Stripe domains excluded from proxy: %s", stripe_domains)
            
# except Exception as e:
#     logger.warning("⚠️ Stripe initialization issue: %s", e)
#     stripe = None

# # ============================================================================
# # FASTAPI APP
# # ============================================================================

# app = FastAPI(
#     title="PixelPerfect Screenshot API",
#     version="1.0.0",
#     description="Professional Website Screenshot API with Playwright",
#     default_response_class=ORJSONResponse,
# )

# # ============================================================================
# # MIDDLEWARE - Security Headers
# # ============================================================================

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
#         response.headers.setdefault("X-Content-Type-Options", "nosniff")
#         response.headers["X-Frame-Options"] = self.x_frame_options
#         response.headers["Referrer-Policy"] = self.referrer_policy
#         response.headers.setdefault("X-XSS-Protection", "0")
        
#         if self.server_header is not None:
#             response.headers["Server"] = self.server_header
            
#         if self.hsts and request.url.scheme == "https":
#             response.headers["Strict-Transport-Security"] = f"max-age={self.hsts_max_age}; includeSubDomains"
            
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
#     "frame-src https://js.stripe.com; "
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

# # ============================================================================
# # MIDDLEWARE - Rate Limiting
# # ============================================================================

# class RateLimitMiddleware(BaseHTTPMiddleware):
#     def __init__(self, app: ASGIApp) -> None:
#         super().__init__(app)
#         self.now = time.time
#         self.buckets: Dict[str, deque] = defaultdict(deque)
#         self.lock = threading.Lock()
#         self.enabled = (os.getenv("RATE_LIMIT_ENABLED", "true").lower() in {"1", "true", "yes", "on"})
#         self.default_window = 60
#         self.default_max = int(os.getenv("RATE_LIMIT_FREE_TIER", "120"))
#         self.auth_window = 60
#         self.auth_max = 10

#     def key_for(self, request: Request) -> tuple[str, int, int]:
#         ip = (request.client.host if request.client else "unknown")
#         path = request.url.path
        
#         if path.startswith(("/token", "/register", "/webhook/stripe")):
#             return (f"AUTH:{ip}", self.auth_window, self.auth_max)
        
#         return (f"GEN:{ip}", self.default_window, self.default_max)

#     async def dispatch(self, request: Request, call_next):
#         if not self.enabled:
#             return await call_next(request)
            
#         key, window, limit = self.key_for(request)
#         now = self.now()
        
#         with self.lock:
#             q = self.buckets[key]
#             while q and now - q[0] > window:
#                 q.popleft()
                
#             if len(q) >= limit:
#                 retry_after = max(1, int(window - (now - q[0])))
#                 return ORJSONResponse(
#                     {"detail": "Too Many Requests"},
#                     status_code=429,
#                     headers={"Retry-After": str(retry_after)},
#                 )
#             q.append(now)
            
#         return await call_next(request)


# app.add_middleware(RateLimitMiddleware)

# # ============================================================================
# # MIDDLEWARE - CORS
# # ============================================================================

# PUBLIC_ORIGINS = [
#     "https://pixelperfectapi.net",
#     "https://www.pixelperfectapi.net",
#     "https://api.pixelperfectapi.net",
# ]

# DEV_ORIGINS = [
#     "http://localhost:3000",
#     "http://127.0.0.1:3000",
#     "http://192.168.1.185:3000",
# ]

# if ENVIRONMENT != "production":
#     allow_origins = PUBLIC_ORIGINS + DEV_ORIGINS + ([FRONTEND_URL] if FRONTEND_URL else [])
# else:
#     allow_origins = PUBLIC_ORIGINS + ([FRONTEND_URL] if FRONTEND_URL else [])

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=[o for o in allow_origins if o],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
#     expose_headers=["Content-Disposition", "Content-Type", "Content-Length"],
# )

# logger.info("✅ CORS enabled for origins: %s", allow_origins)

# # ============================================================================
# # MOUNT STATIC FILES - SCREENSHOTS
# # ============================================================================

# # Mount screenshots directory for public access
# app.mount("/screenshots", StaticFiles(directory=str(SCREENSHOTS_DIR)), name="screenshots")
# logger.info("✅ Mounted /screenshots static directory")

# # ============================================================================
# # AUTH HELPERS
# # ============================================================================

# ALGORITHM = os.getenv("ALGORITHM", "HS256")
# ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
# oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
# pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
#     to_encode = dict(data)
#     expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
#     to_encode.update({"exp": expire})
#     return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# def get_user(db: Session, username: str) -> Optional[User]:
#     return db.query(User).filter(User.username == username).first()


# def canonical_account(user: User) -> Dict[str, Any]:
#     return {
#         "username": (user.username or "").strip(),
#         "email": (user.email or "").strip().lower(),
#     }


# def check_internet() -> bool:
#     try:
#         socket.create_connection(("8.8.8.8", 53), timeout=3)
#         return True
#     except OSError:
#         return False

# # ============================================================================
# # STRIPE HELPERS
# # ============================================================================

# def ensure_stripe_customer_for_user(user: User, db: Session) -> None:
#     """Ensure user has Stripe customer ID (graceful - doesn't fail if Stripe is down)"""
#     if not stripe or not os.getenv("STRIPE_SECRET_KEY"):
#         return

#     if getattr(user, "stripe_customer_id", None):
#         return

#     email = (user.email or "").strip().lower()
#     username = (user.username or "").strip() or None

#     if not email:
#         return

#     try:
#         # Try to find existing customer
#         customer = None
#         try:
#             found = stripe.Customer.search(query=f"email:'{email}'", limit=1)
#             if getattr(found, "data", []):
#                 customer = found.data[0]
#         except Exception:
#             pass

#         if customer:
#             user.stripe_customer_id = customer["id"]
#             db.commit()
#             db.refresh(user)
#             logger.info("🔄 Linked existing Stripe customer %s", customer["id"])
#             return

#         # Create new customer
#         created = stripe.Customer.create(
#             email=email,
#             name=username,
#             metadata={"app_user_id": str(user.id)},
#         )
#         user.stripe_customer_id = created["id"]
#         db.commit()
#         db.refresh(user)
#         logger.info("✅ Created Stripe customer %s", created["id"])

#     except Exception as e:
#         logger.warning("⚠️ Stripe customer creation skipped (non-fatal): %s", e)


# # ============================================================================
# # PYDANTIC MODELS
# # ============================================================================

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


# class Token(BaseModel):
#     access_token: str
#     token_type: str


# class ChangePasswordRequest(BaseModel):
#     current_password: str
#     new_password: str


# class LoginJSON(BaseModel):
#     username: str
#     password: str


# class ForgotPasswordIn(BaseModel):
#     email: EmailStr


# class ResetPasswordIn(BaseModel):
#     token: str
#     new_password: str


# class CancelRequest(BaseModel):
#     at_period_end: Optional[bool] = True

# # ============================================================================
# # STRIPE WEBHOOK IDEMPOTENCY
# # ============================================================================

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

# # ============================================================================
# # STARTUP TASKS
# # ============================================================================

# @app.on_event("startup")
# async def on_startup():
#     """Application startup tasks"""
#     initialize_database()
#     run_startup_migrations(engine)

#     logger.info("=" * 60)
#     logger.info("🚀 PixelPerfect Screenshot API Starting")
#     logger.info("=" * 60)
#     logger.info("📝 Environment: %s", ENVIRONMENT)
#     logger.info("📁 Screenshots directory: %s", SCREENSHOTS_DIR)
#     logger.info("🗄️ Database: %s", "PostgreSQL" if "postgres" in DATABASE_URL else "SQLite")
#     logger.info("💳 Stripe configured: %s", bool(stripe and os.getenv("STRIPE_SECRET_KEY")))
#     logger.info("=" * 60)
#     logger.info("✅ Backend started successfully")
#     logger.info("=" * 60)

# # ============================================================================
# # ROUTES - Core
# # ============================================================================

# @app.get("/")
# def root():
#     return {
#         "message": "PixelPerfect Screenshot API",
#         "status": "running",
#         "version": "1.0.0",
#         "features": ["screenshots", "batch", "webhooks", "payments"],
#     }


# @app.get("/health")
# def health():
#     return {
#         "status": "healthy",
#         "timestamp": datetime.utcnow().isoformat(),
#         "environment": ENVIRONMENT,
#         "services": {
#             "stripe": "configured" if os.getenv("STRIPE_SECRET_KEY") else "not_configured",
#             "playwright": "available",
#         },
#     }


# @app.head("/health")
# def health_head():
#     return Response(status_code=200)

# # ============================================================================
# # ROUTES - Authentication
# # ============================================================================

# @app.post("/register")
# def register(user: UserCreate, db: Session = Depends(get_db)):
#     logger.info(f"🔵 REGISTRATION - Username: {user.username}, Email: {user.email}")

#     username = (user.username or "").strip()
#     email = (user.email or "").strip().lower()

#     # Validation
#     if db.query(User).filter(User.username == username).first():
#         raise HTTPException(status_code=400, detail="Username already exists.")

#     if db.query(User).filter(User.email == email).first():
#         raise HTTPException(status_code=400, detail="Email already exists.")

#     # Create user
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
#     logger.info(f"✅ User created: {username} (ID: {obj.id})")

#     # Try to create Stripe customer (graceful)
#     stripe_customer_id = None
#     if stripe:
#         try:
#             ensure_stripe_customer_for_user(obj, db)
#             stripe_customer_id = obj.stripe_customer_id
#         except Exception as e:
#             logger.warning(f"⚠️ Stripe customer creation skipped: {e}")

#     return {
#         "message": "User registered successfully.",
#         "account": canonical_account(obj),
#         "stripe_customer_id": stripe_customer_id,
#     }


# @app.post("/token")
# def token_login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
#     username_input = (form.username or "").strip()
#     password_input = form.password or ""

#     user = db.query(User).filter(User.username == username_input).first()
#     if not user or not verify_password(password_input, user.hashed_password):
#         raise HTTPException(status_code=401, detail="Incorrect username or password")

#     # Try to ensure Stripe customer (graceful)
#     try:
#         ensure_stripe_customer_for_user(user, db)
#     except Exception as e:
#         logger.warning(f"⚠️ Stripe customer link skipped: {e}")

#     token = create_access_token(
#         {"sub": user.username},
#         timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
#     )
    
#     return {
#         "access_token": token,
#         "token_type": "bearer",
#         "user": canonical_account(user),
#         "must_change_password": bool(getattr(user, "must_change_password", False)),
#     }


# @app.post("/token_json")
# def token_login_json(req: LoginJSON, db: Session = Depends(get_db)):
#     username_input = (req.username or "").strip()
#     password_input = req.password or ""

#     user = db.query(User).filter(User.username == username_input).first()
#     if not user or not verify_password(password_input, user.hashed_password):
#         raise HTTPException(status_code=401, detail="Incorrect username or password")

#     try:
#         ensure_stripe_customer_for_user(user, db)
#     except Exception as e:
#         logger.warning(f"⚠️ Stripe customer link skipped: {e}")

#     token = create_access_token(
#         {"sub": user.username},
#         timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
#     )
    
#     return {
#         "access_token": token,
#         "token_type": "bearer",
#         "user": canonical_account(user),
#         "must_change_password": bool(getattr(user, "must_change_password", False)),
#     }


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
#         except Exception as e:
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


# @app.post("/user/change_password")
# def change_password(
#     req: ChangePasswordRequest,
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     if not verify_password(req.current_password, current_user.hashed_password):
#         raise HTTPException(status_code=400, detail="Current password is incorrect")
    
#     if not req.new_password or len(req.new_password) < 8:
#         raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    
#     current_user.hashed_password = get_password_hash(req.new_password)
#     try:
#         current_user.must_change_password = False
#     except Exception:
#         pass
#     db.commit()
#     db.refresh(current_user)
#     logger.info("🔑 Password changed for user %s", current_user.username)
#     return {"status": "ok"}


# @app.delete("/user/delete-account")
# def delete_account(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
#     uid = int(current_user.id)
#     email = (current_user.email or "unknown@unknown.com")

#     # Cancel Stripe subscriptions
#     try:
#         if stripe and getattr(current_user, "stripe_customer_id", None):
#             subs = stripe.Subscription.list(customer=current_user.stripe_customer_id, limit=100)
#             for sub in getattr(subs, "data", []):
#                 try:
#                     stripe.Subscription.delete(sub.id)
#                 except Exception:
#                     pass
#     except Exception:
#         pass

#     # Delete database records
#     try:
#         db.execute(sqla_delete(Screenshot).where(Screenshot.user_id == uid))
#         db.execute(sqla_delete(Subscription).where(Subscription.user_id == uid))
#         db.execute(sqla_delete(User).where(User.id == uid))
#         db.commit()
#     except Exception as e:
#         db.rollback()
#         logger.error(f"DB delete failed for user {uid}: {e}")
#         raise HTTPException(status_code=500, detail="Failed to delete account")

#     # Delete user files
#     try:
#         for p in SCREENSHOTS_DIR.glob(f"*{uid}*"):
#             try:
#                 p.unlink(missing_ok=True)
#             except Exception:
#                 pass
#     except Exception:
#         pass

#     return {
#         "message": "Account deleted successfully.",
#         "deleted_at": datetime.utcnow().isoformat(),
#         "user_email": email,
#     }

# # ============================================================================
# # ROUTES - Stripe Webhook
# # ============================================================================

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
#         logger.warning(f"Stripe webhook signature verification failed: {e}")
#         raise HTTPException(status_code=400, detail="Invalid signature")

#     if not event or not event.get("id"):
#         raise HTTPException(status_code=400, detail="Invalid event payload")

#     if _idemp_seen(event["id"]):
#         logger.info(f"Stripe webhook duplicate event {event['id']} ignored")
#         return {"status": "ok", "duplicate": True}

#     request.state.verified_event = event
#     result = await handle_stripe_webhook(request) if hasattr(handle_stripe_webhook, "__call__") else {"status": "ok"}
#     return result

# # ============================================================================
# # ROUTES - Subscription Management
# # ============================================================================

# def _latest_subscription(db: Session, user_id: int) -> Optional[Subscription]:
#     try:
#         return (
#             db.query(Subscription)
#             .filter(Subscription.user_id == user_id)
#             .order_by(Subscription.created_at.desc())
#             .first()
#         )
#     except Exception:
#         return None


# @app.post("/subscription/cancel")
# def cancel_subscription(
#     req: CancelRequest,
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     if (current_user.subscription_tier or "free") == "free":
#         raise HTTPException(status_code=400, detail="No active subscription to cancel.")

#     sub = _latest_subscription(db, current_user.id)
#     at_period_end = True if req.at_period_end is None else bool(req.at_period_end)

#     stripe_updated = False
#     if stripe and sub and hasattr(sub, "stripe_subscription_id"):
#         stripe_sub_id = getattr(sub, "stripe_subscription_id", None)
#         if stripe_sub_id:
#             try:
#                 if at_period_end:
#                     stripe.Subscription.modify(stripe_sub_id, cancel_at_period_end=True)
#                     stripe_updated = True
#                 else:
#                     stripe.Subscription.delete(stripe_sub_id)
#                     stripe_updated = True
#             except Exception as e:
#                 logger.warning("Stripe cancel failed: %s", e)

#     if at_period_end:
#         if sub:
#             note = f"cancel_at_period_end=true; updated={datetime.utcnow().isoformat()}"
#             sub.extra_data = ((sub.extra_data or "") + ("\n" if sub.extra_data else "") + note)
#         result = {"status": "scheduled_cancellation", "at_period_end": True}
#     else:
#         if sub:
#             sub.status = "cancelled"
#             sub.cancelled_at = datetime.utcnow()
#         current_user.subscription_tier = "free"
#         reset_monthly_usage(current_user, db)
#         result = {"status": "cancelled", "at_period_end": False, "tier": "free"}

#     try:
#         db.commit()
#         db.refresh(current_user)
#         if sub:
#             db.refresh(sub)
#     except Exception:
#         db.rollback()

#     result.update({"stripe_updated": stripe_updated})
#     return result


# @app.get("/subscription_status")
# def subscription_status(
#     request: Request,
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     # Apply local tier enforcement
#     try:
#         _apply_local_overdue_downgrade_if_possible(current_user, db)
#     except Exception as e:
#         logger.warning(f"Local downgrade check failed: {e}")

#     # Optional Stripe sync
#     if request.query_params.get("sync") == "1":
#         try:
#             sync_user_subscription_from_stripe(current_user, db)
#         except Exception as e:
#             logger.warning(f"Stripe sync failed: {e}")

#     tier = (getattr(current_user, "subscription_tier", "free") or "free").lower()
#     tier_limits = get_tier_limits(tier)

#     usage = {
#         "screenshots": getattr(current_user, "usage_screenshots", 0) or 0,
#         "batch_requests": getattr(current_user, "usage_batch_requests", 0) or 0,
#         "api_calls": getattr(current_user, "usage_api_calls", 0) or 0,
#     }

#     return {
#         "tier": tier,
#         "status": "active" if tier != "free" else "inactive",
#         "usage": usage,
#         "limits": tier_limits,
#         "account": canonical_account(current_user),
#     }

# # ============================================================================
# # INCLUDE ROUTERS - Clean inclusion without double-tagging
# # ============================================================================

# # Screenshot router (primary feature)
# if screenshot_router:
#     app.include_router(screenshot_router)
#     logger.info("✅ Screenshot router loaded")

# # Pricing router (public pricing info)
# if pricing_router:
#     app.include_router(pricing_router)
#     logger.info("✅ Pricing router loaded")

# # Batch router (batch operations)
# if batch_router:
#     app.include_router(batch_router)
#     logger.info("✅ Batch router loaded")

# # Activity router (user activity tracking)
# if activity_router:
#     app.include_router(activity_router)
#     logger.info("✅ Activity router loaded")

# # Payment router (Stripe integration)
# app.include_router(payment_router)
# logger.info("✅ Payment router loaded")

# # ============================================================================
# # FRONTEND SPA (Optional)
# # ============================================================================

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

# # ============================================================================
# # UVICORN ENTRYPOINT
# # ============================================================================

# if __name__ == "__main__":
#     import uvicorn
#     print(f"Starting PixelPerfect API on 0.0.0.0:8000")
#     uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


