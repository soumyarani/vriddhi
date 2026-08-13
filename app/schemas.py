from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class GoogleAuthRequest(BaseModel):
    code: str = Field(min_length=1)
    redirect_uri: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    phone: str | None
    email: EmailStr | None
    name: str | None
    picture_url: str | None
    auth_provider: str
    gstin: str | None = None
    whatsapp_opt_in: bool = True
    created_at: datetime


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    image_url: str | None
    sort_order: int


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    category_id: int
    base_price: Decimal


class ProductDetailOut(ProductOut):
    variants: list[dict[str, Any]]
    reviews: list[dict[str, Any]]


class PaginationResponse(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None


class CartItemCreate(BaseModel):
    product_id: int
    variant_id: int | None = None
    quantity: int = Field(ge=1, le=999)


class CartItemUpdate(BaseModel):
    quantity: int = Field(ge=1, le=999)


class CouponApplyRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class CheckoutRequest(BaseModel):
    address_id: int
    coupon_code: str | None = None


class CheckoutRetryRequest(BaseModel):
    order_id: int


class ShippingEstimateResponse(BaseModel):
    pincode: str
    serviceable: bool
    shipping_cost: Decimal


class ProfileUpdateRequest(BaseModel):
    name: str | None = None
    gstin: str | None = None


class AddressIn(BaseModel):
    label: str | None = None
    line1: str
    line2: str | None = None
    city: str
    state: str
    pincode: str
    country: str = "India"
    is_default: bool = False


class WhatsAppOptInRequest(BaseModel):
    whatsapp_opt_in: bool


class OrderStatusRequest(BaseModel):
    status: str
    tracking_number: str | None = None


class OrderConfirmRequest(BaseModel):
    delivery_eta: datetime | None = None
    tracking_number: str | None = None
    tracking_url: str | None = None


class RefundRequest(BaseModel):
    amount: Decimal
    reason: str | None = None


class ConversationReplyRequest(BaseModel):
    message: str = Field(min_length=1)


class ConversationAssignRequest(BaseModel):
    agent_id: int


class ReviewCreateRequest(BaseModel):
    product_id: int
    rating: int = Field(ge=1, le=5)
    comment: str | None = None


class ProductCreateRequest(BaseModel):
    name: str
    description: str | None = None
    base_price: Decimal
    category_id: int


class ProductUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    base_price: Decimal | None = None
    category_id: int | None = None
    active: bool | None = None


class VariantRequest(BaseModel):
    sku: str
    name: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    price_override: Decimal | None = None
    stock: int = 0


class CouponCreateRequest(BaseModel):
    code: str
    discount_type: str
    discount_value: Decimal
    min_order: Decimal | None = None
    max_uses: int | None = None
    per_user_limit: int | None = None


class CouponUpdateRequest(BaseModel):
    active: bool | None = None
    discount_value: Decimal | None = None


class ShippingConfigRequest(BaseModel):
    zone: str
    min_weight: Decimal
    max_weight: Decimal
    base_cost: Decimal
    free_above_amount: Decimal | None = None


class WebhookAck(BaseModel):
    status: str = "accepted"
