from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class OrderItemOut(ORMModel):
    id: int
    product_id: int | None = None
    variant_id: int | None = None
    product_name: str
    variant_name: str | None = None
    sku: str | None = None
    hsn_code: str | None = None
    image_url: str | None = None
    quantity: int
    unit_price: Decimal
    line_subtotal: Decimal
    line_discount: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    line_total: Decimal
    reviewed: bool = False


class PaymentOut(ORMModel):
    id: int
    status: str
    amount: Decimal
    paid_amount: Decimal | None = None
    payment_method: str | None = None
    payment_link: str | None = None
    cashfree_order_id: str | None = None
    expires_at: datetime | None = None
    paid_at: datetime | None = None
    created_at: datetime


class RefundOut(ORMModel):
    id: int
    amount: Decimal
    status: str
    reason: str | None = None
    cashfree_refund_id: str | None = None
    processed_at: datetime | None = None
    created_at: datetime


class OrderSummary(ORMModel):
    id: int
    order_number: str
    status: str
    total: Decimal
    currency: str
    item_count: int = 0
    channel: str
    delivery_eta: datetime | None = None
    tracking_number: str | None = None
    created_at: datetime


class OrderDetail(OrderSummary):
    address_snapshot: dict[str, Any] = Field(default_factory=dict)
    subtotal: Decimal
    shipping_cost: Decimal
    tax_amount: Decimal
    tax_breakup: dict[str, Any] = Field(default_factory=dict)
    discount_amount: Decimal
    coupon_code: str | None = None
    tracking_url: str | None = None
    cancel_reason: str | None = None
    return_reason: str | None = None
    confirmed_at: datetime | None = None
    shipped_at: datetime | None = None
    delivered_at: datetime | None = None
    cancelled_at: datetime | None = None
    can_cancel: bool = False
    can_return: bool = False
    can_review: bool = False
    items: list[OrderItemOut] = Field(default_factory=list)
    payments: list[PaymentOut] = Field(default_factory=list)
    refunds: list[RefundOut] = Field(default_factory=list)


class CancelOrderRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class ReturnOrderRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    item_ids: list[int] | None = None


class RetryPaymentRequest(BaseModel):
    order_id: int


class ConfirmOrderRequest(BaseModel):
    delivery_eta: datetime | None = None
    tracking_number: str | None = Field(default=None, max_length=100)
    tracking_url: str | None = Field(default=None, max_length=1000)
    note: str | None = Field(default=None, max_length=500)
    notify_customer: bool = True


class UpdateOrderStatusRequest(BaseModel):
    status: str
    tracking_number: str | None = Field(default=None, max_length=100)
    tracking_url: str | None = Field(default=None, max_length=1000)
    note: str | None = Field(default=None, max_length=500)
    notify_customer: bool = True


class RefundRequest(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0)
    reason: str = Field(min_length=3, max_length=500)
