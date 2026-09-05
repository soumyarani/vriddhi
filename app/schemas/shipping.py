from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import ORMModel


class ShippingConfigBase(BaseModel):
    zone: str = Field(min_length=1, max_length=50)
    label: str | None = Field(default=None, max_length=100)
    min_weight: int = Field(default=0, ge=0)
    max_weight: int = Field(default=1_000_000, ge=1)
    base_cost: Decimal = Field(ge=0)
    per_kg_cost: Decimal = Field(default=Decimal("0.00"), ge=0)
    free_above_amount: Decimal | None = Field(default=None, ge=0)
    eta_days_min: int = Field(default=3, ge=0)
    eta_days_max: int = Field(default=7, ge=0)
    active: bool = True

    @model_validator(mode="after")
    def _check_ranges(self) -> "ShippingConfigBase":
        if self.min_weight >= self.max_weight:
            raise ValueError("min_weight must be less than max_weight")
        if self.eta_days_min > self.eta_days_max:
            raise ValueError("eta_days_min must not exceed eta_days_max")
        return self


class ShippingConfigCreate(ShippingConfigBase):
    pass


class ShippingConfigUpdate(BaseModel):
    zone: str | None = Field(default=None, max_length=50)
    label: str | None = Field(default=None, max_length=100)
    min_weight: int | None = Field(default=None, ge=0)
    max_weight: int | None = Field(default=None, ge=1)
    base_cost: Decimal | None = Field(default=None, ge=0)
    per_kg_cost: Decimal | None = Field(default=None, ge=0)
    free_above_amount: Decimal | None = Field(default=None, ge=0)
    eta_days_min: int | None = Field(default=None, ge=0)
    eta_days_max: int | None = Field(default=None, ge=0)
    active: bool | None = None


class ShippingConfigOut(ORMModel):
    id: int
    zone: str
    label: str | None = None
    min_weight: int
    max_weight: int
    base_cost: Decimal
    per_kg_cost: Decimal
    free_above_amount: Decimal | None = None
    eta_days_min: int
    eta_days_max: int
    active: bool


class PincodeEntry(BaseModel):
    pincode: str = Field(min_length=6, max_length=10)
    zone: str = Field(min_length=1, max_length=50)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    cod_available: bool = False


class PincodeUploadRequest(BaseModel):
    pincodes: list[PincodeEntry] = Field(min_length=1, max_length=10000)
    replace_existing: bool = False


class PincodeUploadResult(BaseModel):
    created: int = 0
    updated: int = 0
    deactivated: int = 0
