"""Storefront HTTP surface: catalogue, cart, wishlist, reviews.

These go through the real router stack, so they also cover response-model
validation — a schema that disagrees with its serializer shows up here as a
500 rather than passing silently.
"""

from __future__ import annotations

import pytest

from app.services import cart as cart_service
from app.services import order as order_service


# ---- Catalogue (public) ---------------------------------------------------
async def test_product_list_is_public(client, product):
    resp = await client.get("/api/products")
    assert resp.status_code == 200
    assert [p["name"] for p in resp.json()["items"]] == [product.name]


async def test_product_detail_includes_variants(client, product, variant):
    resp = await client.get(f"/api/products/{product.id}")
    assert resp.status_code == 200
    assert [v["sku"] for v in resp.json()["variants"]] == [variant.sku]


async def test_missing_product_is_a_404(client):
    resp = await client.get("/api/products/999999")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


async def test_soft_deleted_product_is_hidden(client, db, product):
    product.soft_delete()
    await db.commit()

    assert (await client.get(f"/api/products/{product.id}")).status_code == 404
    assert (await client.get("/api/products")).json()["items"] == []


async def test_categories_are_listed(client, category):
    """Categories are a bare list — the set is small enough not to paginate."""
    resp = await client.get("/api/categories")
    assert resp.status_code == 200
    assert [c["name"] for c in resp.json()] == [category.name]


async def test_search_matches_on_name(client, product):
    resp = await client.get("/api/products/search", params={"q": "Kanjivaram"})
    assert resp.status_code == 200
    assert resp.json()["items"]


async def test_pagination_reports_more_pages(client, db, category):
    """`has_more` must be exact, which is why the query over-fetches by one."""
    from decimal import Decimal

    from app.models.product import Product

    for i in range(5):
        db.add(Product(name=f"Item {i}", base_price=Decimal("100.00"),
                       category_id=category.id, active=True, image_urls=[]))
    await db.commit()

    resp = await client.get("/api/products", params={"limit": 2})
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert body["next_cursor"]


# ---- Cart (authenticated) -------------------------------------------------
async def test_cart_starts_empty(client, auth_headers):
    resp = await client.get("/api/cart", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["items"] == []


async def test_add_to_cart_over_http(client, auth_headers, product, variant):
    resp = await client.post(
        "/api/cart/items",
        headers=auth_headers,
        json={"product_id": product.id, "variant_id": variant.id, "quantity": 2},
    )
    assert resp.status_code in (200, 201)
    assert resp.json()["items"][0]["quantity"] == 2


async def test_adding_beyond_stock_is_a_conflict(client, auth_headers, product, variant):
    resp = await client.post(
        "/api/cart/items",
        headers=auth_headers,
        json={"product_id": product.id, "variant_id": variant.id, "quantity": 99},
    )
    assert resp.status_code == 409
    assert "left" in resp.json()["detail"] or "stock" in resp.json()["detail"].lower()


async def test_cart_is_per_user(client, db, auth_headers, product, variant):
    """One shopper must never see another's cart."""
    from app.auth import create_access_token
    from app.models.enums import AuthProvider
    from app.models.user import User

    await client.post(
        "/api/cart/items",
        headers=auth_headers,
        json={"product_id": product.id, "variant_id": variant.id, "quantity": 1},
    )

    other = User(email="other@example.com", name="Other", google_id="g-other",
                 auth_provider=AuthProvider.GOOGLE)
    db.add(other)
    await db.commit()

    resp = await client.get(
        "/api/cart",
        headers={"Authorization": f"Bearer {create_access_token(other.id, 'user')}"},
    )
    assert resp.json()["items"] == []


async def test_invalid_quantity_is_rejected(client, auth_headers, product, variant):
    resp = await client.post(
        "/api/cart/items",
        headers=auth_headers,
        json={"product_id": product.id, "variant_id": variant.id, "quantity": 0},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


# ---- Wishlist -------------------------------------------------------------
async def test_wishlist_add_and_list(client, auth_headers, product):
    added = await client.post(f"/api/wishlist/{product.id}", headers=auth_headers)
    assert added.status_code in (200, 201)

    listed = await client.get("/api/wishlist", headers=auth_headers)
    assert listed.status_code == 200
    assert [item["product_id"] for item in listed.json()] == [product.id]


async def test_wishlist_add_is_idempotent(client, auth_headers, product):
    await client.post(f"/api/wishlist/{product.id}", headers=auth_headers)
    second = await client.post(f"/api/wishlist/{product.id}", headers=auth_headers)
    assert second.status_code in (200, 201, 409)

    listed = await client.get("/api/wishlist", headers=auth_headers)
    assert len(listed.json()) == 1


# ---- Reviews --------------------------------------------------------------
@pytest.fixture(autouse=True)
def stub_payment_link(monkeypatch):
    async def _fake(db, order, user):
        order.payment_link = "https://payments.test/x"
        return type("Link", (), {"url": order.payment_link, "id": "cf-1"})()

    monkeypatch.setattr("app.services.payment.create_payment_link", _fake)


async def test_product_reviews_endpoint_serializes(client, db, user, product, variant, address):
    """Public review listing must not leak user_id or order_id."""
    from app.models.enums import OrderStatus
    from app.services import review as review_service

    await cart_service.add_item(db, user.id, product.id, variant.id, 1)
    order, _ = await order_service.checkout(db, user, address_id=address.id)
    await order_service.mark_paid(db, order.id)
    await order_service.confirm_order(db, order.id)
    for status in (OrderStatus.PROCESSING, OrderStatus.SHIPPED, OrderStatus.DELIVERED):
        await order_service.update_status(db, order.id, status)

    await review_service.create_review(db, user.id, order.id, product.id, 5, "Beautiful weave")
    await db.commit()

    resp = await client.get(f"/api/products/{product.id}/reviews")
    assert resp.status_code == 200

    body = resp.json()["items"][0]
    assert body["rating"] == 5
    assert body["verified_purchase"] is True
    assert "user_id" not in body
    assert "order_id" not in body


async def test_review_requires_a_delivered_order(db, user, product, variant, address):
    """Verified purchase means delivered — not merely paid."""
    from app.errors import ValidationError
    from app.services import review as review_service

    await cart_service.add_item(db, user.id, product.id, variant.id, 1)
    order, _ = await order_service.checkout(db, user, address_id=address.id)

    with pytest.raises(ValidationError):
        await review_service.create_review(db, user.id, order.id, product.id, 5, "Too soon")
