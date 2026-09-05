from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
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

from app.models.base import BaseModel, JSONType, Money, SoftDeleteMixin
from app.models.enums import SyncStatus

if TYPE_CHECKING:
    pass


class Category(BaseModel, SoftDeleteMixin):
    __tablename__ = "categories"

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    slug: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    products: Mapped[list["Product"]] = relationship(back_populates="category")


class Product(BaseModel, SoftDeleteMixin):
    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_category_active", "category_id", "active"),
        CheckConstraint("base_price >= 0", name="ck_products_base_price_non_negative"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    base_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    image_urls: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), index=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Meta Commerce Manager
    meta_retailer_id: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    sync_status: Mapped[str] = mapped_column(String(20), default=SyncStatus.PENDING, nullable=False)
    sync_error: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # GST
    hsn_code: Mapped[str | None] = mapped_column(String(20))
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("18.00"), nullable=False)

    # Shipping
    weight_grams: Mapped[int] = mapped_column(Integer, default=500, nullable=False)

    category: Mapped["Category | None"] = relationship(back_populates="products")
    variants: Mapped[list["ProductVariant"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", lazy="selectin"
    )
    images: Mapped[list["ProductImage"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    @property
    def total_stock(self) -> int:
        return sum(v.stock for v in self.variants) if self.variants else 0


class ProductVariant(BaseModel, SoftDeleteMixin):
    __tablename__ = "product_variants"
    __table_args__ = (
        CheckConstraint("stock >= 0", name="ck_variants_stock_non_negative"),
        Index("ix_variants_product_active", "product_id", "active"),
    )

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sku: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    price_override: Mapped[Decimal | None] = mapped_column(Money)
    stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    weight_grams: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    meta_retailer_id: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)

    product: Mapped["Product"] = relationship(back_populates="variants")
    images: Mapped[list["ProductImage"]] = relationship(back_populates="variant")

    @property
    def effective_price(self) -> Decimal:
        return self.price_override if self.price_override is not None else self.product.base_price


class ProductImage(BaseModel):
    __tablename__ = "product_images"

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True, nullable=False
    )
    variant_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_variants.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    alt_text: Mapped[str | None] = mapped_column(String(255))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    product: Mapped["Product"] = relationship(back_populates="images")
    variant: Mapped["ProductVariant | None"] = relationship(back_populates="images")
