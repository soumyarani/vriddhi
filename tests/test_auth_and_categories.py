from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.main import app
from app.models import Base, Category, RefreshToken, User
from app.routers import auth as auth_router


@pytest.fixture()
def client(tmp_path: Path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    auth_router.rate_limiter._hits.clear()
    with TestClient(app) as test_client:
        yield test_client, TestingSessionLocal

    app.dependency_overrides.clear()


def test_google_auth_refresh_logout_and_me(client, monkeypatch):
    test_client, SessionLocal = client

    async def fake_google_user(*_args, **_kwargs):
        return {
            "id": "google-123",
            "email": "user@example.com",
            "name": "Example User",
            "picture": "https://example.com/pic.png",
        }

    monkeypatch.setattr(auth_router, "fetch_google_user", fake_google_user)

    auth_res = test_client.post("/api/auth/google", json={"code": "dummy-code"})
    assert auth_res.status_code == 200
    tokens = auth_res.json()
    assert tokens["access_token"]
    assert tokens["refresh_token"]

    me_res = test_client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer " + tokens["access_token"]},
    )
    assert me_res.status_code == 200
    assert me_res.json()["email"] == "user@example.com"

    refresh_res = test_client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refresh_res.status_code == 200
    rotated = refresh_res.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]

    old_refresh_res = test_client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert old_refresh_res.status_code == 401

    logout_res = test_client.post("/api/auth/logout", json={"refresh_token": rotated["refresh_token"]})
    assert logout_res.status_code == 204

    revoked_res = test_client.post("/api/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
    assert revoked_res.status_code == 401

    with SessionLocal() as session:
        user = session.query(User).filter(User.google_id == "google-123").one()
        token_rows = session.query(RefreshToken).filter(RefreshToken.user_id == user.id).all()
        assert len(token_rows) == 2
        assert any(t.revoked_at is not None for t in token_rows)


def test_categories_require_auth_and_filter_active(client, monkeypatch):
    test_client, SessionLocal = client

    async def fake_google_user(*_args, **_kwargs):
        return {
            "id": "google-456",
            "email": "buyer@example.com",
            "name": "Buyer",
            "picture": "https://example.com/buyer.png",
        }

    monkeypatch.setattr(auth_router, "fetch_google_user", fake_google_user)

    auth_res = test_client.post("/api/auth/google", json={"code": "dummy-code"})
    access = auth_res.json()["access_token"]

    with SessionLocal() as session:
        session.add_all(
            [
                Category(name="Fruits", sort_order=2, active=True),
                Category(name="Vegetables", sort_order=1, active=True),
                Category(name="Hidden", sort_order=0, active=False),
                Category(name="SoftDeleted", sort_order=0, active=True, deleted_at=datetime.now(timezone.utc)),
            ]
        )
        session.commit()

    unauth = test_client.get("/api/store/categories")
    assert unauth.status_code == 401

    categories_res = test_client.get(
        "/api/store/categories?limit=10&offset=0",
        headers={"Authorization": "Bearer " + access},
    )
    assert categories_res.status_code == 200
    names = [item["name"] for item in categories_res.json()]
    assert names == ["Vegetables", "Fruits"]


def test_google_auth_rate_limited(client, monkeypatch):
    test_client, _ = client

    auth_router.rate_limiter.max_requests = 1

    async def fake_google_user(*_args, **_kwargs):
        return {
            "id": "google-rate",
            "email": "rate@example.com",
            "name": "Rate",
            "picture": "https://example.com/rate.png",
        }

    monkeypatch.setattr(auth_router, "fetch_google_user", fake_google_user)

    first = test_client.post("/api/auth/google", json={"code": "ok"})
    assert first.status_code == 200

    second = test_client.post("/api/auth/google", json={"code": "again"})
    assert second.status_code == 429

    auth_router.rate_limiter.max_requests = 10
