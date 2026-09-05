"""Cart operations, stock availability, and inventory reservation.

Stock accounting has two layers. `ProductVariant.stock` is the physical count
and only moves when a sale completes. `InventoryReservation` rows hold stock
for carts that are mid-payment. Available stock is therefore:

    available = variant.stock - SUM(active reservations)

so two shoppers cannot both buy the last unit while one of them is on the
payment page.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.errors import ConflictError, NotFoundError, OutOfStockError, ValidationError
from app.models.cart import Cart, CartItem, InventoryReservation
from app.models.enums import CartStatus
from app.models.product import Product, ProductVariant
from app.services import coupon as coupon_service
from app.services.tax import compute_line_tax, q
from logging_config import get_logger

log = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cart_expiry() -> datetime:
    return _now() + timedelta(hours=settings.cart_ttl_hours)


# --------------------------------------------------------------------------
# Cart lifecycle
# --------------------------------------------------------------------------
async def get_active_cart(db: AsyncSession, user_id: int) -> Cart | None:
    stmt = (
        select(Cart)
        .options(
            selectinload(Cart.items).selectinload(CartItem.product),
            selectinload(Cart.items).selectinload(CartItem.variant),
            selectinload(Cart.coupon),
        )
        .where(Cart.user_id == user_id, Cart.status == CartStatus.ACTIVE)
        .order_by(Cart.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def get_or_create_cart(db: AsyncSession, user_id: int) -> Cart:
    cart = await get_active_cart(db, user_id)
    if cart is not None:
        if cart.expires_at and _aware(cart.expires_at) < _now():
            await expire_cart(db, cart)
        else:
            return cart

    cart = Cart(user_id=user_id, status=CartStatus.ACTIVE, expires_at=_cart_expiry())
    db.add(cart)
    await db.flush()
    await db.refresh(cart, ["items"])
    return cart


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def expire_cart(db: AsyncSession, cart: Cart) -> None:
    cart.status = CartStatus.EXPIRED
    await release_reservations(db, cart.id, reason="cart_expired")
    await db.flush()
    log.info("cart_expired", cart_id=cart.id, user_id=cart.user_id)


async def touch_cart(db: AsyncSession, cart: Cart) -> None:
    """Sliding expiry — activity pushes the TTL out."""
    cart.expires_at = _cart_expiry()
    cart.reminder_sent_at = None
    await db.flush()


# --------------------------------------------------------------------------
# Stock availability
# --------------------------------------------------------------------------
async def reserved_quantity(db: AsyncSession, variant_id: int, exclude_cart_id: int | None = None) -> int:
    conditions = [
        InventoryReservation.variant_id == variant_id,
        InventoryReservation.released_at.is_(None),
        InventoryReservation.committed_at.is_(None),
        InventoryReservation.expires_at > _now(),
    ]
    if exclude_cart_id is not None:
        conditions.append(InventoryReservation.cart_id != exclude_cart_id)

    total = await db.scalar(
        select(func.coalesce(func.sum(InventoryReservation.quantity), 0)).where(and_(*conditions))
    )
    return int(total or 0)


async def reserved_quantities_bulk(
    db: AsyncSession, variant_ids: list[int], exclude_cart_id: int | None = None
) -> dict[int, int]:
    if not variant_ids:
        return {}

    conditions = [
        InventoryReservation.variant_id.in_(variant_ids),
        InventoryReservation.released_at.is_(None),
        InventoryReservation.committed_at.is_(None),
        InventoryReservation.expires_at > _now(),
    ]
    if exclude_cart_id is not None:
        conditions.append(InventoryReservation.cart_id != exclude_cart_id)

    stmt = (
        select(InventoryReservation.variant_id, func.sum(InventoryReservation.quantity))
        .where(and_(*conditions))
        .group_by(InventoryReservation.variant_id)
    )
    return {int(vid): int(total) for vid, total in (await db.execute(stmt)).all()}


async def available_stock(
    db: AsyncSession, variant: ProductVariant, exclude_cart_id: int | None = None
) -> int:
    held = await reserved_quantity(db, variant.id, exclude_cart_id)
    return max(variant.stock - held, 0)


# --------------------------------------------------------------------------
# Item mutations
# --------------------------------------------------------------------------
async def add_item(
    db: AsyncSession, user_id: int, product_id: int, variant_id: int, quantity: int
) -> Cart:
    cart = await get_or_create_cart(db, user_id)
    variant = await _load_sellable_variant(db, variant_id, product_id)

    existing = next((i for i in cart.items if i.variant_id == variant_id), None)
    desired = (existing.quantity if existing else 0) + quantity

    available = await available_stock(db, variant, exclude_cart_id=cart.id)
    if desired > available:
        raise OutOfStockError(
            f"Only {available} left of {variant.product.name} ({variant.name})"
            if available
            else f"{variant.product.name} ({variant.name}) is out of stock"
        )

    if existing is not None:
        existing.quantity = desired
    else:
        db.add(
            CartItem(
                cart_id=cart.id,
                product_id=product_id,
                variant_id=variant_id,
                quantity=quantity,
            )
        )

    await touch_cart(db, cart)
    await db.refresh(cart, ["items"])
    log.info("cart_item_added", cart_id=cart.id, variant_id=variant_id, quantity=quantity)
    return cart


async def update_item(db: AsyncSession, user_id: int, item_id: int, quantity: int) -> Cart:
    cart = await get_or_create_cart(db, user_id)
    item = next((i for i in cart.items if i.id == item_id), None)
    if item is None:
        raise NotFoundError("Cart item not found")

    variant = await _load_sellable_variant(db, item.variant_id, item.product_id)
    available = await available_stock(db, variant, exclude_cart_id=cart.id)
    if quantity > available:
        raise OutOfStockError(f"Only {available} left of {variant.product.name}")

    item.quantity = quantity
    await touch_cart(db, cart)
    await db.refresh(cart, ["items"])
    return cart


async def remove_item(db: AsyncSession, user_id: int, item_id: int) -> Cart:
    cart = await get_or_create_cart(db, user_id)
    item = next((i for i in cart.items if i.id == item_id), None)
    if item is None:
        raise NotFoundError("Cart item not found")

    await db.delete(item)
    await db.flush()
    await db.refresh(cart, ["items"])
    return cart


async def clear_cart(db: AsyncSession, cart: Cart) -> None:
    for item in list(cart.items):
        await db.delete(item)
    cart.coupon_id = None
    await db.flush()


async def _load_sellable_variant(
    db: AsyncSession, variant_id: int, product_id: int
) -> ProductVariant:
    stmt = (
        select(ProductVariant)
        .options(selectinload(ProductVariant.product))
        .where(ProductVariant.id == variant_id, ProductVariant.deleted_at.is_(None))
    )
    variant = (await db.execute(stmt)).scalar_one_or_none()

    if variant is None:
        raise NotFoundError("Product variant not found")
    if variant.product_id != product_id:
        raise ValidationError("Variant does not belong to this product")
    if not variant.active or not variant.product.active or variant.product.deleted_at is not None:
        raise ValidationError("This product is no longer available")

    return variant


# --------------------------------------------------------------------------
# Coupons
# --------------------------------------------------------------------------
async def apply_coupon(db: AsyncSession, user_id: int, code: str) -> Cart:
    cart = await get_or_create_cart(db, user_id)
    if not cart.items:
        raise ValidationError("Add items to your cart before applying a coupon")

    subtotal = _subtotal(cart)
    coupon, _ = await coupon_service.validate_coupon(db, code, user_id, subtotal)

    cart.coupon_id = coupon.id
    await db.flush()
    await db.refresh(cart, ["coupon"])
    log.info("cart_coupon_applied", cart_id=cart.id, code=coupon.code)
    return cart


async def remove_coupon(db: AsyncSession, user_id: int) -> Cart:
    cart = await get_or_create_cart(db, user_id)
    cart.coupon_id = None
    await db.flush()
    await db.refresh(cart, ["coupon"])
    return cart


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------
def _unit_price(item: CartItem) -> Decimal:
    if item.variant is not None and item.variant.price_override is not None:
        return item.variant.price_override
    return item.product.base_price


def _subtotal(cart: Cart) -> Decimal:
    return q(sum((_unit_price(i) * i.quantity for i in cart.items), Decimal("0.00")))


def cart_weight(cart: Cart) -> int:
    total = 0
    for item in cart.items:
        grams = item.variant.weight_grams or item.product.weight_grams or 0
        total += grams * item.quantity
    return total


async def price_cart(
    db: AsyncSession,
    cart: Cart,
    pincode: str | None = None,
    buyer_state_code: str | None = None,
) -> dict[str, Any]:
    """Compute line items, discount, GST and shipping for a cart.

    The order-level discount is spread across lines in proportion to their
    value so each line's frozen tax matches the amount actually charged.
    """
    from app.services.shipping import calculate_shipping
    from app.services.tax import build_tax_breakup

    subtotal = _subtotal(cart)

    discount = Decimal("0.00")
    applied_coupon = None
    if cart.coupon_id and cart.coupon is not None:
        try:
            _, discount = await coupon_service.validate_coupon(
                db, cart.coupon.code, cart.user_id, subtotal
            )
            applied_coupon = {
                "code": cart.coupon.code,
                "discount_type": cart.coupon.discount_type,
                "discount_value": cart.coupon.discount_value,
                "discount_amount": discount,
            }
        except Exception as exc:
            # A coupon that has since expired must not block viewing the cart.
            log.info("cart_coupon_no_longer_valid", cart_id=cart.id, reason=str(exc))
            cart.coupon_id = None
            discount = Decimal("0.00")

    variant_ids = [i.variant_id for i in cart.items]
    held = await reserved_quantities_bulk(db, variant_ids, exclude_cart_id=cart.id)

    lines: list[dict[str, Any]] = []
    allocated = Decimal("0.00")
    item_count = len(cart.items)

    for index, item in enumerate(cart.items):
        unit = _unit_price(item)
        gross = q(unit * item.quantity)

        if discount > 0 and subtotal > 0:
            if index == item_count - 1:
                # Last line absorbs the rounding remainder.
                line_discount = q(discount - allocated)
            else:
                line_discount = q(discount * gross / subtotal)
                allocated += line_discount
        else:
            line_discount = Decimal("0.00")

        tax = compute_line_tax(unit, item.quantity, item.product.gst_rate, line_discount)
        available = max(item.variant.stock - held.get(item.variant_id, 0), 0)

        lines.append(
            {
                "id": item.id,
                "product_id": item.product_id,
                "variant_id": item.variant_id,
                "product_name": item.product.name,
                "variant_name": item.variant.name,
                "sku": item.variant.sku,
                "hsn_code": item.product.hsn_code,
                "image_url": _first_image(item.product),
                "unit_price": unit,
                "quantity": item.quantity,
                "line_total": tax["gross"],
                "line_subtotal": gross,
                "line_discount": line_discount,
                "tax_rate": item.product.gst_rate,
                "taxable_value": tax["taxable_value"],
                "tax_amount": tax["tax_amount"],
                "available_stock": available,
                "in_stock": available >= item.quantity,
                "stock_message": (
                    None
                    if available >= item.quantity
                    else (f"Only {available} left" if available else "Out of stock")
                ),
            }
        )

    tax_total = q(sum((line["tax_amount"] for line in lines), Decimal("0.00")))
    taxable_total = q(sum((line["taxable_value"] for line in lines), Decimal("0.00")))
    goods_total = q(subtotal - discount)

    shipping = Decimal("0.00")
    shipping_detail: dict[str, Any] | None = None
    if pincode:
        shipping_detail = await calculate_shipping(db, pincode, cart_weight(cart), goods_total)
        shipping = shipping_detail["shipping_cost"]

    return {
        "lines": lines,
        "coupon": applied_coupon,
        "shipping_detail": shipping_detail,
        "has_stock_issues": any(not line["in_stock"] for line in lines),
        "totals": {
            "subtotal": subtotal,
            "discount_amount": discount,
            "taxable_amount": taxable_total,
            "tax_amount": tax_total,
            "tax_breakup": build_tax_breakup(lines, buyer_state_code) if lines else {},
            "shipping_cost": shipping,
            "total": q(goods_total + shipping),
            "currency": settings.currency,
        },
    }


def _first_image(product: Product) -> str | None:
    urls = product.image_urls or []
    return urls[0] if urls else None


async def serialize_cart(
    db: AsyncSession, cart: Cart, pincode: str | None = None
) -> dict[str, Any]:
    priced = await price_cart(db, cart, pincode=pincode)
    return {
        "id": cart.id,
        "status": cart.status,
        "items": priced["lines"],
        "item_count": sum(i.quantity for i in cart.items),
        "coupon": priced["coupon"],
        "totals": priced["totals"],
        "expires_at": cart.expires_at,
        "has_stock_issues": priced["has_stock_issues"],
    }


# --------------------------------------------------------------------------
# Inventory reservation
# --------------------------------------------------------------------------
async def reserve_inventory(db: AsyncSession, cart: Cart) -> list[InventoryReservation]:
    """Lock each variant row, re-check availability, then hold the stock.

    `SELECT ... FOR UPDATE` serialises concurrent checkouts on the same
    variant, so the availability check and the reservation insert cannot
    interleave. Variants are locked in a stable ID order to avoid deadlocks
    between two carts holding overlapping items.
    """
    if not cart.items:
        raise ValidationError("Your cart is empty")

    await release_reservations(db, cart.id, reason="superseded")

    expires_at = _now() + timedelta(minutes=settings.reservation_ttl_minutes)
    reservations: list[InventoryReservation] = []

    for item in sorted(cart.items, key=lambda i: i.variant_id):
        stmt = (
            select(ProductVariant)
            .where(ProductVariant.id == item.variant_id)
            .with_for_update()
        )
        variant = (await db.execute(stmt)).scalar_one_or_none()
        if variant is None or variant.deleted_at is not None or not variant.active:
            raise ValidationError(f"{item.product.name} is no longer available")

        held = await reserved_quantity(db, variant.id, exclude_cart_id=cart.id)
        available = variant.stock - held
        if item.quantity > available:
            raise OutOfStockError(
                f"Only {max(available, 0)} left of {item.product.name} ({item.variant.name})"
            )

        reservation = InventoryReservation(
            cart_id=cart.id,
            variant_id=variant.id,
            quantity=item.quantity,
            expires_at=expires_at,
        )
        db.add(reservation)
        reservations.append(reservation)

    await db.flush()
    log.info("inventory_reserved", cart_id=cart.id, lines=len(reservations))
    return reservations


async def release_reservations(
    db: AsyncSession,
    cart_id: int | None = None,
    order_id: int | None = None,
    reason: str = "released",
) -> int:
    """Release active holds. Committed reservations are never touched."""
    if cart_id is None and order_id is None:
        return 0

    stmt = select(InventoryReservation).where(
        InventoryReservation.released_at.is_(None),
        InventoryReservation.committed_at.is_(None),
    )
    stmt = stmt.where(
        InventoryReservation.cart_id == cart_id
        if cart_id is not None
        else InventoryReservation.order_id == order_id
    )

    rows = (await db.execute(stmt)).scalars().all()
    now = _now()
    for row in rows:
        row.released_at = now

    if rows:
        await db.flush()
        log.info("reservations_released", count=len(rows), reason=reason, cart_id=cart_id)
    return len(rows)


async def commit_reservations(db: AsyncSession, order_id: int) -> int:
    """Convert holds into a real stock decrement once payment succeeds."""
    stmt = select(InventoryReservation).where(
        InventoryReservation.order_id == order_id,
        InventoryReservation.released_at.is_(None),
        InventoryReservation.committed_at.is_(None),
    )
    rows = (await db.execute(stmt)).scalars().all()

    now = _now()
    for row in rows:
        variant = (
            await db.execute(
                select(ProductVariant).where(ProductVariant.id == row.variant_id).with_for_update()
            )
        ).scalar_one_or_none()
        if variant is None:
            continue
        variant.stock = max(variant.stock - row.quantity, 0)
        row.committed_at = now

    if rows:
        await db.flush()
        log.info("reservations_committed", order_id=order_id, count=len(rows))
    return len(rows)


async def restore_stock(db: AsyncSession, order_id: int) -> int:
    """Put committed stock back after a cancellation or return."""
    from app.models.order import OrderItem

    items = (
        (await db.execute(select(OrderItem).where(OrderItem.order_id == order_id)))
        .scalars()
        .all()
    )
    restored = 0
    for item in items:
        if item.variant_id is None:
            continue
        variant = (
            await db.execute(
                select(ProductVariant).where(ProductVariant.id == item.variant_id).with_for_update()
            )
        ).scalar_one_or_none()
        if variant is not None:
            variant.stock += item.quantity
            restored += 1

    await db.flush()
    log.info("stock_restored", order_id=order_id, lines=restored)
    return restored


async def cleanup_expired_reservations(db: AsyncSession) -> int:
    stmt = select(InventoryReservation).where(
        InventoryReservation.released_at.is_(None),
        InventoryReservation.committed_at.is_(None),
        InventoryReservation.expires_at <= _now(),
    )
    rows = (await db.execute(stmt)).scalars().all()

    now = _now()
    for row in rows:
        row.released_at = now

    if rows:
        await db.flush()
        log.info("expired_reservations_cleaned", count=len(rows))
    return len(rows)


async def expire_stale_carts(db: AsyncSession) -> int:
    stmt = select(Cart).where(
        Cart.status == CartStatus.ACTIVE,
        Cart.expires_at.is_not(None),
        Cart.expires_at <= _now(),
    )
    carts = (await db.execute(stmt)).scalars().all()
    for cart in carts:
        await expire_cart(db, cart)
    return len(carts)


async def assert_cart_ready(db: AsyncSession, cart: Cart) -> None:
    if not cart.items:
        raise ValidationError("Your cart is empty")
    if cart.status != CartStatus.ACTIVE:
        raise ConflictError("This cart has already been checked out")
