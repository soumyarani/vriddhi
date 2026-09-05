"""In-app notifications for agents.

Fan-out targets active agents only; a notification with `agent_id = NULL` is a
broadcast every agent sees. Nothing here sends email or WhatsApp — those are
separate channels owned by `services.email` and `services.whatsapp`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError
from app.models.base import utcnow
from app.models.conversation import Notification
from app.models.enums import NotificationType
from app.models.user import Agent
from app.pagination import apply_cursor, build_page
from logging_config import get_logger

log = get_logger(__name__)


async def create_notification(
    db: AsyncSession,
    type_: str,
    title: str,
    message: str | None = None,
    link: str | None = None,
    agent_id: int | None = None,
    meta: dict[str, Any] | None = None,
) -> Notification:
    notification = Notification(
        agent_id=agent_id,
        type=type_,
        title=title,
        message=message,
        link=link,
        meta=meta or {},
    )
    db.add(notification)
    await db.flush()
    log.info("notification_created", type=type_, agent_id=agent_id, notification_id=notification.id)
    return notification


async def broadcast(
    db: AsyncSession,
    type_: str,
    title: str,
    message: str | None = None,
    link: str | None = None,
    meta: dict[str, Any] | None = None,
) -> list[Notification]:
    """One row per active agent, so read state is tracked individually."""
    agent_ids = list(
        (await db.execute(select(Agent.id).where(Agent.active.is_(True)))).scalars().all()
    )
    if not agent_ids:
        # Nobody to notify yet — keep an unassigned row so it isn't lost.
        return [await create_notification(db, type_, title, message, link, None, meta)]

    return [
        await create_notification(db, type_, title, message, link, agent_id, meta)
        for agent_id in agent_ids
    ]


# --------------------------------------------------------------------------
# Event helpers — the vocabulary the rest of the app uses
# --------------------------------------------------------------------------
async def notify_new_order(db: AsyncSession, order: Any) -> None:
    await broadcast(
        db,
        NotificationType.NEW_ORDER,
        title=f"New order {order.order_number}",
        message=f"{order.total} awaiting confirmation",
        link=f"/admin/orders/{order.id}",
        meta={"order_id": order.id, "order_number": order.order_number},
    )


async def notify_escalation(db: AsyncSession, conversation: Any, reason: str | None = None) -> None:
    await broadcast(
        db,
        NotificationType.ESCALATION,
        title="Conversation needs a human",
        message=reason or conversation.escalation_reason or "Escalated from AI",
        link=f"/admin/conversations/{conversation.id}",
        meta={"conversation_id": conversation.id, "user_id": conversation.user_id},
    )


async def notify_payment_flagged(db: AsyncSession, payment: Any, order: Any) -> None:
    await broadcast(
        db,
        NotificationType.PAYMENT_FLAGGED,
        title=f"Payment mismatch on {order.order_number}",
        message=payment.failure_reason or "Paid amount does not match the order total",
        link=f"/admin/orders/{order.id}",
        meta={"order_id": order.id, "payment_id": payment.id},
    )


async def notify_low_stock(db: AsyncSession, variant: Any, available: int) -> None:
    await broadcast(
        db,
        NotificationType.LOW_STOCK,
        title=f"Low stock: {variant.sku}",
        message=f"{available} left",
        link=f"/admin/products/{variant.product_id}",
        meta={"variant_id": variant.id, "available": available},
    )


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
def _visible_to(agent_id: int) -> Any:
    # A NULL agent_id is a broadcast nobody owns yet.
    return (Notification.agent_id == agent_id) | (Notification.agent_id.is_(None))


async def list_notifications(
    db: AsyncSession,
    agent_id: int,
    unread_only: bool = False,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    stmt: Select = select(Notification).where(_visible_to(agent_id))
    if unread_only:
        stmt = stmt.where(Notification.read.is_(False))

    stmt = apply_cursor(stmt, Notification.created_at, Notification.id, cursor, descending=True)
    rows = (
        (
            await db.execute(
                stmt.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(
                    limit + 1
                )
            )
        )
        .scalars()
        .all()
    )
    items, next_cursor, has_more = build_page(rows, limit)
    return {
        "items": [serialize_notification(n) for n in items],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


async def unread_count(db: AsyncSession, agent_id: int) -> int:
    return int(
        await db.scalar(
            select(func.count(Notification.id)).where(
                _visible_to(agent_id), Notification.read.is_(False)
            )
        )
        or 0
    )


async def mark_read(db: AsyncSession, agent_id: int, notification_id: int) -> Notification:
    notification = (
        await db.execute(
            select(Notification).where(
                Notification.id == notification_id, _visible_to(agent_id)
            )
        )
    ).scalar_one_or_none()
    if notification is None:
        raise NotFoundError("Notification not found")

    if not notification.read:
        notification.read = True
        notification.read_at = utcnow()
        await db.flush()
    return notification


async def mark_all_read(db: AsyncSession, agent_id: int) -> int:
    result = await db.execute(
        update(Notification)
        .where(_visible_to(agent_id), Notification.read.is_(False))
        .values(read=True, read_at=utcnow())
    )
    await db.flush()
    return result.rowcount or 0


def serialize_notification(notification: Notification) -> dict[str, Any]:
    return {
        "id": notification.id,
        "type": notification.type,
        "title": notification.title,
        "message": notification.message,
        "link": notification.link,
        "meta": notification.meta,
        "read": notification.read,
        "created_at": notification.created_at,
    }
