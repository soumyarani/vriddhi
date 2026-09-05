"""Shared test fixtures.

Environment is configured *before* any `app.*` import: `app.config.settings` is
instantiated at import time and `app.database.engine` is built from it, so the
database URL has to be in place first. Everything below therefore imports lazily
inside fixtures rather than at module top level.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# ---- Environment (must precede app imports) -------------------------------
_TMP_DB = Path(tempfile.gettempdir()) / "whatsapp_commerce_test.sqlite3"

os.environ.update(
    {
        "ENVIRONMENT": "development",
        "DATABASE_URL": f"sqlite+aiosqlite:///{_TMP_DB}",
        "REDIS_URL": "redis://localhost:6379/15",
        "JWT_SECRET": "test-secret-not-used-in-production-0123456789abcdef",
        "GOOGLE_CLIENT_ID": "test-client-id",
        "GOOGLE_CLIENT_SECRET": "test-client-secret",
        "GOOGLE_REDIRECT_URI": "http://localhost:3000/auth/callback",
        "WHATSAPP_TOKEN": "test-wa-token",
        "WHATSAPP_PHONE_NUMBER_ID": "1234567890",
        "WHATSAPP_VERIFY_TOKEN": "verify-me",
        "WHATSAPP_APP_SECRET": "test-app-secret",
        "WHATSAPP_CATALOG_ID": "catalog-123",
        "CASHFREE_APP_ID": "cf-app",
        "CASHFREE_SECRET_KEY": "cf-secret",
        "CASHFREE_WEBHOOK_SECRET": "cf-webhook-secret",
        "AGENT_ALLOWED_DOMAINS": "shop.test",
        "CORS_ALLOWED_ORIGINS": "http://localhost:3000",
        # Rate limiting off: slowapi would otherwise reach for Redis, and the
        # limits themselves are asserted in test_security.py with it re-enabled.
        "RATE_LIMIT_ENABLED": "false",
        "LOG_LEVEL": "WARNING",
    }
)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402


# ---- Redis stub -----------------------------------------------------------
class FakeRedis:
    """Enough of the Redis surface for cache, counters and locks.

    The real client is swapped out rather than skipped so cache-hit paths are
    genuinely exercised; `app.cache` swallows backend errors, which would
    otherwise let a broken cache call pass unnoticed.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def setex(self, key, ttl, value):
        self.store[key] = value
        return True

    async def delete(self, *keys):
        return sum(bool(self.store.pop(k, None)) for k in keys)

    async def incr(self, key):
        value = int(self.store.get(key, 0)) + 1
        self.store[key] = str(value)
        return value

    async def expire(self, key, ttl):
        return True

    async def exists(self, key):
        return int(key in self.store)

    async def ping(self):
        return True

    async def aclose(self):
        return None

    async def scan_iter(self, match=None, count=None):
        import fnmatch

        for key in list(self.store):
            if match is None or fnmatch.fnmatch(key, match):
                yield key


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    import app.cache as cache_mod
    import app.redis as redis_mod

    client = FakeRedis()
    monkeypatch.setattr(redis_mod, "get_redis", lambda: client)
    monkeypatch.setattr(cache_mod, "get_redis", lambda: client)
    return client


@pytest.fixture(autouse=True)
def captured_jobs(monkeypatch):
    """Capture arq enqueues instead of dialling Redis.

    Returns the list of `(job_name, args, kwargs)` so tests can assert that a
    webhook handed work off to a worker without running the worker.
    """
    jobs: list[tuple[str, tuple, dict]] = []

    async def _enqueue(job: str, *args, **kwargs) -> bool:
        jobs.append((job, args, kwargs))
        return True

    import app.redis as redis_mod
    import app.routers.admin as admin_mod
    import app.routers.webhook as webhook_mod

    monkeypatch.setattr(redis_mod, "enqueue", _enqueue)
    monkeypatch.setattr(webhook_mod, "enqueue", _enqueue)
    monkeypatch.setattr(admin_mod, "enqueue", _enqueue)
    return jobs


