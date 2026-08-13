from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.main import app
from app.models import Agent, Base, Category, Product, ProductVariant, ShippingConfig
from app.routers import auth as auth_router


@pytest.fixture()
def client(tmp_path: Path):
    db_path = tmp_path / "test-extended.db"
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


def _customer_token(test_client: TestClient, monkeypatch):
    async def fake_google_user(*_args, **_kwargs):
        return {"id": "google-user-1", "email": "shopper@example.com", "name": "Shopper", "picture": "https://example.com/u.png"}

    monkeypatch.setattr(auth_router, "fetch_google_user", fake_google_user)
    response = test_client.post("/api/auth/google", json={"code": "ok"})
    assert response.status_code == 200
    return response.json()["access_token"]


def test_store_checkout_and_order_flow(client, monkeypatch):
    test_client, SessionLocal = client
    token = _customer_token(test_client, monkeypatch)

    with SessionLocal() as session:
        cat = Category(name="Electronics", active=True, sort_order=1)
        session.add(cat)
        session.flush()
        product = Product(name="Laptop", description="Portable", base_price=50000, category_id=cat.id, active=True)
        session.add(product)
        session.flush()
        session.add(ProductVariant(product_id=product.id, sku="LP-13", name="13-inch", attributes={"size": "13"}, stock=5))
        session.add(ShippingConfig(zone="IN", min_weight=0, max_weight=10, base_cost=99, active=True))
        session.commit()

    products = test_client.get("/api/store/products", headers={"Authorization": "Bearer " + token})
    assert products.status_code == 200
    product_id = products.json()["items"][0]["id"]

    add_item = test_client.post(
        "/api/store/cart/items",
        json={"product_id": product_id, "quantity": 1},
        headers={"Authorization": "Bearer " + token},
    )
    assert add_item.status_code == 200

    add_address = test_client.post(
        "/api/store/profile/addresses",
        json={"line1": "Line 1", "city": "Bengaluru", "state": "KA", "pincode": "560001"},
        headers={"Authorization": "Bearer " + token},
    )
    assert add_address.status_code == 200
    address_id = add_address.json()["id"]

    checkout = test_client.post(
        "/api/store/checkout",
        json={"address_id": address_id},
        headers={"Authorization": "Bearer " + token},
    )
    assert checkout.status_code == 200
    assert checkout.json()["payment_link"]

    orders = test_client.get("/api/store/orders", headers={"Authorization": "Bearer " + token})
    assert orders.status_code == 200
    assert len(orders.json()["items"]) == 1


def test_agent_admin_and_webhook_endpoints(client, monkeypatch):
    test_client, SessionLocal = client

    with SessionLocal() as session:
        session.add(Agent(name="Admin", email="admin@company.com", google_id="google-admin-1", role="admin", active=True))
        session.commit()

    async def fake_admin_google(*_args, **_kwargs):
        return {"id": "google-admin-1", "email": "admin@company.com", "name": "Admin", "picture": "https://example.com/a.png"}

    monkeypatch.setattr(auth_router, "fetch_google_user", fake_admin_google)
    auth = test_client.post("/api/auth/agent/google", json={"code": "ok"})
    assert auth.status_code == 200
    agent_token = auth.json()["access_token"]

    dashboard = test_client.get("/api/admin/dashboard/stats", headers={"Authorization": "Bearer " + agent_token})
    assert dashboard.status_code == 200

    webhook = test_client.post("/webhook", json={"object": "whatsapp_business_account", "entry": [{"id": "evt-1"}]})
    assert webhook.status_code == 200

    webhook_repeat = test_client.post("/webhook", json={"object": "whatsapp_business_account", "entry": [{"id": "evt-1"}]})
    assert webhook_repeat.status_code == 200
