"""Outbound customer notifications and agent SLA monitoring."""

from __future__ import annotations

from typing import Any

from app.database import session_scope
from logging_config import get_logger

log = get_logger(__name__)


async def notify_order_placed(ctx: dict, order_id: int) -> dict[str, Any]:
    """Tell the shopper on WhatsApp that payment landed."""
    from app.services import order as order_service
    from app.services import whatsapp

    async with session_scope() as db:
        order = await order_service.get_order(db, order_id)
        user = order.user
        if user is None or not user.phone:
            return {"sent": False, "reason": "no_phone"}

        await whatsapp.send_text(
            user.phone,
            f"Payment received for order {order.order_number} (Rs.{order.total}).\n"
            "Our team is confirming it now and will share a delivery estimate shortly.",
        )
    return {"sent": True, "order_id": order_id}


async def notify_order_confirmed(ctx: dict, order_id: int) -> dict[str, Any]:
    """Send the agent's confirmation and ETA — step 7 of the shopping flow."""
    from app.services import order as order_service
    from app.services import whatsapp

    async with session_scope() as db:
        order = await order_service.get_order(db, order_id)
        user = order.user
        if user is None or not user.phone:
            return {"sent": False, "reason": "no_phone"}

        eta = (
            order.delivery_eta.strftime("%d %b")
            if getattr(order, "delivery_eta", None)
            else "soon"
        )
        lines = [f"Your order {order.order_number} is confirmed.", f"Expected delivery: {eta}."]
        if getattr(order, "tracking_url", None):
            lines.append(f"Track it: {order.tracking_url}")

        await whatsapp.send_text(user.phone, "\n".join(lines))
    return {"sent": True, "order_id": order_id}


async def notify_order_status(ctx: dict, order_id: int, status: str) -> dict[str, Any]:
    from app.services import order as order_service
    from app.services import whatsapp

    message = _STATUS_MESSAGES.get(status)
    if message is None:
        return {"sent": False, "reason": "no_template"}

    async with session_scope() as db:
        order = await order_service.get_order(db, order_id)
        user = order.user
        if user is None or not user.phone:
            return {"sent": False, "reason": "no_phone"}

        await whatsapp.send_text(user.phone, message.format(order_number=order.order_number))
    return {"sent": True, "order_id": order_id, "status": status}


_STATUS_MESSAGES = {
    "shipped": "Order {order_number} has shipped.",
    "out_for_delivery": "Order {order_number} is out for delivery today.",
    "delivered": "Order {order_number} has been delivered. Enjoy!",
    "cancelled": "Order {order_number} has been cancelled. Any payment will be refunded.",
    "refunded": "Refund for order {order_number} has been processed.",
}


async def check_sla_breaches(ctx: dict) -> dict[str, Any]:
    """Re-alert on queued conversations nobody picked up in time."""
    from app.services.conversation import breached_sla
    from app.services.notification import broadcast

    async with session_scope() as db:
        breached = await breached_sla(db)
        for conversation in breached:
            await broadcast(
                db,
                "escalation",
                title="Conversation waiting too long",
                message=f"Conversation #{conversation.id} has passed its response deadline",
                link=f"/admin/conversations/{conversation.id}",
                meta={"conversation_id": conversation.id},
            )
            # Push the deadline out so the next sweep doesn't re-alert immediately.
            conversation.sla_deadline = None

    if breached:
        log.warning("sla_breaches_detected", count=len(breached))
    return {"breached": len(breached)}
