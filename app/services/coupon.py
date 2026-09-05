"""Coupon validation and redemption.

Usage limits are enforced by COUNT(*) over `coupon_usage` rather than a
mutable `used_count` column. Two concurrent checkouts reading a counter would
both see the same value and both pass; counting rows inside the checkout
transaction, with the coupon row locked, cannot over-redeem.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ConflictError, NotFoundError, ValidationError
from app.models.coupon import Coupon, CouponUsage
from app.models.enums import DiscountType
from app.services.tax import q
from logging_config import get_logger

log = get_logger(__name__)


async def get_coupon_by_code(
    db: AsyncSession, code: str, for_update: bool = False
) -> Coupon | None:
    stmt = select(Coupon).where(
        func.upper(Coupon.code) == code.strip().upper(),
        Coupon.deleted_at.is_(None),
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def times_used(db: AsyncSession, coupon_id: int) -> int:
    return int(
        await db.scalar(
            select(func.count(CouponUsage.id)).where(CouponUsage.coupon_id == coupon_id)
        )
        or 0
    )


async def times_used_by(db: AsyncSession, coupon_id: int, user_id: int) -> int:
    return int(
        await db.scalar(
            select(func.count(CouponUsage.id)).where(
                CouponUsage.coupon_id == coupon_id, CouponUsage.user_id == user_id
            )
        )
        or 0
    )


def compute_discount(coupon: Coupon, subtotal: Decimal) -> Decimal:
    if coupon.discount_type == DiscountType.PERCENT:
        discount = subtotal * coupon.discount_value / Decimal("100")
        if coupon.max_discount_amount is not None:
            discount = min(discount, coupon.max_discount_amount)
    else:
        discount = coupon.discount_value

    # Never discount below zero, and never below the order value.
    return q(max(Decimal("0.00"), min(discount, subtotal)))


async def validate_coupon(
    db: AsyncSession,
    code: str,
    user_id: int,
    subtotal: Decimal,
    lock: bool = False,
) -> tuple[Coupon, Decimal]:
    """Validate a coupon for a user and return `(coupon, discount)`.

    Pass `lock=True` from checkout so the row is held for the rest of the
    transaction and the usage count cannot change underneath us.
    """
    coupon = await get_coupon_by_code(db, code, for_update=lock)
    if coupon is None:
        raise NotFoundError("Coupon not found")

    if not coupon.active:
        raise ValidationError("This coupon is no longer active")

    now = datetime.now(timezone.utc)
    if coupon.starts_at and _aware(coupon.starts_at) > now:
        raise ValidationError("This coupon is not active yet")
    if coupon.expires_at and _aware(coupon.expires_at) < now:
        raise ValidationError("This coupon has expired")

    if subtotal < coupon.min_order:
        raise ValidationError(
            f"Add items worth {coupon.min_order - subtotal:.2f} more to use this coupon"
        )

    if coupon.max_uses is not None and await times_used(db, coupon.id) >= coupon.max_uses:
        raise ConflictError("This coupon has reached its usage limit")

    if (
        coupon.per_user_limit is not None
        and await times_used_by(db, coupon.id, user_id) >= coupon.per_user_limit
    ):
        raise ConflictError("You have already used this coupon")

    discount = compute_discount(coupon, subtotal)
    if discount <= 0:
        raise ValidationError("This coupon does not apply to your cart")

    return coupon, discount


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def record_usage(
    db: AsyncSession, coupon_id: int, user_id: int, order_id: int, discount: Decimal
) -> CouponUsage:
    usage = CouponUsage(
        coupon_id=coupon_id,
        user_id=user_id,
        order_id=order_id,
        discount_amount=discount,
        used_at=datetime.now(timezone.utc),
    )
    db.add(usage)
    await db.flush()
    log.info("coupon_redeemed", coupon_id=coupon_id, order_id=order_id)
    return usage


async def release_usage(db: AsyncSession, order_id: int) -> None:
    """Free the redemption when an order is cancelled before fulfilment."""
    usages = (
        (await db.execute(select(CouponUsage).where(CouponUsage.order_id == order_id)))
        .scalars()
        .all()
    )
    for usage in usages:
        await db.delete(usage)
    if usages:
        log.info("coupon_usage_released", order_id=order_id, count=len(usages))


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------
async def create_coupon(db: AsyncSession, data: Any) -> Coupon:
    if await get_coupon_by_code(db, data.code) is not None:
        raise ConflictError("A coupon with this code already exists")

    coupon = Coupon(**data.model_dump())
    db.add(coupon)
    await db.flush()
    log.info("coupon_created", code=coupon.code, coupon_id=coupon.id)
    return coupon


async def update_coupon(db: AsyncSession, coupon_id: int, data: Any) -> Coupon:
    coupon = (
        await db.execute(
            select(Coupon).where(Coupon.id == coupon_id, Coupon.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if coupon is None:
        raise NotFoundError("Coupon not found")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(coupon, field, value)

    await db.flush()
    return coupon


async def serialize_coupon(db: AsyncSession, coupon: Coupon) -> dict[str, Any]:
    return {
        "id": coupon.id,
        "code": coupon.code,
        "description": coupon.description,
        "discount_type": coupon.discount_type,
        "discount_value": coupon.discount_value,
        "max_discount_amount": coupon.max_discount_amount,
        "min_order": coupon.min_order,
        "max_uses": coupon.max_uses,
        "per_user_limit": coupon.per_user_limit,
        "starts_at": coupon.starts_at,
        "expires_at": coupon.expires_at,
        "active": coupon.active,
        "times_used": await times_used(db, coupon.id),
        "created_at": coupon.created_at,
    }
