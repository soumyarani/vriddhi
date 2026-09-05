"""Product reviews.

Every review must be a verified purchase: the reviewer has to own a *delivered*
order containing the product, and each order buys exactly one review per
product. That pairing is also enforced by a unique index on
`(order_id, product_id)`, so a race between two concurrent submissions ends in
an IntegrityError rather than a duplicate.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.cache import CacheKeys, cache_delete
from app.errors import ConflictError, NotFoundError, PermissionError_, ValidationError
from app.models.enums import OrderStatus
from app.models.order import Order, OrderItem
from app.models.review import Review
from app.pagination import apply_cursor, build_page
from logging_config import get_logger

log = get_logger(__name__)

MAX_COMMENT_CHARS = 2000


async def _purchased_in_order(
    db: AsyncSession, user_id: int, order_id: int, product_id: int
) -> Order:
    order = (
        await db.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id, Order.user_id == user_id)
        )
    ).scalar_one_or_none()
    # Same 404 for "not yours" as for "doesn't exist" so order IDs can't be probed.
    if order is None:
        raise NotFoundError("Order not found")

    if order.status != OrderStatus.DELIVERED:
        raise ValidationError("You can review an item once the order is delivered")

    if not any(item.product_id == product_id for item in order.items):
        raise ValidationError("That product was not part of this order")

    return order


async def create_review(
    db: AsyncSession,
    user_id: int,
    product_id: int,
    order_id: int,
    rating: int,
    comment: str | None = None,
) -> Review:
    if rating < 1 or rating > 5:
        raise ValidationError("Rating must be between 1 and 5")

    await _purchased_in_order(db, user_id, order_id, product_id)

    existing = (
        await db.execute(
            select(Review).where(Review.order_id == order_id, Review.product_id == product_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("You have already reviewed this item for this order")

    review = Review(
        user_id=user_id,
        product_id=product_id,
        order_id=order_id,
        rating=rating,
        comment=(comment or "").strip()[:MAX_COMMENT_CHARS] or None,
        verified_purchase=True,
    )
    # Savepoint, not a bare flush: losing the race on the unique index must undo
    # this insert only. A session-wide rollback here would silently discard
    # whatever else the caller had pending in the same transaction.
    try:
        async with db.begin_nested():
            db.add(review)
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError("You have already reviewed this item for this order") from exc

    await _invalidate_rating(product_id)
    log.info("review_created", review_id=review.id, product_id=product_id, rating=rating)
    return review


async def update_review(
    db: AsyncSession,
    user_id: int,
    review_id: int,
    rating: int | None = None,
    comment: str | None = None,
) -> Review:
    review = await _own_review(db, user_id, review_id)

    if rating is not None:
        if rating < 1 or rating > 5:
            raise ValidationError("Rating must be between 1 and 5")
        review.rating = rating
    if comment is not None:
        review.comment = comment.strip()[:MAX_COMMENT_CHARS] or None

    await db.flush()
    await _invalidate_rating(review.product_id)
    return review


async def delete_review(db: AsyncSession, user_id: int, review_id: int) -> None:
    review = await _own_review(db, user_id, review_id)
    review.soft_delete()
    await db.flush()
    await _invalidate_rating(review.product_id)
    log.info("review_deleted", review_id=review_id, user_id=user_id)


async def _own_review(db: AsyncSession, user_id: int, review_id: int) -> Review:
    review = (
        await db.execute(
            select(Review).where(Review.id == review_id, Review.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if review is None:
        raise NotFoundError("Review not found")
    if review.user_id != user_id:
        raise PermissionError_("You can only change your own review")
    return review


def _visible(stmt: Select) -> Select:
    return stmt.where(Review.deleted_at.is_(None), Review.flagged.is_(False))


async def list_product_reviews(
    db: AsyncSession, product_id: int, cursor: str | None = None, limit: int = 20
) -> dict[str, Any]:
    stmt = _visible(
        select(Review)
        .options(selectinload(Review.user))
        .where(Review.product_id == product_id)
    )
    stmt = apply_cursor(stmt, Review.created_at, Review.id, cursor, descending=True)
    rows = (
        (await db.execute(stmt.order_by(Review.created_at.desc(), Review.id.desc()).limit(limit + 1)))
        .scalars()
        .all()
    )
    items, next_cursor, has_more = build_page(rows, limit)
    return {
        "items": [serialize_review(r) for r in items],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


async def list_user_reviews(
    db: AsyncSession, user_id: int, cursor: str | None = None, limit: int = 20
) -> dict[str, Any]:
    stmt = select(Review).where(Review.user_id == user_id, Review.deleted_at.is_(None))
    stmt = apply_cursor(stmt, Review.created_at, Review.id, cursor, descending=True)
    rows = (
        (await db.execute(stmt.order_by(Review.created_at.desc(), Review.id.desc()).limit(limit + 1)))
        .scalars()
        .all()
    )
    items, next_cursor, has_more = build_page(rows, limit)
    return {
        "items": [serialize_review(r) for r in items],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


async def reviewable_items(db: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    """Delivered order lines the shopper has not reviewed yet."""
    reviewed = set(
        (
            await db.execute(
                select(Review.order_id, Review.product_id).where(Review.user_id == user_id)
            )
        ).all()
    )

    rows = (
        await db.execute(
            select(OrderItem, Order)
            .join(Order, OrderItem.order_id == Order.id)
            .where(Order.user_id == user_id, Order.status == OrderStatus.DELIVERED)
            .order_by(Order.id.desc())
        )
    ).all()

    pending = []
    seen: set[tuple[int, int]] = set()
    for item, order in rows:
        key = (order.id, item.product_id)
        if key in reviewed or key in seen:
            continue
        seen.add(key)
        pending.append(
            {
                "order_id": order.id,
                "order_number": order.order_number,
                "product_id": item.product_id,
                "product_name": item.product_name,
                "delivered_at": order.delivered_at,
            }
        )
    return pending


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------
async def set_flagged(db: AsyncSession, review_id: int, flagged: bool) -> Review:
    review = (
        await db.execute(
            select(Review).where(Review.id == review_id, Review.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if review is None:
        raise NotFoundError("Review not found")

    review.flagged = flagged
    await db.flush()
    await _invalidate_rating(review.product_id)
    log.info("review_flag_changed", review_id=review_id, flagged=flagged)
    return review


async def admin_delete_review(db: AsyncSession, review_id: int) -> None:
    review = (
        await db.execute(
            select(Review).where(Review.id == review_id, Review.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if review is None:
        raise NotFoundError("Review not found")
    review.soft_delete()
    await db.flush()
    await _invalidate_rating(review.product_id)


async def _invalidate_rating(product_id: int) -> None:
    await cache_delete(CacheKeys.PRODUCT_RATING.format(product_id=product_id))
    await cache_delete(CacheKeys.PRODUCT_DETAIL.format(product_id=product_id))


def serialize_review(review: Review) -> dict[str, Any]:
    reviewer = getattr(review, "user", None)
    return {
        "id": review.id,
        "product_id": review.product_id,
        "rating": review.rating,
        "comment": review.comment,
        "verified_purchase": review.verified_purchase,
        # Only a first name is exposed — reviews are public.
        "author": (reviewer.display_name.split()[0] if reviewer else "Customer"),
        "created_at": review.created_at,
    }
