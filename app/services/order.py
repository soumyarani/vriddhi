"""Order lifecycle: checkout orchestration, status transitions, cancellation and returns."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models.cart import Cart, InventoryReservation
from app.models.enums import (
    AGENT_SETTABLE_STATUSES,
    NON_CANCELLABLE_STATUSES,
    ORDER_TRANSITIONS,
    CartStatus,
    OrderStatus,
)
from app.models.order import Order, OrderItem
from app.models.review import Review
from app.models.user import Address, User
from app.pagination import apply_cursor, build_page
from app.services import cart as cart_service
from app.services import coupon as coupon_service
from app.services.tax import q, state_code_for
from logging_config import get_logger

log = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Checkout
# --------------------------------------------------------------------------
async def resolve_address(
    db: AsyncSession, user: User, address_id: int | None, new_address: dict | None
) -> Address:
    if address_id is not None:
        address = (
            await db.execute(
                select(Address).where(
                    Address.id == address_id,
                    Address.user_id == user.id,
                    Address.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if address is None:
            raise NotFoundError("Address not found")
        return address

    if new_address:
        from app.schemas.user import AddressCreate
        from app.services.user import create_address

        return await create_address(db, user.id, AddressCreate(**new_address))

    default = (
        await db.execute(
            select(Address)
            .where(
                Address.user_id == user.id,
                Address.deleted_at.is_(None),
                Address.is_default.is_(True),
            )
            .limit(1)
        )
    ).scalars().first()

    if default is None:
        raise ValidationError("Please add a delivery address before checking out")
    return default


async def checkout(
    db: AsyncSession,
    user: User,
    address_id: int | None = None,
    new_address: dict | None = None,
    channel: str = "web",
) -> tuple[Order, Any]:
    """Validate, reserve, price, and create an order with a payment link.

    Ordering matters: inventory is reserved *before* the order row exists, so
    a stock failure aborts with nothing written. The coupon is redeemed last,
    under the row lock taken during validation, so the usage count and the
    order insert commit together.
    """
    from app.services.payment import create_payment_link

    cart = await cart_service.get_active_cart(db, user.id)
    if cart is None or not cart.items:
        raise ValidationError("Your cart is empty")

    await cart_service.assert_cart_ready(db, cart)

    address = await resolve_address(db, user, address_id, new_address)

    from app.services.shipping import is_serviceable

    if not await is_serviceable(db, address.pincode):
        raise ValidationError(f"We do not deliver to {address.pincode} yet")

    reservations = await cart_service.reserve_inventory(db, cart)

    buyer_state = state_code_for(address.state)
    priced = await cart_service.price_cart(
        db, cart, pincode=address.pincode, buyer_state_code=buyer_state
    )

    if priced["has_stock_issues"]:
        await cart_service.release_reservations(db, cart.id, reason="stock_issue")
        raise ConflictError("Some items in your cart are no longer available")

    totals = priced["totals"]

    locked_coupon = None
    discount = totals["discount_amount"]
    if cart.coupon_id and cart.coupon is not None:
        locked_coupon, discount = await coupon_service.validate_coupon(
            db, cart.coupon.code, user.id, totals["subtotal"], lock=True
        )

    order = Order(
        order_number=f"TMP-{uuid.uuid4().hex}",
        user_id=user.id,
        cart_id=cart.id,
        address_snapshot=address.snapshot(),
        subtotal=totals["subtotal"],
        shipping_cost=totals["shipping_cost"],
        tax_amount=totals["tax_amount"],
        tax_breakup=totals["tax_breakup"],
        discount_amount=discount,
        total=totals["total"],
        currency=totals["currency"],
        status=OrderStatus.PENDING_PAYMENT,
        channel=channel,
        coupon_id=locked_coupon.id if locked_coupon else None,
        coupon_code=locked_coupon.code if locked_coupon else None,
    )
    db.add(order)
    await db.flush()

    # Derived from the primary key, so the number is unique without a
    # separate sequence or a racy MAX() lookup.
    order.order_number = f"ORD-{_now().year}-{order.id:05d}"

    for line in priced["lines"]:
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=line["product_id"],
                variant_id=line["variant_id"],
                product_name=line["product_name"],
                variant_name=line["variant_name"],
                sku=line["sku"],
                hsn_code=line["hsn_code"],
                image_url=line["image_url"],
                quantity=line["quantity"],
                unit_price=line["unit_price"],
                line_subtotal=line["line_subtotal"],
                line_discount=line["line_discount"],
                tax_rate=line["tax_rate"],
                tax_amount=line["tax_amount"],
                line_total=line["line_total"],
            )
        )

    for reservation in reservations:
        reservation.order_id = order.id

    if locked_coupon is not None:
        await coupon_service.record_usage(db, locked_coupon.id, user.id, order.id, discount)

    cart.status = CartStatus.CHECKED_OUT
    await db.flush()

    payment = await create_payment_link(db, order, user)
    await db.flush()

    log.info(
        "order_created",
        order_id=order.id,
        order_number=order.order_number,
        total=str(order.total),
        channel=channel,
    )
    return order, payment


async def retry_payment(db: AsyncSession, user: User, order_id: int) -> tuple[Order, Any]:
    """Issue a fresh payment link, re-reserving stock for the new attempt."""
    from app.services.payment import create_payment_link

    order = await get_user_order(db, user.id, order_id)
    if order.status != OrderStatus.PENDING_PAYMENT:
        raise ConflictError("This order is not awaiting payment")

    await cart_service.release_reservations(db, order_id=order.id, reason="payment_retry")

    expires_at = _now() + timedelta(minutes=settings.reservation_ttl_minutes)
    for item in order.items:
        if item.variant_id is None:
            continue

        from app.models.product import ProductVariant

        variant = (
            await db.execute(
                select(ProductVariant).where(ProductVariant.id == item.variant_id).with_for_update()
            )
        ).scalar_one_or_none()
        if variant is None:
            raise ValidationError(f"{item.product_name} is no longer available")

        held = await cart_service.reserved_quantity(db, variant.id)
        if item.quantity > variant.stock - held:
            raise ConflictError(f"{item.product_name} is no longer in stock")

        db.add(
            InventoryReservation(
                cart_id=order.cart_id,
                order_id=order.id,
                variant_id=variant.id,
                quantity=item.quantity,
                expires_at=expires_at,
            )
        )

    await db.flush()
    payment = await create_payment_link(db, order, user)
    log.info("payment_retried", order_id=order.id)
    return order, payment


# --------------------------------------------------------------------------
# Status transitions
# --------------------------------------------------------------------------
def can_transition(current: str, target: str) -> bool:
    allowed = ORDER_TRANSITIONS.get(OrderStatus(current), set())
    return OrderStatus(target) in allowed


async def transition(db: AsyncSession, order: Order, target: OrderStatus) -> Order:
    if order.status == target:
        return order
    if not can_transition(order.status, target):
        raise ConflictError(f"Cannot move an order from {order.status} to {target}")

    order.status = target
    stamps = {
        OrderStatus.CONFIRMED: "confirmed_at",
        OrderStatus.SHIPPED: "shipped_at",
        OrderStatus.DELIVERED: "delivered_at",
        OrderStatus.CANCELLED: "cancelled_at",
    }
    if target in stamps:
        setattr(order, stamps[target], _now())

    await db.flush()
    log.info("order_status_changed", order_id=order.id, status=target)
    return order


async def mark_paid(db: AsyncSession, order_id: int) -> Order:
    """Settle a paid order: commit the stock hold and queue it for an agent."""
    order = await get_order(db, order_id)

    if order.status != OrderStatus.PENDING_PAYMENT:
        log.info("order_already_settled", order_id=order_id, status=order.status)
        return order

    await transition(db, order, OrderStatus.PAID)
    await cart_service.commit_reservations(db, order.id)
    await transition(db, order, OrderStatus.PENDING_CONFIRMATION)
    return order


async def confirm_order(
    db: AsyncSession,
    order_id: int,
    delivery_eta: datetime | None = None,
    tracking_number: str | None = None,
    tracking_url: str | None = None,
    note: str | None = None,
) -> Order:
    order = await get_order(db, order_id)
    await transition(db, order, OrderStatus.CONFIRMED)

    # Only overwrite what the agent actually supplied. Confirming a second time
    # to add a note must not blank out an ETA or tracking link set earlier.
    if delivery_eta is not None:
        order.delivery_eta = delivery_eta
    if tracking_number:
        order.tracking_number = tracking_number
    if tracking_url:
        order.tracking_url = tracking_url
    if note:
        order.agent_notes = note

    await db.flush()
    return order


async def update_status(
    db: AsyncSession,
    order_id: int,
    target: str,
    tracking_number: str | None = None,
    tracking_url: str | None = None,
    note: str | None = None,
) -> Order:
    try:
        target_status = OrderStatus(target)
    except ValueError as exc:
        raise ValidationError(f"Unknown order status: {target}") from exc

    if target_status not in AGENT_SETTABLE_STATUSES:
        raise ValidationError(f"Agents cannot set an order to {target}")

    order = await get_order(db, order_id)
    await transition(db, order, target_status)

    if tracking_number:
        order.tracking_number = tracking_number
    if tracking_url:
        order.tracking_url = tracking_url
    if note:
        order.agent_notes = note

    await db.flush()
    return order


# --------------------------------------------------------------------------
# Cancellation and returns
# --------------------------------------------------------------------------
async def cancel_order(
    db: AsyncSession, order: Order, reason: str, by_agent: bool = False
) -> Order:
    if OrderStatus(order.status) in NON_CANCELLABLE_STATUSES:
        raise ConflictError("This order can no longer be cancelled")

    was_paid = OrderStatus(order.status) in (
        OrderStatus.PAID,
        OrderStatus.PENDING_CONFIRMATION,
        OrderStatus.CONFIRMED,
        OrderStatus.PROCESSING,
    )

    order.status = OrderStatus.CANCELLED
    order.cancelled_at = _now()
    order.cancel_reason = reason

    if was_paid:
        await cart_service.restore_stock(db, order.id)
    else:
        await cart_service.release_reservations(db, order_id=order.id, reason="order_cancelled")

    # Free the redemption so the customer can use the coupon again.
    await coupon_service.release_usage(db, order.id)

    await db.flush()
    log.info("order_cancelled", order_id=order.id, by_agent=by_agent, was_paid=was_paid)
    return order


async def request_return(db: AsyncSession, order: Order, reason: str) -> Order:
    if OrderStatus(order.status) != OrderStatus.DELIVERED:
        raise ConflictError("Only delivered orders can be returned")

    delivered = _aware(order.delivered_at) or _aware(order.created_at)
    window_ends = delivered + timedelta(days=settings.return_window_days)
    if _now() > window_ends:
        raise ConflictError(
            f"The {settings.return_window_days}-day return window for this order has closed"
        )

    await transition(db, order, OrderStatus.RETURN_REQUESTED)
    order.return_requested_at = _now()
    order.return_reason = reason
    await db.flush()

    log.info("return_requested", order_id=order.id)
    return order


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------
async def get_order(db: AsyncSession, order_id: int) -> Order:
    order = (
        await db.execute(
            select(Order)
            .options(
                selectinload(Order.items),
                selectinload(Order.payments),
                selectinload(Order.refunds),
                selectinload(Order.user),
            )
            .where(Order.id == order_id)
        )
    ).scalar_one_or_none()
    if order is None:
        raise NotFoundError("Order not found")
    return order


async def get_user_order(db: AsyncSession, user_id: int, order_id: int) -> Order:
    order = await get_order(db, order_id)
    if order.user_id != user_id:
        # Same response as a missing order so IDs cannot be probed.
        raise NotFoundError("Order not found")
    return order


async def get_order_by_number(db: AsyncSession, order_number: str) -> Order | None:
    return (
        await db.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(func.upper(Order.order_number) == order_number.strip().upper())
        )
    ).scalar_one_or_none()


async def list_orders(
    db: AsyncSession,
    user_id: int | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    stmt = select(Order).options(selectinload(Order.items))

    if user_id is not None:
        stmt = stmt.where(Order.user_id == user_id)
    if status:
        stmt = stmt.where(Order.status == status)
    if date_from is not None:
        stmt = stmt.where(Order.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Order.created_at <= date_to)

    stmt = apply_cursor(stmt, Order.created_at, Order.id, cursor, descending=True)
    rows = (await db.execute(stmt.limit(limit + 1))).scalars().unique().all()
    items, next_cursor, has_more = build_page(rows, limit)

    return {
        "items": [serialize_order_summary(o) for o in items],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


def serialize_order_summary(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "order_number": order.order_number,
        "status": order.status,
        "total": order.total,
        "currency": order.currency,
        "item_count": sum(i.quantity for i in order.items),
        "channel": order.channel,
        "delivery_eta": order.delivery_eta,
        "tracking_number": order.tracking_number,
        "created_at": order.created_at,
    }


def can_cancel(order: Order) -> bool:
    return OrderStatus(order.status) not in NON_CANCELLABLE_STATUSES


def can_return(order: Order) -> bool:
    if OrderStatus(order.status) != OrderStatus.DELIVERED:
        return False
    delivered = _aware(order.delivered_at) or _aware(order.created_at)
    return _now() <= delivered + timedelta(days=settings.return_window_days)


async def serialize_order_detail(db: AsyncSession, order: Order) -> dict[str, Any]:
    reviewed_products = set(
        (
            await db.execute(
                select(Review.product_id).where(
                    Review.order_id == order.id, Review.deleted_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )

    detail = serialize_order_summary(order)
    detail.update(
        {
            "address_snapshot": order.address_snapshot or {},
            "subtotal": order.subtotal,
            "shipping_cost": order.shipping_cost,
            "tax_amount": order.tax_amount,
            "tax_breakup": order.tax_breakup or {},
            "discount_amount": order.discount_amount,
            "coupon_code": order.coupon_code,
            "tracking_url": order.tracking_url,
            "cancel_reason": order.cancel_reason,
            "return_reason": order.return_reason,
            "confirmed_at": order.confirmed_at,
            "shipped_at": order.shipped_at,
            "delivered_at": order.delivered_at,
            "cancelled_at": order.cancelled_at,
            "can_cancel": can_cancel(order),
            "can_return": can_return(order),
            "can_review": OrderStatus(order.status) == OrderStatus.DELIVERED,
            "items": [
                {
                    "id": i.id,
                    "product_id": i.product_id,
                    "variant_id": i.variant_id,
                    "product_name": i.product_name,
                    "variant_name": i.variant_name,
                    "sku": i.sku,
                    "hsn_code": i.hsn_code,
                    "image_url": i.image_url,
                    "quantity": i.quantity,
                    "unit_price": i.unit_price,
                    "line_subtotal": i.line_subtotal,
                    "line_discount": i.line_discount,
                    "tax_rate": i.tax_rate,
                    "tax_amount": i.tax_amount,
                    "line_total": i.line_total,
                    "reviewed": i.product_id in reviewed_products,
                }
                for i in order.items
            ],
            "payments": [
                {
                    "id": p.id,
                    "status": p.status,
                    "amount": p.amount,
                    "paid_amount": p.paid_amount,
                    "payment_method": p.payment_method,
                    "payment_link": p.payment_link,
                    "cashfree_order_id": p.cashfree_order_id,
                    "expires_at": p.expires_at,
                    "paid_at": p.paid_at,
                    "created_at": p.created_at,
                }
                for p in sorted(order.payments, key=lambda x: x.created_at, reverse=True)
            ],
            "refunds": [
                {
                    "id": r.id,
                    "amount": r.amount,
                    "status": r.status,
                    "reason": r.reason,
                    "cashfree_refund_id": r.cashfree_refund_id,
                    "processed_at": r.processed_at,
                    "created_at": r.created_at,
                }
                for r in order.refunds
            ],
        }
    )
    return detail


async def active_orders_for_ai(db: AsyncSession, user_id: int, limit: int = 5) -> list[dict[str, Any]]:
    """Compact recent-order list injected into the AI system prompt."""
    stmt = (
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.user_id == user_id)
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    orders = (await db.execute(stmt)).scalars().unique().all()
    return [
        {
            "order_number": o.order_number,
            "status": o.status,
            "total": float(o.total),
            "items": [f"{i.product_name} x{i.quantity}" for i in o.items][:5],
            "delivery_eta": o.delivery_eta.isoformat() if o.delivery_eta else None,
            "tracking_number": o.tracking_number,
        }
        for o in orders
    ]