# ---- Database -------------------------------------------------------------
@pytest_asyncio.fixture(scope="session", autouse=True)
async def _schema():
    from app.database import engine
    from app.models.base import Base

    # Importing the model modules is what registers them on Base.metadata.
    import app.models  # noqa: F401

    if _TMP_DB.exists():
        _TMP_DB.unlink()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()
    if _TMP_DB.exists():
        _TMP_DB.unlink()


@pytest_asyncio.fixture
async def db(_schema):
    """A real session, with every table emptied afterwards.

    The usual "wrap each test in a transaction and roll back" trick does not
    hold here: routers legitimately call `db.commit()`, and SQLite defers its
    BEGIN in a way that lets those commits escape the enclosing transaction
    even in savepoint mode. Deleting the rows afterwards is less elegant but
    actually isolates, and it keeps commits behaving as they do in production.
    """
    from app.database import SessionLocal, engine
    from app.models.base import Base

    async with SessionLocal() as session:
        try:
            yield session
        finally:
            await session.rollback()

    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest_asyncio.fixture
async def client(db):
    """HTTP client with `get_db` pinned to the test's transaction."""
    from httpx import ASGITransport, AsyncClient

    from app.database import get_db
    from app.main import app

    async def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# ---- Domain fixtures ------------------------------------------------------
@pytest_asyncio.fixture
async def user(db):
    from app.models.enums import AuthProvider
    from app.models.user import User

    record = User(
        email="shopper@example.com",
        name="Asha Rao",
        phone="919876543210",
        google_id="google-shopper-1",
        auth_provider=AuthProvider.GOOGLE,
    )
    db.add(record)
    await db.flush()
    return record


@pytest_asyncio.fixture
async def agent(db):
    from app.models.enums import AgentRole
    from app.models.user import Agent

    record = Agent(
        email="agent@shop.test",
        name="Ravi Kumar",
        google_id="google-agent-1",
        role=AgentRole.AGENT,
        active=True,
    )
    db.add(record)
    await db.flush()
    return record


@pytest_asyncio.fixture
async def admin(db):
    from app.models.enums import AgentRole
    from app.models.user import Agent

    record = Agent(
        email="boss@shop.test",
        name="Meera Nair",
        google_id="google-admin-1",
        role=AgentRole.ADMIN,
        active=True,
    )
    db.add(record)
    await db.flush()
    return record


@pytest_asyncio.fixture
async def category(db):
    from app.models.product import Category

    record = Category(name="Sarees", slug="sarees", active=True)
    db.add(record)
    await db.flush()
    return record


@pytest_asyncio.fixture
async def product(db, category):
    """A published product with one variant holding 10 units."""
    from decimal import Decimal

    from app.models.product import Product, ProductVariant

    record = Product(
        name="Kanjivaram Silk Saree",
        description="Handwoven silk with a zari border.",
        category_id=category.id,
        base_price=Decimal("4999.00"),
        image_urls=["https://cdn.example.com/saree.jpg"],
        active=True,
        gst_rate=Decimal("5.00"),
    )
    db.add(record)
    await db.flush()

    variant = ProductVariant(
        product_id=record.id,
        sku="SAR-KAN-RED",
        name="Red",
        stock=10,
        active=True,
    )
    db.add(variant)
    await db.flush()
    await db.refresh(record, ["variants"])
    return record


@pytest_asyncio.fixture
async def variant(db, product):
    return product.variants[0]


@pytest_asyncio.fixture
async def address(db, user):
    from app.models.user import Address

    record = Address(
        user_id=user.id,
        label="Home",
        recipient_name="Asha Rao",
        recipient_phone="919876543210",
        line1="12 MG Road",
        city="Bengaluru",
        state="Karnataka",
        pincode="560001",
        is_default=True,
    )
    db.add(record)
    await db.flush()
    return record


@pytest.fixture
def auth_headers(user):
    from app.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user.id, 'user')}"}


@pytest.fixture
def agent_headers(agent):
    from app.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token(agent.id, 'agent', role=agent.role)}"}


@pytest.fixture
def admin_headers(admin):
    from app.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token(admin.id, 'agent', role=admin.role)}"}
