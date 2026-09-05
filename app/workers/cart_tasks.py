"""Scheduled cart and inventory housekeeping.

These crons are what keep stock from leaking. A reservation that is never
released holds inventory nobody is buying, so `cleanup_reservations` runs far
more often than the others.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import session_scope
from app.models.base import utcnow
from app.models.cart import Cart
from app.models.enums import CartStatus
from logging_config import get_logger

log = get_logger(__name__)


async def cleanup_reservations(ctx: dict) -> dict[str, Any]:
    """Release holds whose payment window closed. Every 5 minutes."""
    from app.services.cart import cleanup_expired_reservations

    async with session_scope() as db:
        released = await cleanup_expired_reservations(db)

    if released:
        log.info("reservations_released", count=released)
    return {"released": released}


async def expire_carts(ctx: dict) -> dict[str, Any]:
    """Expire carts untouched for `CART_TTL_HOURS`. Every 15 minutes."""
    from app.services.cart import expire_stale_carts

    async with session_scope() as db:
        expired = await expire_stale_carts(db)

    if expired:
        log.info("carts_expired", count=expired)
    return {"expired": expired}


async def expire_payments(ctx: dict) -> dict[str, Any]:
    """Fail payment links that were never completed, freeing their stock."""
    from app.services.payment import expire_stale_payments

    async with session_scope() as db:
        expired = await expire_stale_payments(db)

    if expired:
        log.info("payments_expired", count=expired)
    return {"expired": expired}


async def abandoned_cart_nudge(ctx: dict) -> dict[str, Any]:
    """Remind shoppers who left something behind. Every 6 hours.

    Only one nudge per cart, tracked on the cart itself — a reminder that
    repeats every six hours is spam, and on WhatsApp it gets the number
    reported.
    """
    from app.services import cart as cart_service
    from app.services import whatsapp

    cutoff = utcnow() - _hours(settings.abandoned_cart_after_hours)
    nudged = 0

    async with session_scope() as db:
        carts = (
            (
                await db.execute(
                    # `Cart.user` is lazy, and a lazy load inside a coroutine
                    # raises — the owner has to come back with the cart.
                    select(Cart)
                    .options(selectinload(Cart.user))
                    .where(
                        Cart.status == CartStatus.ACTIVE,
                        Cart.updated_at < cutoff,
                        Cart.reminder_sent_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

        for cart in carts:
            if not cart.items:
                continue

            user = cart.user
            if user is None or not user.phone or not user.whatsapp_opt_in:
                continue

            try:
                priced = await cart_service.price_cart(db, cart)
                await whatsapp.send_buttons(
                    user.phone,
                    f"You left {len(cart.items)} item(s) in your cart "
                    f"(Rs.{priced.get('total')}). Want to finish up?",
                    ["Checkout", "View cart"],
                )
                cart.reminder_sent_at = utcnow()
                nudged += 1
            except Exception as exc:
                log.warning("abandoned_cart_nudge_failed", cart_id=cart.id, error=str(exc))

    log.info("abandoned_cart_nudges_sent", count=nudged)
    return {"nudged": nudged}


def _hours(value: int):
    from datetime import timedelta

    return timedelta(hours=value)
