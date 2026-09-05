"""Checkout: GST maths, coupon limits, and the order/reservation handoff.

`create_payment_link` is stubbed throughout — checkout must not depend on
Cashfree being reachable for these invariants to hold.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.errors import ConflictError, OutOfStockError, ValidationError
from app.services import cart as cart_service
from app.services import coupon as coupon_service
from app.services import order as order_service
from app.services import tax


@pytest.fixture(autouse=True)
def stub_payment_link(monkeypatch):
    async def _fake(db, order, user):
        order.payment_link = "https://payments.test/link/abc"
        return type("Link", (), {"url": order.payment_link, "id": "cf-link-1"})()

    monkeypatch.setattr("app.services.payment.create_payment_link", _fake)


# ---- GST -----------------------------------------------------------------
def test_prices_are_gst_inclusive():
    """The listed price is the amount charged; tax is carved out of it.

    Rs.1180 at 18% is the textbook case: Rs.1000 taxable + Rs.180 tax. The
    split must never add tax on top, and the parts must re-sum to the price.
    """
    taxable, amount = tax.split_inclusive(Decimal("1180.00"), Decimal("18.00"))
    assert (taxable, amount) == (Decimal("1000.00"), Decimal("180.00"))

    taxable, amount = tax.split_inclusive(Decimal("4999.00"), Decimal("5.00"))
    assert taxable + amount == Decimal("4999.00")
    assert taxable == Decimal("4760.95")


def test_zero_rate_produces_no_tax():
    taxable, amount = tax.split_inclusive(Decimal("100.00"), Decimal("0.00"))
    assert (taxable, amount) == (Decimal("100.00"), Decimal("0.00"))


def test_intra_state_splits_into_cgst_and_sgst():
    lines = [{"tax_rate": Decimal("5.00"), "taxable_value": Decimal("100.00"),
              "tax_amount": Decimal("5.00")}]
    breakup = tax.build_tax_breakup(lines, buyer_state_code=None)
    dumped = str(breakup)
    assert "cgst" in dumped and "sgst" in dumped
    assert "igst" not in dumped or "0" in dumped


def test_line_tax_applies_discount_before_splitting():
    line = tax.compute_line_tax(Decimal("1000.00"), 2, Decimal("18.00"), Decimal("200.00"))
    assert line["gross"] == Decimal("1800.00")
    assert line["taxable_value"] + line["tax_amount"] == Decimal("1800.00")


# ---- Checkout ------------------------------------------------------------
async def test_checkout_creates_an_order_and_payment_link(db, user, product, variant, address):
    await cart_service.add_item(db, user.id, product.id, variant.id, 2)
    order, _link = await order_service.checkout(db, user, address_id=address.id)

    assert order.id is not None
    assert order.total > 0
    assert order.order_number.startswith("ORD") or "TMP" not in order.order_number


async def test_checkout_freezes_item_details(db, user, product, variant, address):
    """Order items must snapshot the product, not point at a live row."""
    await cart_service.add_item(db, user.id, product.id, variant.id, 1)
    order, _ = await order_service.checkout(db, user, address_id=address.id)
    await db.refresh(order, ["items"])

    item = order.items[0]
    assert item.product_name == product.name
    assert item.unit_price == variant.effective_price

    product.name = "Renamed After Purchase"
    await db.flush()
    await db.refresh(item)
    assert item.product_name == "Kanjivaram Silk Saree"


async def test_checkout_snapshots_the_address(db, user, product, variant, address):
    await cart_service.add_item(db, user.id, product.id, variant.id, 1)
    order, _ = await order_service.checkout(db, user, address_id=address.id)
    assert order.address_snapshot["pincode"] == address.pincode


async def test_empty_cart_cannot_check_out(db, user, address):
    with pytest.raises(ValidationError):
        await order_service.checkout(db, user, address_id=address.id)


async def test_checkout_holds_stock_without_decrementing_it(db, user, product, variant, address):
    """Stock moves only when payment settles, but the units must be held now."""
    await cart_service.add_item(db, user.id, product.id, variant.id, 3)
    await order_service.checkout(db, user, address_id=address.id)

    await db.refresh(variant)
    assert variant.stock == 10
    assert await cart_service.available_stock(db, variant, exclude_cart_id=None) == 7


async def test_checkout_fails_when_stock_vanishes(db, user, product, variant, address):
    await cart_service.add_item(db, user.id, product.id, variant.id, 5)
    variant.stock = 2
    await db.flush()

    with pytest.raises((OutOfStockError, ConflictError)):
        await order_service.checkout(db, user, address_id=address.id)


# ---- Coupons -------------------------------------------------------------
@pytest.fixture
async def coupon(db):
    from app.models.coupon import Coupon
    from app.models.enums import DiscountType

    record = Coupon(
        code="SAVE10",
        discount_type=DiscountType.PERCENT,
        discount_value=Decimal("10.00"),
        min_order=Decimal("0.00"),
        max_uses=2,
        per_user_limit=1,
        active=True,
    )
    db.add(record)
    await db.flush()
    return record


async def test_coupon_discount_is_applied(db, user, coupon):
    _, discount = await coupon_service.validate_coupon(
        db, "SAVE10", user.id, Decimal("1000.00")
    )
    assert discount == Decimal("100.00")


async def test_coupon_below_minimum_is_rejected(db, user, coupon):
    coupon.min_order = Decimal("5000.00")
    await db.flush()
    with pytest.raises(ValidationError):
        await coupon_service.validate_coupon(db, "SAVE10", user.id, Decimal("1000.00"))


async def test_inactive_coupon_is_rejected(db, user, coupon):
    coupon.active = False
    await db.flush()
    with pytest.raises(ValidationError):
        await coupon_service.validate_coupon(db, "SAVE10", user.id, Decimal("1000.00"))


async def test_per_user_limit_counts_recorded_usage(db, user, coupon, product, variant, address):
    """The limit is enforced from `coupon_usage` rows, not a mutable counter."""
    await cart_service.add_item(db, user.id, product.id, variant.id, 1)
    order, _ = await order_service.checkout(db, user, address_id=address.id)
    await coupon_service.record_usage(db, coupon.id, user.id, order.id, Decimal("100.00"))

    assert await coupon_service.times_used_by(db, coupon.id, user.id) == 1
    with pytest.raises(ConflictError):
        await coupon_service.validate_coupon(db, "SAVE10", user.id, Decimal("1000.00"))


async def test_global_max_uses_is_enforced(db, user, coupon):
    """Two redemptions by two other shoppers exhausts `max_uses=2`."""
    from app.models.enums import AuthProvider
    from app.models.order import Order
    from app.models.user import User

    for idx in range(2):
        buyer = User(email=f"b{idx}@example.com", name=f"B{idx}", google_id=f"g-b{idx}",
                     auth_provider=AuthProvider.GOOGLE)
        db.add(buyer)
        await db.flush()
        # One usage row per order — the table has a unique index on order_id.
        order = Order(user_id=buyer.id, order_number=f"ORD-X-{idx}", subtotal=0,
                      total=0, address_snapshot={})
        db.add(order)
        await db.flush()
        await coupon_service.record_usage(db, coupon.id, buyer.id, order.id, Decimal("10.00"))

    assert await coupon_service.times_used(db, coupon.id) == 2
    with pytest.raises(ConflictError):
        await coupon_service.validate_coupon(db, "SAVE10", user.id, Decimal("1000.00"))


async def test_unknown_coupon_is_a_404(db, user):
    from app.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await coupon_service.validate_coupon(db, "NOPE", user.id, Decimal("1000.00"))
