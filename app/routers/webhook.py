"""Inbound webhooks from Meta and Cashfree.

Every handler follows the same four steps, in this order:

1. **Verify the signature** before parsing anything. An unsigned body is not
   trusted enough to JSON-decode into our own models.
2. **Check the source IP** when `WEBHOOK_IP_WHITELIST_ENABLED` is on. Off by
   default so local development works without tunnelling.
3. **Write an idempotency row**, keyed on the provider's own event id. A
   duplicate delivery returns 200 without doing the work twice — providers
   retry aggressively and a retried payment must not settle twice.
4. **ACK immediately** and let arq do the real work. Meta retries anything
   slower than a few seconds, so processing inline would guarantee duplicates.

Handlers return 200 even for events we do not recognise. A 4xx just makes the
provider retry a payload we already decided to ignore.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import DbSession
from app.models.enums import WebhookStatus
from app.models.webhook_event import WebhookEvent
from app.redis import enqueue
from app.security import (
    check_cashfree_ip,
    check_meta_ip,
    client_ip,
    payload_hash,
    verify_cashfree_signature,
    verify_meta_signature,
    verify_meta_verify_token,
)
from app.services import whatsapp as whatsapp_service
from logging_config import get_logger

router = APIRouter(prefix="/webhook", tags=["webhooks"])
log = get_logger(__name__)

ACK = {"status": "ok"}


async def _record_event(
    db: AsyncSession,
    event_id: str,
    source: str,
    event_type: str | None,
    raw_body: bytes,
    payload: dict[str, Any],
) -> WebhookEvent | None:
    """Claim an event id. Returns None when it was already claimed.

    The unique index on `event_id` is what actually decides the race between
    two concurrent retries — the SELECT is only a cheap fast path.
    """
    digest = payload_hash(raw_body)

    existing = await db.scalar(
        select(WebhookEvent.id).where(WebhookEvent.event_id == event_id)
    )
    if existing is not None:
        log.info("webhook_duplicate", source=source, event_id=event_id)
        return None

    event = WebhookEvent(
        event_id=event_id,
        source=source,
        event_type=event_type,
        payload_hash=digest,
        payload=payload,
        status=WebhookStatus.PENDING,
    )
    try:
        async with db.begin_nested():
            db.add(event)
            await db.flush()
    except IntegrityError:
        # Savepoint so only the losing insert is undone; the session stays usable.
        log.info("webhook_duplicate_race", source=source, event_id=event_id)
        return None

    return event


# --------------------------------------------------------------------------
# Meta / WhatsApp
# --------------------------------------------------------------------------
@router.get("")
async def verify_webhook(request: Request) -> Response:
    """Meta's subscription handshake: echo hub.challenge when the token matches."""
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and verify_meta_verify_token(
        params.get("hub.verify_token")
    ):
        log.info("meta_webhook_verified")
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")

    log.warning("meta_webhook_verify_failed", ip=client_ip(request))
    return Response(content="Verification failed", status_code=403)


@router.post("")
async def meta_webhook(request: Request, db: DbSession) -> Any:
    raw_body = await request.body()

    if not verify_meta_signature(raw_body, request.headers.get("x-hub-signature-256")):
        log.warning("meta_webhook_bad_signature", ip=client_ip(request))
        return Response(content="Invalid signature", status_code=401)

    if not check_meta_ip(request):
        log.warning("meta_webhook_ip_rejected", ip=client_ip(request))
        return Response(content="Forbidden", status_code=403)

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        log.warning("meta_webhook_bad_json")
        return ACK

    queued = 0

    for message in whatsapp_service.parse_incoming(payload):
        wa_id = message.get("wa_message_id")
        if not wa_id or not message.get("from_phone"):
            continue

        event = await _record_event(
            db,
            event_id=f"wa:{wa_id}",
            source="meta",
            event_type=message.get("type"),
            raw_body=raw_body,
            payload=message,
        )
        if event is None:
            continue

        await db.commit()
        await enqueue("process_whatsapp_message", event.id)
        queued += 1

    for status in whatsapp_service.parse_statuses(payload):
        wa_id = status.get("wa_message_id")
        if not wa_id or not status.get("status"):
            continue

        event = await _record_event(
            db,
            event_id=f"wa-status:{wa_id}:{status['status']}",
            source="meta",
            event_type="status",
            raw_body=raw_body,
            payload=status,
        )
        if event is None:
            continue

        await db.commit()
        await enqueue("process_message_status", event.id)
        queued += 1

    await db.commit()
    return {**ACK, "queued": queued}


# --------------------------------------------------------------------------
# Cashfree
# --------------------------------------------------------------------------
@router.post("/cashfree")
async def cashfree_webhook(request: Request, db: DbSession) -> Any:
    raw_body = await request.body()

    if not verify_cashfree_signature(
        raw_body,
        request.headers.get("x-webhook-signature"),
        request.headers.get("x-webhook-timestamp"),
    ):
        log.warning("cashfree_webhook_bad_signature", ip=client_ip(request))
        return Response(content="Invalid signature", status_code=401)

    if not check_cashfree_ip(request):
        log.warning("cashfree_webhook_ip_rejected", ip=client_ip(request))
        return Response(content="Forbidden", status_code=403)

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        log.warning("cashfree_webhook_bad_json")
        return ACK

    event_id = _cashfree_event_id(payload)
    if event_id is None:
        log.warning("cashfree_webhook_unidentifiable", type=payload.get("type"))
        return ACK

    event = await _record_event(
        db,
        event_id=event_id,
        source="cashfree",
        event_type=payload.get("type"),
        raw_body=raw_body,
        payload=payload,
    )
    if event is None:
        return {**ACK, "duplicate": True}

    await db.commit()
    await enqueue("process_payment_event", event.id)
    return {**ACK, "queued": 1}


def _cashfree_event_id(payload: dict[str, Any]) -> str | None:
    """Build a stable id from whatever the payload actually carries.

    Cashfree does not send a single canonical event id across webhook types, so
    the id is composed from the payment/link identifier plus the event type —
    which is what makes a retry of the *same* event collide, while a genuine
    later event (e.g. a refund after a payment) does not.
    """
    data = payload.get("data") or {}
    event_type = payload.get("type") or "unknown"

    payment = data.get("payment") or {}
    order = data.get("order") or {}
    link = data.get("link") or {}
    refund = data.get("refund") or {}

    identifier = (
        refund.get("refund_id")
        or payment.get("cf_payment_id")
        or link.get("link_id")
        or order.get("order_id")
    )
    if not identifier:
        return None
    return f"cf:{event_type}:{identifier}"
