from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class ReviewCreate(BaseModel):
    product_id: int
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


class ReviewOut(ORMModel):
    """Public shape of a review.

    `user_id` and `order_id` are deliberately absent: product reviews are
    readable by anyone, and shipping either field would let a scraper map
    customers to orders. Admin views add them back via `ReviewAdminOut`.
    """

    id: int
    product_id: int
    rating: int
    comment: str | None = None
    verified_purchase: bool
    author: str | None = None
    created_at: datetime


class ReviewAdminOut(ReviewOut):
    user_id: int | None = None
    order_id: int | None = None
    flagged: bool = False
    product_name: str | None = None
    deleted_at: datetime | None = None


class RatingSummary(BaseModel):
    product_id: int
    avg_rating: float | None = None
    review_count: int = 0
    distribution: dict[int, int] = Field(default_factory=dict)


class WishlistItemOut(ORMModel):
    id: int
    product_id: int
    product_name: str
    base_price: float
    image_url: str | None = None
    in_stock: bool = True
    added_at: datetime
