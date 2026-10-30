# tests/suite/conftest.py
# =============================================================================
# PixelPerfect test suite: shared fixtures
# =============================================================================
#
# How the suite isolates the app from the outside world
# -----------------------------------------------------
# The backend reads DATABASE_URL, SECRET_KEY and the Stripe/R2/SMTP settings
# at IMPORT time (models.py builds its engine the moment it is imported). So
# the environment below is set before anything from the app is imported, and
# every external dependency is replaced:
#
#   Database     a throwaway SQLite file in pytest's temp dir; every table is
#                emptied between tests, so tests never see each other's rows.
#   Playwright   FakeScreenshotService. It writes a tiny real PNG and returns
#                the same result dict the real service returns. The real
#                browser is exercised separately in integration/ (opt-in).
#   R2 storage   FakeStorage, in memory.
#   Stripe       a fake module per test that needs it (see FakeStripe).
#   SMTP         the send functions are patched; mail is captured, not sent.
#   Network      any outbound socket to a non-loopback address fails the test,
#                so a missed patch can never reach Stripe or a real website.
#
# Nothing here changes application code. The suite tests the code as it is.
# =============================================================================

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import socket
import uuid
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest

# -----------------------------------------------------------------------------
# 1. Environment, set before the app is imported
# -----------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parents[2]
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="pixelperfect-tests-"))
_DB_FILE = _TMP_ROOT / "test.db"
atexit.register(shutil.rmtree, _TMP_ROOT, ignore_errors=True)

_TEST_ENV = {
    "SECRET_KEY": "test-secret-key-not-for-production",
    "DATABASE_URL": f"sqlite:///{_DB_FILE.as_posix()}",
    "ENVIRONMENT": "test",
    "FRONTEND_URL": "http://frontend.test",
    "BACKEND_URL": "http://backend.test",
    # A short wait so the concurrency tests do not take 5 s each.
    "CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS": "0.3",
}
# Settings that, if inherited from a developer's shell or .env, would make
# the app talk to real services. Removed for the whole run.
_SCRUB = [
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
    "R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME", "R2_PUBLIC_URL",
    "SMTP_USERNAME", "SMTP_PASSWORD",
    # Tier limits come from env; scrub so the documented defaults apply.
    *[f"{t}_{k}_LIMIT" for t in ("FREE", "PRO", "BUSINESS", "PREMIUM")
      for k in ("SCREENSHOTS", "BATCH", "API_CALLS")],
]
for _k in _SCRUB:
    os.environ.pop(_k, None)
os.environ.update(_TEST_ENV)

# main.py calls load_dotenv(). Point it at nothing so a developer's real .env
# (with live Stripe keys) is never read during tests.
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **k: False  # type: ignore[assignment]
dotenv.find_dotenv = lambda *a, **k: ""     # type: ignore[assignment]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# The app writes relative paths ("screenshots/...") against the working
# directory; the clean_state fixture below moves each test into its own temp
# dir so a run never litters the repo. (Not done here at import time: pytest
# is still resolving test paths relative to the working directory.)


