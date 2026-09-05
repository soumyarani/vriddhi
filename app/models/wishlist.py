from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.user import User


class Wishlist(BaseModel):
    __tablename__ = "wishlists"
    __table_args__ = (Index("ix_wishlists_user_product", "user_id", "product_id", unique=True),)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True, nullable=False
    )

    user: Mapped["User"] = relationship()
    product: Mapped["Product"] = relationship(lazy="selectin")
