from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class CartItemAdd(BaseModel):
    product_id: int
    variant_id: int
    quantity: int = Field(default=1, ge=1, le=99)


class CartItemUpdate(BaseModel):
    quantity: int = Field(ge=1, le=99)


class CartItemOut(ORMModel):
    id: int
    product_id: int
    variant_id: int
    product_name: str
    variant_name: str | None = None
    sku: str | None = None
    image_url: str | None = None
    unit_price: Decimal
    quantity: int
    line_total: Decimal
    available_stock: int = 0
    in_stock: bool = True
    stock_message: str | None = None


class AppliedCoupon(BaseModel):
    code: str
    discount_type: str
    discount_value: Decimal
    discount_amount: Decimal


class CartTotals(BaseModel):
    subtotal: Decimal
    discount_amount: Decimal = Decimal("0.00")
    taxable_amount: Decimal = Decimal("0.00")
    tax_amount: Decimal = Decimal("0.00")
    tax_breakup: dict[str, Any] = Field(default_factory=dict)
    shipping_cost: Decimal = Decimal("0.00")
    total: Decimal
    currency: str = "INR"


class CartOut(ORMModel):
    id: int
    status: str
    items: list[CartItemOut] = Field(default_factory=list)
    item_count: int = 0
    coupon: AppliedCoupon | None = None
    totals: CartTotals
    expires_at: datetime | None = None
    has_stock_issues: bool = False


class ApplyCouponRequest(BaseModel):
    code: str = Field(min_length=1, max_length=50)


class CheckoutRequest(BaseModel):
    address_id: int | None = None
    # WhatsApp checkout can pass a fresh address inline instead of an ID.
    new_address: dict[str, Any] | None = None
    notes: str | None = Field(default=None, max_length=500)


class CheckoutResponse(BaseModel):
    order_id: int
    order_number: str
    total: Decimal
    currency: str = "INR"
    payment_link: str | None = None
    payment_session_id: str | None = None
    payment_expires_at: datetime | None = None
    status: str


class ShippingEstimateRequest(BaseModel):
    pincode: str = Field(min_length=6, max_length=10)


class ShippingEstimate(BaseModel):
    pincode: str
    serviceable: bool
    zone: str | None = None
    shipping_cost: Decimal = Decimal("0.00")
    free_shipping_applied: bool = False
    eta_days_min: int | None = None
    eta_days_max: int | None = None
    message: str | None = None
