"""Async processing of webhook events.

The HTTP handler only writes a `webhook_events` row and ACKs; everything real
happens here. Each task is keyed on that row, so a retry re-reads the same
stored payload rather than trusting a re-delivery.

Tasks are idempotent at two levels: the event row itself moves to `processed`,
and the underlying services (message dedupe, payment settlement) refuse to
apply the same effect twice.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import session_scope
from app.models.base import utcnow
from app.models.conversation import Conversation
from app.models.enums import ConversationStatus, MessageType, WebhookStatus
from app.models.user import User
from app.models.webhook_event import WebhookEvent
from app.services import ai_chat, conversation as convo_service, whatsapp
from logging_config import get_logger

log = get_logger(__name__)

MAX_EVENT_ERROR_CHARS = 1000


async def _claim(db: AsyncSession, event_id: int) -> WebhookEvent | None:
    """Load an event and mark an attempt. Returns None if already done."""
    event = await db.get(WebhookEvent, event_id)
    if event is None:
        log.warning("webhook_event_missing", event_row_id=event_id)
        return None
    if event.status == WebhookStatus.PROCESSED:
        log.info("webhook_event_already_processed", event_row_id=event_id)
        return None

    event.attempts += 1
    await db.flush()
    return event


def _mark_processed(event: WebhookEvent) -> None:
    event.status = WebhookStatus.PROCESSED
    event.processed_at = utcnow()
    event.error = None


async def _persist_failure(event_row_id: int, exc: Exception) -> None:
    """Record the failure in its own transaction.

    The caller's session is about to be rolled back by `session_scope`, which
    would take the failure marker with it — so this needs a fresh session.
    """
    try:
        async with session_scope() as db:
            event = await db.get(WebhookEvent, event_row_id)
            if event is not None:
                event.status = WebhookStatus.FAILED
                event.error = str(exc)[:MAX_EVENT_ERROR_CHARS]
    except Exception as persist_exc:
        log.warning(
            "webhook_failure_persist_failed",
            event_row_id=event_row_id,
            error=str(persist_exc),
        )


# --------------------------------------------------------------------------
# Inbound WhatsApp messages
# --------------------------------------------------------------------------
async def process_whatsapp_message(ctx: dict, event_row_id: int) -> dict[str, Any]:
    async with session_scope() as db:
        event = await _claim(db, event_row_id)
        if event is None:
            return {"skipped": True}

        try:
            result = await _handle_message(db, event.payload or {})
            _mark_processed(event)
            return result
        except Exception as exc:
            log.exception("whatsapp_message_failed", event_row_id=event_row_id)
            await _persist_failure(event_row_id, exc)
            raise


async def _handle_message(db: AsyncSession, message: dict[str, Any]) -> dict[str, Any]:
    from app.services.user import get_or_create_whatsapp_user

    phone = message.get("from_phone")
    if not phone:
        return {"skipped": "no_sender"}

    user, _ = await get_or_create_whatsapp_user(db, phone, message.get("profile_name"))
    conversation = await convo_service.get_or_create_conversation(db, user.id)

    text = message.get("text") or ""
    reply_id = message.get("interactive_reply_id")

    logged = await convo_service.record_inbound(
        db,
        conversation,
        body=text or reply_id,
        message_type=message.get("type") or MessageType.TEXT,
        wa_message_id=message.get("wa_message_id"),
        payload=message,
    )
    if logged is None:
        return {"duplicate": True}

    if message.get("wa_message_id"):
        await whatsapp.mark_read(message["wa_message_id"])

    # A human owns this thread — stay out of the way, just surface it.
    if convo_service.is_human_handled(conversation):
        await _bump_agent(db, conversation)
        return {"handed_to_human": True, "conversation_id": conversation.id}

    # A tapped button is an unambiguous instruction; asking the model to
    # re-derive an intent we already encoded would only add cost and risk.
    scripted = await _handle_scripted(db, user, conversation, reply_id, message)
    if scripted is not None:
        return scripted

    response = await ai_chat.generate_reply(db, user, conversation, text)
    results = await ai_chat.execute_actions(db, user, response)

    if response.escalate or any(r.get("escalate") for r in results):
        await convo_service.escalate(
            db, conversation, reason=response.response_text or "AI requested escalation"
        )

    body = _apply_action_results(response.response_text, results)
    await _deliver(db, user, conversation, response, body)

    await convo_service.update_context(
        db,
        conversation,
        user_text=text,
        assistant_text=body,
        intent=str(response.intent),
    )
    await ai_chat.maybe_summarize(db, conversation)

    return {"conversation_id": conversation.id, "intent": str(response.intent)}


def _apply_action_results(text: str, results: list[dict[str, Any]]) -> str:
    """Let real outcomes override the model's optimism.

    The model writes its reply before the action runs, so if the action failed
    (out of stock, order not found) its text would otherwise be a lie.
    """
    failures = [r for r in results if not r.get("ok") and r.get("message")]
    if failures:
        return failures[0]["message"]

    for result in results:
        if result.get("type") == "checkout" and result.get("payment_link"):
            return (
                f"{text}\n\nOrder {result['order_number']} — Rs.{result['total']}\n"
                f"Pay securely here: {result['payment_link']}"
            )
    return text


# --------------------------------------------------------------------------
# Scripted (non-AI) paths
# --------------------------------------------------------------------------
async def _handle_scripted(
    db: AsyncSession,
    user: User,
    conversation: Conversation,
    reply_id: str | None,
    message: dict[str, Any],
) -> dict[str, Any] | None:
    """Handle taps and catalogue orders without spending a model call."""
    if message.get("order_items"):
        return await _handle_catalog_order(db, user, conversation, message["order_items"])

    if not reply_id:
        return None

    if reply_id == "talk_to_human":
        await convo_service.escalate(db, conversation, reason="Customer asked for a human")
        await _send_text(
            db,
            user,
            conversation,
            "Sure — connecting you with someone from our team. They'll reply here shortly.",
        )
        return {"escalated": True}

    if reply_id.startswith("category_"):
        return await _send_category(db, user, conversation, reply_id)

    return None


async def _handle_catalog_order(
    db: AsyncSession,
    user: User,
    conversation: Conversation,
    order_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """A cart submitted straight from the Meta catalogue.

    Retailer ids were minted by `catalog_sync.build_retailer_id`, so they decode
    back to our own product and variant ids.
    """
    from app.services import cart as cart_service

    added, failed = 0, []
    for item in order_items:
        ids = _decode_retailer_id(item.get("product_retailer_id"))
        if ids is None:
            failed.append(item.get("product_retailer_id"))
            continue

        product_id, variant_id = ids
        if variant_id is None:
            failed.append(item.get("product_retailer_id"))
            continue

        try:
            await cart_service.add_item(
                db,
                user_id=user.id,
                product_id=product_id,
                variant_id=variant_id,
                quantity=max(1, int(item.get("quantity") or 1)),
            )
            added += 1
        except Exception as exc:
            log.info("catalog_order_item_rejected", error=str(exc))
            failed.append(item.get("product_retailer_id"))

    if added == 0:
        await _send_text(
            db, user, conversation, "Sorry, those items are out of stock right now."
        )
        return {"added": 0}

    cart = await cart_service.get_active_cart(db, user.id)
    priced = await cart_service.price_cart(db, cart) if cart else {}
    note = f"\n({len(failed)} item(s) were unavailable.)" if failed else ""

    await _send_buttons(
        db,
        user,
        conversation,
        f"Added {added} item(s) to your cart.\nTotal: Rs.{priced.get('total', '0.00')}{note}",
        ["Checkout", "Keep shopping", "Talk to human"],
    )
    return {"added": added, "failed": len(failed)}


async def _send_category(
    db: AsyncSession, user: User, conversation: Conversation, reply_id: str
) -> dict[str, Any]:
    from app.schemas.product import ProductFilters
    from app.services import product as product_service

    try:
        category_id = int(reply_id.removeprefix("category_"))
    except ValueError:
        return {"skipped": "bad_category"}

    page = await product_service.list_products(
        db, ProductFilters(category_id=category_id), limit=10
    )
    items = page.get("items") or []
    if not items:
        await _send_text(db, user, conversation, "Nothing in that category right now.")
        return {"products": 0}

    lines = "\n".join(f"- {p['name']} — Rs.{p.get('price')}" for p in items)
    await _send_buttons(
        db,
        user,
        conversation,
        f"Here's what we have:\n{lines}\n\nTell me which one you'd like.",
        ["View cart", "Talk to human"],
    )
    return {"products": len(items)}


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------
async def _deliver(
    db: AsyncSession,
    user: User,
    conversation: Conversation,
    response: Any,
    body: str,
) -> None:
    """Send the AI's reply in whichever WhatsApp format it asked for.

    Falls back to plain text if the richer format is rejected — a shopper
    getting a plain message beats getting nothing.
    """
    kind = response.message_type
    try:
        if kind == MessageType.BUTTONS and response.buttons:
            result = await whatsapp.send_buttons(
                user.phone, body, list(response.buttons), header=response.header
            )
        elif kind == MessageType.LIST and response.list_items:
            result = await whatsapp.send_list(
                user.phone,
                body,
                button_text="Choose",
                sections=[{"title": "Options", "rows": response.list_items}],
                header=response.header,
            )
        elif kind == MessageType.PRODUCT_LIST and response.product_ids:
            result = await _send_products(db, user, body, response)
        else:
            result = await whatsapp.send_text(user.phone, body)
    except Exception as exc:
        log.warning("rich_reply_failed_falling_back", error=str(exc))
        try:
            result = await whatsapp.send_text(user.phone, body)
            kind = MessageType.TEXT
        except Exception as fallback_exc:
            await convo_service.record_outbound(
                db,
                conversation,
                body=body,
                ai_generated=True,
                error=str(fallback_exc),
            )
            raise

    await convo_service.record_outbound(
        db,
        conversation,
        body=body,
        message_type=kind,
        ai_generated=True,
        wa_message_id=_wa_id(result),
        payload={"intent": str(response.intent)},
    )


async def _send_products(db: AsyncSession, user: User, body: str, response: Any) -> dict:
    from app.services.catalog_sync import build_retailer_id
    from app.services.product import get_product

    sections = []
    for product_id in response.product_ids[:10]:
        product = await get_product(db, product_id)
        variants = [v for v in product.variants if v.active and not v.is_deleted]
        if not variants:
            continue
        sections.append(
            {
                "product_retailer_id": variants[0].meta_retailer_id
                or build_retailer_id(product.id, variants[0].id)
            }
        )

    if not sections:
        return await whatsapp.send_text(user.phone, body)

    return await whatsapp.send_product_list(
        user.phone,
        header=response.header or "Our picks",
        body=body,
        sections=[{"title": "Products", "product_items": sections}],
    )


async def _send_text(
    db: AsyncSession, user: User, conversation: Conversation, body: str
) -> None:
    result = await whatsapp.send_text(user.phone, body)
    await convo_service.record_outbound(
        db, conversation, body=body, wa_message_id=_wa_id(result)
    )


async def _send_buttons(
    db: AsyncSession,
    user: User,
    conversation: Conversation,
    body: str,
    buttons: list[str],
) -> None:
    result = await whatsapp.send_buttons(user.phone, body, buttons)
    await convo_service.record_outbound(
        db,
        conversation,
        body=body,
        message_type=MessageType.BUTTONS,
        wa_message_id=_wa_id(result),
    )


def _wa_id(result: dict | None) -> str | None:
    if not isinstance(result, dict):
        return None
    messages = result.get("messages") or []
    return messages[0].get("id") if messages else None


def _decode_retailer_id(retailer_id: str | None) -> tuple[int, int | None] | None:
    """`prod_12_v34` -> (12, 34); `prod_12` -> (12, None)."""
    if not retailer_id or not retailer_id.startswith("prod_"):
        return None
    body = retailer_id.removeprefix("prod_")
    try:
        if "_v" in body:
            product_part, variant_part = body.split("_v", 1)
            return int(product_part), int(variant_part)
        return int(body), None
    except ValueError:
        return None


async def _bump_agent(db: AsyncSession, conversation: Conversation) -> None:
    if conversation.status == ConversationStatus.QUEUED:
        return
    from app.services.notification import notify_escalation

    await notify_escalation(db, conversation, reason="New message on an assigned chat")


# --------------------------------------------------------------------------
# Delivery status callbacks
# --------------------------------------------------------------------------
async def process_message_status(ctx: dict, event_row_id: int) -> dict[str, Any]:
    from sqlalchemy import select

    from app.models.conversation import Message

    async with session_scope() as db:
        event = await _claim(db, event_row_id)
        if event is None:
            return {"skipped": True}

        payload = event.payload or {}
        wa_id = payload.get("wa_message_id")

        message = (
            await db.execute(select(Message).where(Message.wa_message_id == wa_id))
        ).scalar_one_or_none()
        if message is not None:
            message.delivery_status = payload.get("status")

        _mark_processed(event)
        return {"updated": message is not None}


# --------------------------------------------------------------------------
# Payments
# --------------------------------------------------------------------------
async def process_payment_event(ctx: dict, event_row_id: int) -> dict[str, Any]:
    from app.services import payment as payment_service

    async with session_scope() as db:
        event = await _claim(db, event_row_id)
        if event is None:
            return {"skipped": True}

        payload = event.payload or {}
        event_type = str(payload.get("type") or "").upper()

        try:
            if "REFUND" in event_type:
                result = await payment_service.settle_refund(db, payload)
            else:
                result = await payment_service.settle_payment(db, payload)

            _mark_processed(event)
        except Exception as exc:
            log.exception("payment_event_failed", event_row_id=event_row_id)
            await _persist_failure(event_row_id, exc)
            raise

        await _after_payment(db, result)
        return result


async def _after_payment(db: AsyncSession, result: dict[str, Any]) -> None:
    """Fan out the consequences of a settled payment.

    A failure here must not roll back the settlement itself — the money moved
    regardless of whether we managed to send the confirmation.
    """
    order_id = result.get("order_id")
    if not order_id:
        return

    from app.models.enums import EmailType
    from app.redis import enqueue
    from app.services import order as order_service
    from app.services.notification import notify_new_order, notify_payment_flagged

    try:
        order = await order_service.get_order(db, order_id)
    except Exception:
        return

    if result.get("flagged"):
        from app.services.payment import active_payment_for

        payment = await active_payment_for(db, order_id)
        if payment is not None:
            await notify_payment_flagged(db, payment, order)
        return

    if not result.get("settled"):
        return

    await notify_new_order(db, order)
    await enqueue("send_order_email_task", order_id, EmailType.ORDER_CONFIRMED.value)
    await enqueue("notify_order_placed", order_id)


# --------------------------------------------------------------------------
# Recovery sweep
# --------------------------------------------------------------------------
async def sweep_pending_events(ctx: dict, older_than_minutes: int = 5) -> dict[str, Any]:
    """Re-drive events that were recorded but never processed.

    The webhook handler commits the event row before enqueueing, so a Redis
    blip between those two steps leaves a `pending` row with no job. Without
    this sweep that message is silently lost — the customer's payment settles
    and nobody ever hears about it.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from app.redis import enqueue

    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    handlers = {
        "cashfree": "process_payment_event",
        "meta": "process_whatsapp_message",
    }
    requeued = 0

    async with session_scope() as db:
        stale = (
            (
                await db.execute(
                    select(WebhookEvent)
                    .where(
                        WebhookEvent.status == WebhookStatus.PENDING,
                        WebhookEvent.created_at < cutoff,
                        WebhookEvent.attempts < 3,
                    )
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )

        for event in stale:
            job = handlers.get(event.source)
            if event.source == "meta" and event.event_type == "status":
                job = "process_message_status"
            if job and await enqueue(job, event.id):
                requeued += 1

    if requeued:
        log.warning("webhook_events_requeued", count=requeued)
    return {"requeued": requeued}
