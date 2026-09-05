from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, JSONType, Money
from app.models.enums import OrderStatus, PaymentStatus, RefundStatus

if TYPE_CHECKING:
    from app.models.product import Product, ProductVariant
    from app.models.user import User


class Order(BaseModel):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_user_status", "user_id", "status"),
        Index("ix_orders_status_created", "status", "created_at"),
        CheckConstraint("total >= 0", name="ck_orders_total_non_negative"),
    )

    order_number: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    cart_id: Mapped[int | None] = mapped_column(ForeignKey("carts.id", ondelete="SET NULL"))
    address_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)

    subtotal: Mapped[Decimal] = mapped_column(Money, nullable=False)
    shipping_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"), nullable=False)
    tax_breakup: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"), nullable=False)
    total: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    status: Mapped[str] = mapped_column(
        String(30), default=OrderStatus.PENDING_PAYMENT, index=True, nullable=False
    )
    channel: Mapped[str] = mapped_column(String(20), default="web", nullable=False)

    coupon_id: Mapped[int | None] = mapped_column(ForeignKey("coupons.id", ondelete="SET NULL"))
    coupon_code: Mapped[str | None] = mapped_column(String(50))

    delivery_eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tracking_number: Mapped[str | None] = mapped_column(String(100))
    tracking_url: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    return_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    return_reason: Mapped[str | None] = mapped_column(Text)
    agent_notes: Mapped[str | None] = mapped_column(Text)
    invoice_path: Mapped[str | None] = mapped_column(Text)

    user: Mapped["User"] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    refunds: Mapped[list["Refund"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(BaseModel):
    """Frozen snapshot of what was bought — never re-reads the live product."""

    __tablename__ = "order_items"
    __table_args__ = (CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),)

    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    variant_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_variants.id", ondelete="SET NULL")
    )

    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    variant_name: Mapped[str | None] = mapped_column(String(255))
    sku: Mapped[str | None] = mapped_column(String(100))
    hsn_code: Mapped[str | None] = mapped_column(String(20))
    image_url: Mapped[str | None] = mapped_column(Text)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    line_subtotal: Mapped[Decimal] = mapped_column(Money, nullable=False)
    line_discount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"), nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0.00"), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Money, nullable=False)

    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped["Product | None"] = relationship()
    variant: Mapped["ProductVariant | None"] = relationship()


class Payment(BaseModel):
    __tablename__ = "payments"

    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    cashfree_order_id: Mapped[str | None] = mapped_column(String(100), index=True)
    cashfree_payment_id: Mapped[str | None] = mapped_column(String(100), index=True)
    idempotency_key: Mapped[str] = mapped_column(
        String(100), unique=True, index=True, nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    paid_amount: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=PaymentStatus.CREATED, index=True, nullable=False
    )
    payment_link: Mapped[str | None] = mapped_column(Text)
    payment_session_id: Mapped[str | None] = mapped_column(Text)
    payment_method: Mapped[str | None] = mapped_column(String(50))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped["Order"] = relationship(back_populates="payments")
    refunds: Mapped[list["Refund"]] = relationship(back_populates="payment")


class Refund(BaseModel):
    __tablename__ = "refunds"

    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"))
    cashfree_refund_id: Mapped[str | None] = mapped_column(String(100), index=True)
    idempotency_key: Mapped[str] = mapped_column(
        String(100), unique=True, index=True, nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=RefundStatus.PENDING, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    initiated_by_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL")
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    order: Mapped["Order"] = relationship(back_populates="refunds")
    payment: Mapped["Payment | None"] = relationship(back_populates="refunds")
