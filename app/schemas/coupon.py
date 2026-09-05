from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import DiscountType
from app.schemas.common import ORMModel


class CouponBase(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    discount_type: DiscountType = DiscountType.PERCENT
    discount_value: Decimal = Field(gt=0)
    max_discount_amount: Decimal | None = Field(default=None, gt=0)
    min_order: Decimal = Field(default=Decimal("0.00"), ge=0)
    max_uses: int | None = Field(default=None, gt=0)
    per_user_limit: int | None = Field(default=1, gt=0)
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    active: bool = True

    @model_validator(mode="after")
    def _check_percent(self) -> "CouponBase":
        if self.discount_type == DiscountType.PERCENT and self.discount_value > 100:
            raise ValueError("Percent discount cannot exceed 100")
        if self.starts_at and self.expires_at and self.starts_at >= self.expires_at:
            raise ValueError("starts_at must be before expires_at")
        return self


class CouponCreate(CouponBase):
    code: str = Field(min_length=3, max_length=50)

    @field_validator("code")
    @classmethod
    def _normalize(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.isalnum():
            raise ValueError("Coupon code must be alphanumeric")
        return v


class CouponUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    discount_value: Decimal | None = Field(default=None, gt=0)
    max_discount_amount: Decimal | None = Field(default=None, gt=0)
    min_order: Decimal | None = Field(default=None, ge=0)
    max_uses: int | None = Field(default=None, gt=0)
    per_user_limit: int | None = Field(default=None, gt=0)
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    active: bool | None = None


class CouponOut(ORMModel):
    id: int
    code: str
    description: str | None = None
    discount_type: str
    discount_value: Decimal
    max_discount_amount: Decimal | None = None
    min_order: Decimal
    max_uses: int | None = None
    per_user_limit: int | None = None
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    active: bool
    times_used: int = 0
    created_at: datetime


class CouponUsageOut(ORMModel):
    id: int
    user_id: int
    order_id: int
    discount_amount: Decimal
    used_at: datetime | None = None
    created_at: datetime
