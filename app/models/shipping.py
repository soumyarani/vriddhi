from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Boolean, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel, Money


class ShippingConfig(BaseModel):
    """Shipping rate for a zone within a weight bracket."""

    __tablename__ = "shipping_config"
    __table_args__ = (Index("ix_shipping_zone_weight", "zone", "min_weight", "max_weight"),)

    zone: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    label: Mapped[str | None] = mapped_column(String(100))
    min_weight: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_weight: Mapped[int] = mapped_column(Integer, default=1000000, nullable=False)
    base_cost: Mapped[Decimal] = mapped_column(Money, nullable=False)
    per_kg_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"), nullable=False)
    free_above_amount: Mapped[Decimal | None] = mapped_column(Money)
    eta_days_min: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    eta_days_max: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ServiceablePincode(BaseModel):
    """Pincodes we deliver to, each mapped to a shipping zone."""

    __tablename__ = "serviceable_pincodes"

    pincode: Mapped[str] = mapped_column(String(10), unique=True, index=True, nullable=False)
    zone: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    cod_available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
