from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class PopularProduct(BaseModel):
    product_id: int
    name: str
    units_sold: int
    revenue: Decimal


class StatusBreakdown(BaseModel):
    status: str
    count: int


class DashboardStats(BaseModel):
    total_orders: int = 0
    orders_today: int = 0
    pending_confirmation: int = 0
    total_revenue: Decimal = Decimal("0.00")
    revenue_today: Decimal = Decimal("0.00")
    average_order_value: Decimal = Decimal("0.00")
    total_users: int = 0
    new_users_today: int = 0
    active_carts: int = 0
    conversion_rate: float = 0.0
    ai_resolution_rate: float = 0.0
    conversations_open: int = 0
    conversations_queued: int = 0
    ai_tokens_today: int = 0
    popular_products: list[PopularProduct] = Field(default_factory=list)
    status_breakdown: list[StatusBreakdown] = Field(default_factory=list)


class ChartPoint(BaseModel):
    period: date
    orders: int = 0
    revenue: Decimal = Decimal("0.00")


class OrdersChart(BaseModel):
    granularity: str
    points: list[ChartPoint] = Field(default_factory=list)