# -----------------------------------------------------------------------------
# 2. Network guard
# -----------------------------------------------------------------------------
# Three layers, because no single one covers every platform:
#   getaddrinfo  every hostname lookup, including Windows' Proactor event loop
#                (whose connects bypass socket.connect)
#   connect / connect_ex  direct connects to IP literals
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _is_local(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode(errors="ignore")
    return isinstance(host, str) and (
        host in ("localhost", "::1", "testserver", "test", "") or host.startswith("127.")
    )


def _refuse(target):
    raise RuntimeError(
        f"Test tried to reach {target!r}. External services must be faked in tests."
    )


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    return _real_connect(self, address) if _is_local(host) else _refuse(address)


def _guarded_connect_ex(self, address):
    host = address[0] if isinstance(address, tuple) else address
    return _real_connect_ex(self, address) if _is_local(host) else _refuse(address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    return _real_getaddrinfo(host, *args, **kwargs) if _is_local(host) else _refuse(host)


socket.socket.connect = _guarded_connect        # type: ignore[assignment]
socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[assignment]
socket.getaddrinfo = _guarded_getaddrinfo       # type: ignore[assignment]


# -----------------------------------------------------------------------------
# 3. Import the app (once, for the whole session)
# -----------------------------------------------------------------------------
import main  # noqa: E402
import models  # noqa: E402
import batch as batch_module  # noqa: E402
import screenshot_endpoints  # noqa: E402
import routers.screenshot as screenshot_router  # noqa: E402
from api_key_system import create_api_key_for_user  # noqa: E402
from auth_utils import get_password_hash  # noqa: E402
from models import Base, SessionLocal, engine  # noqa: E402

Base.metadata.create_all(bind=engine)

# A 1x1 transparent PNG. Real bytes, so anything that inspects the image works.
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)

TEST_PASSWORD = "Correct-Horse-9"


# -----------------------------------------------------------------------------
# 4. Fakes
# -----------------------------------------------------------------------------
class FakeScreenshotService:
    """
    Stands in for both ScreenshotService classes.

    Records every call in `calls`. Behaviour per test:
      raise_exc   an exception instance to raise instead of capturing
      delay_s     seconds to await before returning (concurrency tests)
      result      dict merged over the default result
    """

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.calls: List[Dict[str, Any]] = []
        self.raise_exc: Optional[BaseException] = None
        self.delay_s: float = 0.0
        self.result: Dict[str, Any] = {}
        self.ready = True
        self.in_flight = 0
        self.max_in_flight = 0

    def is_ready(self) -> bool:
        return self.ready

    def last_error(self) -> Optional[str]:
        return None

    async def initialize(self) -> None:
        self.ready = True

    async def close(self) -> None:
        self.ready = False

    def get_available_devices(self) -> List[str]:
        return ["iphone_13", "pixel_7", "ipad_pro"]

    async def capture_screenshot(self, url: str, **kwargs) -> Dict[str, Any]:
        self.calls.append({"url": url, **kwargs})
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            if self.raise_exc is not None:
                raise self.raise_exc
            fmt = kwargs.get("format", "png")
            # Unique per call, so concurrent captures never share a file.
            name = f"fake_{uuid.uuid4().hex}.{fmt}"
            path = self.out_dir / name
            path.write_bytes(TINY_PNG)
            width = kwargs.get("width", 1920)
            height = kwargs.get("height", 1080)
            result = {
                "filename": name,
                "filepath": str(path),
                "url": url,
                "width": width,
                "height": height,
                "viewport_width": width,
                "viewport_height": height,
                "image_width": 1,
                "image_height": 1,
                "format": fmt,
                "full_page": kwargs.get("full_page", False),
                "dark_mode": kwargs.get("dark_mode", False),
                "file_size": len(TINY_PNG),
                "created_at": datetime.utcnow(),
                "js_warning": None,
                "element_selector": kwargs.get("target_element"),
                "truncated": False,
                "document_height": None,
            }
            result.update(self.result)
            return result
        finally:
            self.in_flight -= 1


class FakeStorage:
    """
    In-memory replacement for services.storage_service.StorageService.
    use_r2 is True, as in production; batch.py only uploads when it is set.
    """

    def __init__(self):
        self.use_r2 = True
        self.files: Dict[str, bytes] = {}
        self.deleted: List[str] = []
        self.fail_uploads = False

    async def upload_screenshot(self, file_data: bytes, filename: str,
                                content_type: str = "image/png") -> str:
        if self.fail_uploads:
            raise RuntimeError("simulated R2 outage")
        self.files[filename] = file_data
        return f"https://r2.test/{filename}"

    async def delete_screenshot(self, filename: str) -> bool:
        self.deleted.append(filename)
        return self.files.pop(filename, None) is not None


class StripeObj(dict):
    """Dict with attribute access, the way StripeObject behaves."""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as e:
            raise AttributeError(item) from e

    def auto_paging_iter(self):
        return iter(self.get("data") or [])


class FakeStripeError(Exception):
    pass


class FakeStripe:
    """
    A minimal fake of the stripe module, covering only what the app calls.
    Tests set the class-level lists and inspect `calls`.
    """

    def __init__(self):
        self.api_key = "sk_test_fake"
        self.calls: List[tuple] = []
        self.customers: List[Dict[str, Any]] = []
        # customer_id -> list of subscription dicts
        self.subscriptions: Dict[str, List[Dict[str, Any]]] = {}
        self.fail_subscription_list = False
        self.fail_customer_list = False
        self.webhook_event: Optional[Dict[str, Any]] = None
        self.webhook_raises: Optional[Exception] = None
        self.products: Dict[str, Dict[str, Any]] = {}
        self.prices: Dict[str, Dict[str, Any]] = {}
        outer = self

        class _error:
            InvalidRequestError = FakeStripeError
            StripeError = FakeStripeError

        self.error = _error

        class Customer:
            @staticmethod
            def list(email=None, limit=10):
                outer.calls.append(("Customer.list", email))
                if outer.fail_customer_list:
                    raise FakeStripeError("customer list down")
                data = [StripeObj(c) for c in outer.customers
                        if email is None or c.get("email") == email]
                return StripeObj(data=data[:limit])

            @staticmethod
            def create(**kw):
                outer.calls.append(("Customer.create", kw))
                cid = f"cus_{len(outer.customers) + 1}"
                outer.customers.append({"id": cid, **kw})
                return StripeObj(id=cid, **kw)

            @staticmethod
            def modify(cid, **kw):
                outer.calls.append(("Customer.modify", cid, kw))
                return StripeObj(id=cid, **kw)

        class Subscription:
            @staticmethod
            def list(customer=None, status=None, limit=5, expand=None):
                outer.calls.append(("Subscription.list", customer, status))
                if outer.fail_subscription_list:
                    raise FakeStripeError("subscription list down")
                subs = outer.subscriptions.get(customer, [])
                if status and status != "all":
                    subs = [s for s in subs if s.get("status") == status]
                elif status is None:
                    # Real Stripe omits canceled subscriptions unless asked.
                    subs = [s for s in subs if s.get("status") != "canceled"]
                return StripeObj(data=[StripeObj(s) for s in subs[:limit]])

            @staticmethod
            def modify(sid, **kw):
                outer.calls.append(("Subscription.modify", sid, kw))
                for subs in outer.subscriptions.values():
                    for s in subs:
                        if s["id"] == sid:
                            s.update(kw)
                            return StripeObj(s)
                raise FakeStripeError("no such subscription")

            @staticmethod
            def delete(sid):
                outer.calls.append(("Subscription.delete", sid))
                return StripeObj(id=sid, status="canceled")

        class Product:
            @staticmethod
            def retrieve(pid):
                outer.calls.append(("Product.retrieve", pid))
                return StripeObj(outer.products.get(pid, {"id": pid, "name": ""}))

        class Price:
            @staticmethod
            def list(lookup_keys=None, limit=1):
                outer.calls.append(("Price.list", tuple(lookup_keys or ())))
                data = [StripeObj(outer.prices[k]) for k in (lookup_keys or [])
                        if k in outer.prices]
                return StripeObj(data=data)

        class _Session:
            @staticmethod
            def create(**kw):
                outer.calls.append(("checkout.Session.create", kw))
                return StripeObj(id="cs_test_1", url="https://checkout.stripe.test/cs_test_1")

        class _PortalSession:
            @staticmethod
            def create(**kw):
                outer.calls.append(("billing_portal.Session.create", kw))
                return StripeObj(url="https://billing.stripe.test/session")

        class Webhook:
            @staticmethod
            def construct_event(payload=None, sig_header=None, secret=None):
                outer.calls.append(("Webhook.construct_event", sig_header))
                if outer.webhook_raises is not None:
                    raise outer.webhook_raises
                return outer.webhook_event

        self.Customer = Customer
        self.Subscription = Subscription
        self.Product = Product
        self.Price = Price
        self.checkout = SimpleNamespace(Session=_Session)
        self.billing_portal = SimpleNamespace(Session=_PortalSession)
        self.Webhook = Webhook


# -----------------------------------------------------------------------------
# 5. Fixtures
# -----------------------------------------------------------------------------
def _empty_all_tables() -> None:
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Every test starts with empty tables and empty in-process caches."""
    monkeypatch.chdir(tmp_path)
    _empty_all_tables()
    main._USER_LIMITERS.clear()
    main._IDEMP_STORE.clear()
    main._SUB_STATE_CACHE.clear()
    batch_module.JOBS.clear()
    yield
    _empty_all_tables()


@pytest.fixture
def fake_shots(monkeypatch, tmp_path) -> FakeScreenshotService:
    """Replace the capture engine everywhere the app holds a reference."""
    fake = FakeScreenshotService(tmp_path)
    for mod in (screenshot_router, batch_module, screenshot_endpoints):
        monkeypatch.setattr(mod, "screenshot_service", fake)
    monkeypatch.setattr(main, "screenshot_service", fake)
    return fake


@pytest.fixture
def fake_storage(monkeypatch) -> FakeStorage:
    store = FakeStorage()
    for mod in (screenshot_router, batch_module, screenshot_endpoints):
        monkeypatch.setattr(mod, "storage_service", store)
    return store


@pytest.fixture
def client(fake_shots, fake_storage):
    """
    TestClient WITHOUT the startup event (which would launch Chromium).
    Tables already exist; startup itself is tested in test_routing_contract.
    """
    from fastapi.testclient import TestClient

    return TestClient(main.app)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def make_user(db) -> Callable[..., SimpleNamespace]:
    """
    make_user(tier="pro", username=None, email=None, **columns)
    Returns SimpleNamespace(id, username, email, password, token, api_key, headers,
                            key_headers).
    """
    counter = {"n": 0}

    def _make(tier: str = "free", username: Optional[str] = None,
              email: Optional[str] = None, **cols) -> SimpleNamespace:
        counter["n"] += 1
        n = counter["n"]
        username = username or f"user{n}_{tier}"
        email = (email or f"{username}@example.com").lower()
        user = models.User(
            username=username,
            email=email,
            hashed_password=get_password_hash(TEST_PASSWORD),
            subscription_tier=tier,
            created_at=datetime.utcnow(),
            **cols,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        key, _ = create_api_key_for_user(db, user.id)
        token = main.create_access_token({"sub": username}, timedelta(minutes=30))
        return SimpleNamespace(
            id=user.id, username=username, email=email, password=TEST_PASSWORD,
            token=token, api_key=key,
            headers={"Authorization": f"Bearer {token}"},
            key_headers={"Authorization": f"Bearer {key}"},
        )

    return _make


@pytest.fixture
def add_screenshots(db):
    """add_screenshots(user_id, n, created_at=None, **cols) -> list of ids."""

    def _add(user_id: int, n: int, created_at: Optional[datetime] = None, **cols):
        ids = []
        for _ in range(n):
            s = models.Screenshot(
                user_id=user_id,
                url="https://example.test/",
                width=1920, height=1080, format="png",
                size_bytes=10, storage_url="https://r2.test/x.png",
                created_at=created_at or datetime.utcnow(),
                **cols,
            )
            db.add(s)
            db.flush()
            ids.append(s.id)
        db.commit()
        return ids

    return _add


@pytest.fixture
def fake_stripe(monkeypatch) -> FakeStripe:
    """
    Install a FakeStripe in every module that holds a stripe reference, and
    set the env vars the endpoints check for.
    """
    import subscription_sync
    import webhook_handler

    fake = FakeStripe()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_fake")
    monkeypatch.setattr(main, "stripe", fake)
    monkeypatch.setattr(subscription_sync, "stripe", fake)
    monkeypatch.setattr(subscription_sync, "STRIPE_AVAILABLE", True)
    monkeypatch.setattr(webhook_handler, "stripe", fake)
    # webhook_handler._ensure_stripe_key re-imports the real module when its
    # global is falsy; the fake is truthy, so it is kept.
    return fake


@pytest.fixture
def sent_mail(monkeypatch) -> List[Dict[str, str]]:
    """Capture outgoing mail instead of sending it."""
    box: List[Dict[str, str]] = []
    monkeypatch.setattr(
        main, "send_password_reset_email",
        lambda to, link: box.append({"kind": "reset", "to": to, "link": link}),
    )
    monkeypatch.setattr(
        main, "_send_username_reminder_email",
        lambda to, username: box.append({"kind": "username", "to": to, "username": username}),
    )
    return box


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "browser: needs a real Chromium (opt-in: run with --browser)",
    )
    config.addinivalue_line(
        "markers",
        "known_defect: pins a defect found in review; xfail(strict) until fixed",
    )


def pytest_addoption(parser):
    parser.addoption(
        "--browser", action="store_true", default=False,
        help="Also run the real-Chromium integration tests.",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--browser"):
        return
    skip = pytest.mark.skip(reason="real-browser test; run with --browser")
    for item in items:
        if "browser" in item.keywords:
            item.add_marker(skip)
