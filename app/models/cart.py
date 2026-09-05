from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel
from app.models.enums import CartStatus

if TYPE_CHECKING:
    from app.models.coupon import Coupon
    from app.models.product import Product, ProductVariant
    from app.models.user import User


class Cart(BaseModel):
    __tablename__ = "carts"
    __table_args__ = (Index("ix_carts_user_status", "user_id", "status"),)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default=CartStatus.ACTIVE, nullable=False)
    coupon_id: Mapped[int | None] = mapped_column(ForeignKey("coupons.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="carts")
    coupon: Mapped["Coupon | None"] = relationship()
    items: Mapped[list["CartItem"]] = relationship(
        back_populates="cart", cascade="all, delete-orphan", lazy="selectin"
    )
    reservations: Mapped[list["InventoryReservation"]] = relationship(
        back_populates="cart", cascade="all, delete-orphan"
    )


class CartItem(BaseModel):
    __tablename__ = "cart_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_cart_items_quantity_positive"),
        Index("ix_cart_items_cart_variant", "cart_id", "variant_id", unique=True),
    )

    cart_id: Mapped[int] = mapped_column(
        ForeignKey("carts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[int] = mapped_column(
        ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    cart: Mapped["Cart"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(lazy="selectin")
    variant: Mapped["ProductVariant"] = relationship(lazy="selectin")


class InventoryReservation(BaseModel):
    __tablename__ = "inventory_reservations"
    __table_args__ = (
        Index("ix_reservations_variant_released", "variant_id", "released_at"),
        CheckConstraint("quantity > 0", name="ck_reservations_quantity_positive"),
    )

    cart_id: Mapped[int] = mapped_column(
        ForeignKey("carts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), index=True)
    variant_id: Mapped[int] = mapped_column(
        ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the sale completes, so cleanup never restores already-sold stock.
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    cart: Mapped["Cart"] = relationship(back_populates="reservations")
    variant: Mapped["ProductVariant"] = relationship()

    @property
    def is_active(self) -> bool:
        return self.released_at is None and self.committed_at is None
