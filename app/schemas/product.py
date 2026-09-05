from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMModel


class CategoryOut(ORMModel):
    id: int
    name: str
    slug: str | None = None
    description: str | None = None
    image_url: str | None = None
    sort_order: int
    active: bool
    product_count: int = 0


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    image_url: str | None = None
    sort_order: int = 0
    active: bool = True


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    image_url: str | None = None
    sort_order: int | None = None
    active: bool | None = None


class VariantOut(ORMModel):
    id: int
    sku: str
    name: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    price: Decimal
    price_override: Decimal | None = None
    stock: int
    in_stock: bool = True
    active: bool
    meta_retailer_id: str | None = None


class VariantCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    attributes: dict[str, Any] = Field(default_factory=dict)
    price_override: Decimal | None = Field(default=None, ge=0)
    stock: int = Field(default=0, ge=0)
    weight_grams: int | None = Field(default=None, ge=0)
    active: bool = True


class VariantUpdate(BaseModel):
    sku: str | None = Field(default=None, max_length=100)
    name: str | None = Field(default=None, max_length=255)
    attributes: dict[str, Any] | None = None
    price_override: Decimal | None = Field(default=None, ge=0)
    stock: int | None = Field(default=None, ge=0)
    weight_grams: int | None = Field(default=None, ge=0)
    active: bool | None = None


class ProductImageOut(ORMModel):
    id: int
    url: str
    alt_text: str | None = None
    sort_order: int
    variant_id: int | None = None


class ProductSummary(ORMModel):
    id: int
    name: str
    description: str | None = None
    base_price: Decimal
    image_urls: list[Any] = Field(default_factory=list)
    category_id: int | None = None
    category_name: str | None = None
    avg_rating: float | None = None
    review_count: int = 0
    in_stock: bool = True
    min_price: Decimal | None = None


class ProductDetail(ProductSummary):
    hsn_code: str | None = None
    gst_rate: Decimal
    weight_grams: int
    active: bool
    meta_retailer_id: str | None = None
    sync_status: str | None = None
    variants: list[VariantOut] = Field(default_factory=list)
    images: list[ProductImageOut] = Field(default_factory=list)
    created_at: datetime | None = None


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    base_price: Decimal = Field(ge=0)
    category_id: int | None = None
    image_urls: list[str] = Field(default_factory=list)
    hsn_code: str | None = Field(default=None, max_length=20)
    gst_rate: Decimal = Field(default=Decimal("18.00"), ge=0, le=100)
    weight_grams: int = Field(default=500, ge=0)
    active: bool = True
    variants: list[VariantCreate] = Field(default_factory=list)

    @field_validator("variants")
    @classmethod
    def _unique_skus(cls, v: list[VariantCreate]) -> list[VariantCreate]:
        skus = [variant.sku for variant in v]
        if len(skus) != len(set(skus)):
            raise ValueError("Duplicate SKUs in variant list")
        return v


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    base_price: Decimal | None = Field(default=None, ge=0)
    category_id: int | None = None
    image_urls: list[str] | None = None
    hsn_code: str | None = Field(default=None, max_length=20)
    gst_rate: Decimal | None = Field(default=None, ge=0, le=100)
    weight_grams: int | None = Field(default=None, ge=0)
    active: bool | None = None


class ProductImportItem(ProductCreate):
    category_name: str | None = None


class ProductImportRequest(BaseModel):
    products: list[ProductImportItem] = Field(min_length=1, max_length=500)
    update_existing: bool = False


class ProductImportResult(BaseModel):
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)


class ProductFilters(BaseModel):
    category_id: int | None = None
    q: str | None = Field(default=None, max_length=200)
    min_price: Decimal | None = Field(default=None, ge=0)
    max_price: Decimal | None = Field(default=None, ge=0)
    min_rating: float | None = Field(default=None, ge=0, le=5)
    in_stock_only: bool = False
