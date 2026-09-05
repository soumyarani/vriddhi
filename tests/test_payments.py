"""Payment settlement.

The rule that matters most here: the amount in a callback is attacker-visible
input. It is only ever compared against our own recorded total, never trusted.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.enums import PaymentStatus
from app.services import cart as cart_service
from app.services import order as order_service
from app.services import payment as payment_service


@pytest.fixture(autouse=True)
def stub_payment_link(monkeypatch):
    async def _fake(db, order, user):
        order.payment_link = "https://payments.test/link/abc"
        return type("Link", (), {"url": order.payment_link, "id": "cf-link-1"})()

    monkeypatch.setattr("app.services.payment.create_payment_link", _fake)


@pytest.fixture
async def payment(db, user, product, variant, address):
    """An order with a pending payment row for its exact total."""
    from app.models.order import Payment

    await cart_service.add_item(db, user.id, product.id, variant.id, 1)
    order, _ = await order_service.checkout(db, user, address_id=address.id)

    record = Payment(
        order_id=order.id,
        amount=order.total,
        status=PaymentStatus.PENDING,
        idempotency_key=f"idem-{order.id}",
        cashfree_order_id=order.order_number,
    )
    db.add(record)
    await db.flush()
    return record


def success_payload(payment, amount) -> dict:
    return {
        "type": "PAYMENT_SUCCESS_WEBHOOK",
        "data": {
            "order": {"order_id": payment.cashfree_order_id},
            "payment": {
                "cf_payment_id": "cf-99",
                "payment_status": "SUCCESS",
                "payment_amount": float(amount),
                "payment_group": "upi",
            },
        },
    }


async def test_matching_amount_settles_the_payment(db, payment):
    result = await payment_service.settle_payment(db, success_payload(payment, payment.amount))

    assert result["settled"] is True
    assert payment.status == PaymentStatus.SUCCESS
    assert payment.paid_at is not None


async def test_underpayment_is_flagged_not_settled(db, payment):
    """Paying Rs.1 for a Rs.4999 order must never mark the order paid."""
    result = await payment_service.settle_payment(db, success_payload(payment, Decimal("1.00")))

    assert result["settled"] is False
    assert result["flagged"] is True
    assert payment.status == PaymentStatus.FLAGGED


async def test_overpayment_is_also_flagged(db, payment):
    """A mismatch in either direction means the callback disagrees with us."""
    inflated = payment.amount + Decimal("500.00")
    result = await payment_service.settle_payment(db, success_payload(payment, inflated))

    assert result["settled"] is False
    assert payment.status == PaymentStatus.FLAGGED


async def test_missing_amount_is_flagged(db, payment):
    payload = success_payload(payment, payment.amount)
    del payload["data"]["payment"]["payment_amount"]

    result = await payment_service.settle_payment(db, payload)
    assert result["settled"] is False
    assert payment.status == PaymentStatus.FLAGGED


async def test_flagged_payment_leaves_the_order_unpaid(db, payment):
    from app.models.enums import OrderStatus

    await payment_service.settle_payment(db, success_payload(payment, Decimal("1.00")))
    order = await order_service.get_order(db, payment.order_id)
    assert order.status == OrderStatus.PENDING_PAYMENT


async def test_failed_event_marks_the_payment_failed(db, payment):
    payload = success_payload(payment, payment.amount)
    payload["type"] = "PAYMENT_FAILED_WEBHOOK"
    payload["data"]["payment"]["payment_status"] = "FAILED"
    payload["data"]["payment"]["payment_message"] = "Insufficient funds"

    result = await payment_service.settle_payment(db, payload)
    assert result["settled"] is False
    assert payment.status == PaymentStatus.FAILED
    assert "Insufficient" in payment.failure_reason


async def test_user_dropped_is_treated_as_failure(db, payment):
    payload = success_payload(payment, payment.amount)
    payload["data"]["payment"]["payment_status"] = "USER_DROPPED"

    await payment_service.settle_payment(db, payload)
    assert payment.status == PaymentStatus.FAILED


async def test_replayed_success_is_ignored(db, payment):
    """Cashfree retries; settling twice must not double-apply anything."""
    payload = success_payload(payment, payment.amount)
    await payment_service.settle_payment(db, payload)
    second = await payment_service.settle_payment(db, payload)

    assert second["already_settled"] is True


async def test_unmatched_event_is_reported_not_raised(db):
    payload = {
        "type": "PAYMENT_SUCCESS_WEBHOOK",
        "data": {"order": {"order_id": "ORD-DOES-NOT-EXIST"},
                 "payment": {"payment_status": "SUCCESS", "payment_amount": 10.0}},
    }
    result = await payment_service.settle_payment(db, payload)
    assert result["matched"] is False


async def test_event_ids_distinguish_distinct_events():
    a = payment_service.extract_event_id(
        {"type": "PAYMENT_SUCCESS_WEBHOOK", "data": {"payment": {"cf_payment_id": "1"}}}
    )
    b = payment_service.extract_event_id(
        {"type": "PAYMENT_FAILED_WEBHOOK", "data": {"payment": {"cf_payment_id": "1"}}}
    )
    assert a != b


async def test_pending_status_does_not_settle(db, payment):
    payload = success_payload(payment, payment.amount)
    payload["data"]["payment"]["payment_status"] = "PENDING"

    result = await payment_service.settle_payment(db, payload)
    assert result.get("pending") is True
    assert payment.status == PaymentStatus.PENDING
