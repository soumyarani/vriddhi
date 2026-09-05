from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, Money, SoftDeleteMixin
from app.models.enums import DiscountType

if TYPE_CHECKING:
    from app.models.order import Order
    from app.models.user import User


class Coupon(BaseModel, SoftDeleteMixin):
    __tablename__ = "coupons"

    code: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    discount_type: Mapped[str] = mapped_column(String(10), default=DiscountType.PERCENT, nullable=False)
    discount_value: Mapped[Decimal] = mapped_column(Money, nullable=False)
    max_discount_amount: Mapped[Decimal | None] = mapped_column(Money)
    min_order: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"), nullable=False)
    max_uses: Mapped[int | None] = mapped_column(Integer)
    per_user_limit: Mapped[int | None] = mapped_column(Integer, default=1)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    usages: Mapped[list["CouponUsage"]] = relationship(back_populates="coupon")


class CouponUsage(BaseModel):
    """One row per redemption.

    Usage counts are derived with COUNT(*) over this table rather than a mutable
    counter column, so concurrent checkouts cannot over-redeem a coupon.
    """

    __tablename__ = "coupon_usage"
    __table_args__ = (
        Index("ix_coupon_usage_coupon_user", "coupon_id", "user_id"),
        Index("ix_coupon_usage_order", "order_id", unique=True),
    )

    coupon_id: Mapped[int] = mapped_column(
        ForeignKey("coupons.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    coupon: Mapped["Coupon"] = relationship(back_populates="usages")
    user: Mapped["User"] = relationship()
    order: Mapped["Order"] = relationship()
