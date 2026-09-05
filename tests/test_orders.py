"""Order lifecycle: status transitions, settlement, cancellation, returns."""

from __future__ import annotations

import pytest

from app.errors import ConflictError, NotFoundError, ValidationError
from app.models.enums import OrderStatus
from app.services import cart as cart_service
from app.services import order as order_service


@pytest.fixture(autouse=True)
def stub_payment_link(monkeypatch):
    async def _fake(db, order, user):
        order.payment_link = "https://payments.test/link/abc"
        return type("Link", (), {"url": order.payment_link, "id": "cf-link-1"})()

    monkeypatch.setattr("app.services.payment.create_payment_link", _fake)


@pytest.fixture
async def placed_order(db, user, product, variant, address):
    await cart_service.add_item(db, user.id, product.id, variant.id, 2)
    order, _ = await order_service.checkout(db, user, address_id=address.id)
    return order


async def test_new_order_awaits_payment(placed_order):
    assert placed_order.status == OrderStatus.PENDING_PAYMENT


async def test_marking_paid_moves_to_pending_confirmation(db, placed_order):
    """Payment does not confirm an order — a human still has to accept it."""
    order = await order_service.mark_paid(db, placed_order.id)
    assert order.status == OrderStatus.PENDING_CONFIRMATION


async def test_payment_commits_the_stock_hold(db, placed_order, variant):
    await db.refresh(variant)
    assert variant.stock == 10

    await order_service.mark_paid(db, placed_order.id)
    await db.refresh(variant)
    assert variant.stock == 8


async def test_marking_paid_twice_is_a_no_op(db, placed_order, variant):
    """Cashfree can deliver the same success event more than once."""
    await order_service.mark_paid(db, placed_order.id)
    await order_service.mark_paid(db, placed_order.id)

    await db.refresh(variant)
    assert variant.stock == 8


async def test_agent_confirmation_records_an_eta(db, placed_order):
    from datetime import datetime, timedelta, timezone

    await order_service.mark_paid(db, placed_order.id)
    eta = datetime.now(timezone.utc) + timedelta(days=3)
    order = await order_service.confirm_order(db, placed_order.id, delivery_eta=eta)

    assert order.status == OrderStatus.CONFIRMED
    assert order.confirmed_at is not None
    assert order.delivery_eta is not None


async def test_reconfirming_keeps_existing_tracking(db, placed_order):
    """Adding a note later must not blank out tracking set earlier."""
    await order_service.mark_paid(db, placed_order.id)
    await order_service.confirm_order(db, placed_order.id, tracking_number="TRK-1")
    order = await order_service.confirm_order(db, placed_order.id, note="Called customer")

    assert order.tracking_number == "TRK-1"
    assert order.agent_notes == "Called customer"


async def test_illegal_transition_is_rejected(db, placed_order):
    with pytest.raises(ConflictError):
        await order_service.transition(db, placed_order, OrderStatus.DELIVERED)


async def test_agents_cannot_set_arbitrary_statuses(db, placed_order):
    await order_service.mark_paid(db, placed_order.id)
    with pytest.raises(ValidationError):
        await order_service.update_status(db, placed_order.id, "refunded")


async def test_unknown_status_is_rejected(db, placed_order):
    with pytest.raises(ValidationError):
        await order_service.update_status(db, placed_order.id, "teleported")


async def test_cancelling_releases_the_stock_hold(db, placed_order, variant):
    await order_service.cancel_order(db, placed_order, reason="Changed my mind")

    assert placed_order.status == OrderStatus.CANCELLED
    assert await cart_service.available_stock(db, variant, exclude_cart_id=None) == 10


async def test_cancelling_after_payment_restores_stock(db, placed_order, variant):
    await order_service.mark_paid(db, placed_order.id)
    await db.refresh(variant)
    assert variant.stock == 8

    await order_service.cancel_order(db, placed_order, reason="Out of area")
    await db.refresh(variant)
    assert variant.stock == 10


async def _walk_to_delivered(db, order):
    """Drive an order through the full fulfilment chain.

    The chain is paid → pending_confirmation → confirmed → processing →
    shipped → delivered; no step may be skipped.
    """
    await order_service.mark_paid(db, order.id)
    await order_service.confirm_order(db, order.id)
    for status in (OrderStatus.PROCESSING, OrderStatus.SHIPPED, OrderStatus.DELIVERED):
        await order_service.update_status(db, order.id, status)


async def test_delivered_orders_cannot_be_cancelled(db, placed_order):
    await _walk_to_delivered(db, placed_order)

    with pytest.raises((ConflictError, ValidationError)):
        await order_service.cancel_order(db, placed_order, reason="too late")


async def test_return_requires_delivery(db, placed_order):
    with pytest.raises((ConflictError, ValidationError)):
        await order_service.request_return(db, placed_order, reason="Not needed")


async def test_return_is_accepted_after_delivery(db, placed_order):
    await _walk_to_delivered(db, placed_order)

    order = await order_service.request_return(db, placed_order, reason="Wrong colour")
    assert order.return_requested_at is not None


async def test_another_user_cannot_fetch_the_order(db, placed_order):
    from app.models.enums import AuthProvider
    from app.models.user import User

    intruder = User(email="nosy@example.com", name="Nosy", google_id="g-nosy",
                    auth_provider=AuthProvider.GOOGLE)
    db.add(intruder)
    await db.flush()

    # A 404 rather than a 403: order ids must not be probeable.
    with pytest.raises(NotFoundError):
        await order_service.get_user_order(db, intruder.id, placed_order.id)


async def test_order_numbers_are_unique_and_sequential(db, user, product, variant, address):
    await cart_service.add_item(db, user.id, product.id, variant.id, 1)
    first, _ = await order_service.checkout(db, user, address_id=address.id)
    await cart_service.add_item(db, user.id, product.id, variant.id, 1)
    second, _ = await order_service.checkout(db, user, address_id=address.id)

    assert first.order_number != second.order_number
