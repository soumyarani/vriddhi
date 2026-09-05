"""Cart mutations and the reservation layer that guards oversell."""

from __future__ import annotations

import pytest

from app.errors import OutOfStockError
from app.services import cart as cart_service


async def test_add_item_creates_a_cart(db, user, product, variant):
    cart = await cart_service.add_item(db, user.id, product.id, variant.id, 2)
    assert len(cart.items) == 1
    assert cart.items[0].quantity == 2


async def test_adding_the_same_variant_accumulates(db, user, product, variant):
    await cart_service.add_item(db, user.id, product.id, variant.id, 2)
    cart = await cart_service.add_item(db, user.id, product.id, variant.id, 3)
    assert len(cart.items) == 1
    assert cart.items[0].quantity == 5


async def test_cannot_add_more_than_stock(db, user, product, variant):
    with pytest.raises(OutOfStockError):
        await cart_service.add_item(db, user.id, product.id, variant.id, variant.stock + 1)


async def test_accumulation_is_also_stock_checked(db, user, product, variant):
    """The second add must consider what is already in the cart, not just its own quantity."""
    await cart_service.add_item(db, user.id, product.id, variant.id, 8)
    with pytest.raises(OutOfStockError):
        await cart_service.add_item(db, user.id, product.id, variant.id, 5)


async def test_remove_item_empties_the_cart(db, user, product, variant):
    cart = await cart_service.add_item(db, user.id, product.id, variant.id, 1)
    cart = await cart_service.remove_item(db, user.id, cart.items[0].id)
    assert cart.items == []


async def test_update_item_quantity(db, user, product, variant):
    cart = await cart_service.add_item(db, user.id, product.id, variant.id, 1)
    cart = await cart_service.update_item(db, user.id, cart.items[0].id, 4)
    assert cart.items[0].quantity == 4


async def test_reservations_hide_stock_from_other_shoppers(db, user, product, variant):
    """A hold placed by one cart must reduce what a different cart can add.

    This is the oversell guard: physical stock is untouched at reservation
    time, so availability has to be computed as stock minus live holds.
    """
    from app.models.enums import AuthProvider
    from app.models.user import User

    cart = await cart_service.add_item(db, user.id, product.id, variant.id, 9)
    await cart_service.reserve_inventory(db, cart)

    other = User(email="other@example.com", name="Vik", google_id="g-2",
                 auth_provider=AuthProvider.GOOGLE)
    db.add(other)
    await db.flush()

    # 10 in stock, 9 held by the first cart — only 1 may be added.
    await cart_service.add_item(db, other.id, product.id, variant.id, 1)
    with pytest.raises(OutOfStockError):
        await cart_service.add_item(db, other.id, product.id, variant.id, 1)


async def test_reserving_does_not_decrement_physical_stock(db, user, product, variant):
    cart = await cart_service.add_item(db, user.id, product.id, variant.id, 3)
    await cart_service.reserve_inventory(db, cart)
    await db.refresh(variant)
    assert variant.stock == 10


async def test_releasing_a_reservation_returns_availability(db, user, product, variant):
    cart = await cart_service.add_item(db, user.id, product.id, variant.id, 10)
    await cart_service.reserve_inventory(db, cart)
    assert await cart_service.available_stock(db, variant, exclude_cart_id=None) == 0

    await cart_service.release_reservations(db, cart_id=cart.id)
    assert await cart_service.available_stock(db, variant, exclude_cart_id=None) == 10


async def test_re_reserving_supersedes_the_previous_hold(db, user, product, variant):
    """Reserving twice must not double-count the same cart's own hold."""
    cart = await cart_service.add_item(db, user.id, product.id, variant.id, 6)
    await cart_service.reserve_inventory(db, cart)
    await cart_service.reserve_inventory(db, cart)

    assert await cart_service.available_stock(db, variant, exclude_cart_id=None) == 4


async def test_committing_reservations_decrements_real_stock(db, user, product, variant):
    from app.models.order import Order

    cart = await cart_service.add_item(db, user.id, product.id, variant.id, 4)
    reservations = await cart_service.reserve_inventory(db, cart)

    order = Order(user_id=user.id, order_number="TEST-1", cart_id=cart.id,
                  subtotal=0, total=0, address_snapshot={})
    db.add(order)
    await db.flush()
    for res in reservations:
        res.order_id = order.id
    await db.flush()

    assert await cart_service.commit_reservations(db, order.id) == 1
    await db.refresh(variant)
    assert variant.stock == 6


async def test_empty_cart_cannot_be_reserved(db, user):
    cart = await cart_service.get_or_create_cart(db, user.id)
    from app.errors import ValidationError

    with pytest.raises(ValidationError):
        await cart_service.reserve_inventory(db, cart)


async def test_cart_endpoint_requires_auth(client):
    assert (await client.get("/api/cart")).status_code in (401, 403)
