"""Transactional email delivery.

`send_order_email` is idempotent against `email_log`, so a retried job will not
send a second copy of the same message for the same order.
"""

from __future__ import annotations

from typing import Any

from app.database import session_scope
from app.models.enums import EmailType
from logging_config import get_logger

log = get_logger(__name__)


async def send_order_email_task(ctx: dict, order_id: int, email_type: str) -> dict[str, Any]:
    from app.services.email import send_order_email

    try:
        kind = EmailType(email_type)
    except ValueError:
        log.warning("unknown_email_type", email_type=email_type, order_id=order_id)
        return {"sent": False, "reason": "unknown_type"}

    async with session_scope() as db:
        sent = await send_order_email(db, order_id, kind)

    return {"order_id": order_id, "type": email_type, "sent": sent}
