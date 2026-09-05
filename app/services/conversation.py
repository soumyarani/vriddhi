"""Conversation state machine and message log.

    ai ──escalate──> queued ──assign──> human ──resolve──> resolved
     ^                                                        |
     └──────────────── reopen on a new inbound message ───────┘

`conversations.context` is the AI's rolling memory and is capped at
CONTEXT_MAX_BYTES (4 KB). It is stored on the row rather than rebuilt from the
message log because replaying every message on each turn would be both slow and
unbounded in cost.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models.base import utcnow
from app.models.conversation import CONTEXT_MAX_BYTES, Conversation, Message
from app.models.enums import ConversationStatus, MessageDirection, MessageType
from app.models.user import Agent, User
from app.pagination import apply_cursor, build_page
from logging_config import get_logger

log = get_logger(__name__)

MAX_RECENT_MESSAGES = 10
RECENT_BODY_CHARS = 400


# --------------------------------------------------------------------------
# Lookup / lifecycle
# --------------------------------------------------------------------------
async def get_or_create_conversation(db: AsyncSession, user_id: int) -> Conversation:
    """The user's open thread, reopening a resolved one on a new message."""
    conversation = (
        await db.execute(
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.created_at.desc(), Conversation.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if conversation is not None and conversation.status != ConversationStatus.RESOLVED:
        return conversation

    if conversation is not None and _within_reopen_window(conversation):
        # A follow-up right after resolution belongs to the same thread; a new
        # topic days later does not.
        conversation.status = ConversationStatus.AI
        conversation.resolved_at = None
        conversation.resolved_by = None
        await db.flush()
        log.info("conversation_reopened", conversation_id=conversation.id)
        return conversation

    conversation = Conversation(user_id=user_id, status=ConversationStatus.AI, context={})
    db.add(conversation)
    await db.flush()
    log.info("conversation_started", conversation_id=conversation.id, user_id=user_id)
    return conversation


def _within_reopen_window(conversation: Conversation) -> bool:
    if conversation.resolved_at is None:
        return True
    age = utcnow() - _aware(conversation.resolved_at)
    return age < timedelta(hours=settings.conversation_reopen_hours)


def _aware(value):
    from datetime import timezone

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def get_conversation(db: AsyncSession, conversation_id: int) -> Conversation:
    conversation = (
        await db.execute(
            select(Conversation)
            .options(selectinload(Conversation.user), selectinload(Conversation.agent))
            .where(Conversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise NotFoundError("Conversation not found")
    return conversation


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------
async def record_inbound(
    db: AsyncSession,
    conversation: Conversation,
    body: str | None,
    message_type: str = MessageType.TEXT,
    wa_message_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Message | None:
    """Log an inbound message. Returns None if this WhatsApp id was already seen.

    Meta retries webhooks, so `wa_message_id` is the deduplication key and the
    unique index is what actually settles a race between two retries.
    """
    if wa_message_id:
        existing = (
            await db.execute(select(Message).where(Message.wa_message_id == wa_message_id))
        ).scalar_one_or_none()
        if existing is not None:
            log.info("inbound_message_duplicate", wa_message_id=wa_message_id)
            return None

    message = Message(
        conversation_id=conversation.id,
        direction=MessageDirection.IN,
        body=body,
        message_type=message_type,
        wa_message_id=wa_message_id,
        payload=payload or {},
    )
    try:
        async with db.begin_nested():
            db.add(message)
            await db.flush()
    except IntegrityError:
        # Savepoint: a session-wide rollback would also undo the conversation
        # this call may have just created or reopened.
        log.info("inbound_message_duplicate_race", wa_message_id=wa_message_id)
        return None

    conversation.last_message_at = utcnow()
    await db.flush()
    return message


async def record_outbound(
    db: AsyncSession,
    conversation: Conversation,
    body: str | None,
    message_type: str = MessageType.TEXT,
    ai_generated: bool = False,
    sent_by_agent_id: int | None = None,
    payload: dict[str, Any] | None = None,
    wa_message_id: str | None = None,
    error: str | None = None,
) -> Message:
    message = Message(
        conversation_id=conversation.id,
        direction=MessageDirection.OUT,
        body=body,
        message_type=message_type,
        ai_generated=ai_generated,
        sent_by_agent_id=sent_by_agent_id,
        payload=payload or {},
        wa_message_id=wa_message_id,
        error=error,
    )
    db.add(message)
    await db.flush()

    conversation.last_message_at = utcnow()
    await db.flush()
    return message


async def recent_messages(
    db: AsyncSession, conversation_id: int, limit: int = MAX_RECENT_MESSAGES
) -> list[Message]:
    rows = (
        (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(reversed(rows))


async def list_messages(
    db: AsyncSession, conversation_id: int, cursor: str | None = None, limit: int = 50
) -> dict[str, Any]:
    stmt: Select = select(Message).where(Message.conversation_id == conversation_id)
    stmt = apply_cursor(stmt, Message.created_at, Message.id, cursor, descending=True)
    rows = (
        (
            await db.execute(
                stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    items, next_cursor, has_more = build_page(rows, limit)
    return {
        "items": [serialize_message(m) for m in items],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


# --------------------------------------------------------------------------
# AI context
# --------------------------------------------------------------------------
def _trim_context(context: dict[str, Any]) -> dict[str, Any]:
    """Shrink `context` until it serialises under CONTEXT_MAX_BYTES.

    Recent turns are dropped oldest-first; the summary is the last thing to go
    because it is the only durable memory of the conversation.
    """
    recent = list(context.get("recent") or [])

    while True:
        candidate = {**context, "recent": recent}
        encoded = json.dumps(candidate, default=str).encode("utf-8")
        if len(encoded) <= CONTEXT_MAX_BYTES:
            return candidate
        if recent:
            recent.pop(0)
            continue

        summary = str(candidate.get("summary") or "")
        if summary:
            # Halve the summary rather than dropping it outright.
            candidate["summary"] = summary[: max(1, len(summary) // 2)]
            context = candidate
            continue

        return {"summary": "", "recent": [], "last_intent": context.get("last_intent")}


async def update_context(
    db: AsyncSession,
    conversation: Conversation,
    user_text: str | None = None,
    assistant_text: str | None = None,
    intent: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    context = dict(conversation.context or {})
    recent = list(context.get("recent") or [])

    if user_text:
        recent.append({"role": "user", "text": user_text[:RECENT_BODY_CHARS]})
    if assistant_text:
        recent.append({"role": "assistant", "text": assistant_text[:RECENT_BODY_CHARS]})

    context["recent"] = recent[-MAX_RECENT_MESSAGES:]
    if intent:
        context["last_intent"] = str(intent)
    if summary is not None:
        context["summary"] = summary

    context = _trim_context(context)
    conversation.context = context
    # JSON columns are replaced wholesale, so reassignment is what marks it dirty.
    await db.flush()
    return context


# --------------------------------------------------------------------------
# Escalation and assignment
# --------------------------------------------------------------------------
async def escalate(
    db: AsyncSession, conversation: Conversation, reason: str | None = None
) -> Conversation:
    if conversation.status == ConversationStatus.HUMAN:
        return conversation

    conversation.status = ConversationStatus.QUEUED
    conversation.escalation_reason = reason
    conversation.sla_deadline = utcnow() + timedelta(minutes=settings.agent_sla_minutes)
    conversation.queue_position = await _next_queue_position(db)
    await db.flush()

    from app.services.notification import notify_escalation

    await notify_escalation(db, conversation, reason)
    log.info("conversation_escalated", conversation_id=conversation.id, reason=reason)
    return conversation


async def _next_queue_position(db: AsyncSession) -> int:
    waiting = await db.scalar(
        select(func.count(Conversation.id)).where(
            Conversation.status == ConversationStatus.QUEUED
        )
    )
    return int(waiting or 0) + 1


async def assign(
    db: AsyncSession, conversation_id: int, agent_id: int, take_over: bool = False
) -> Conversation:
    conversation = await get_conversation(db, conversation_id)

    if (
        conversation.assigned_agent_id is not None
        and conversation.assigned_agent_id != agent_id
        and not take_over
    ):
        raise ConflictError("Another agent is already handling this conversation")

    agent = await db.get(Agent, agent_id)
    if agent is None or not agent.active:
        raise ValidationError("Agent is not active")

    conversation.assigned_agent_id = agent_id
    conversation.status = ConversationStatus.HUMAN
    conversation.queue_position = None
    await db.flush()

    log.info("conversation_assigned", conversation_id=conversation_id, agent_id=agent_id)
    return conversation


async def unassign(db: AsyncSession, conversation_id: int) -> Conversation:
    """Return a conversation to the queue without losing its history."""
    conversation = await get_conversation(db, conversation_id)
    conversation.assigned_agent_id = None
    conversation.status = ConversationStatus.QUEUED
    conversation.queue_position = await _next_queue_position(db)
    await db.flush()
    return conversation


async def resolve(
    db: AsyncSession, conversation_id: int, resolved_by: str = "human"
) -> Conversation:
    conversation = await get_conversation(db, conversation_id)
    conversation.status = ConversationStatus.RESOLVED
    conversation.resolved_at = utcnow()
    conversation.resolved_by = resolved_by
    conversation.queue_position = None
    conversation.sla_deadline = None
    await db.flush()
    log.info("conversation_resolved", conversation_id=conversation_id, by=resolved_by)
    return conversation


async def return_to_ai(db: AsyncSession, conversation_id: int) -> Conversation:
    conversation = await get_conversation(db, conversation_id)
    conversation.status = ConversationStatus.AI
    conversation.assigned_agent_id = None
    conversation.queue_position = None
    conversation.sla_deadline = None
    conversation.escalation_reason = None
    await db.flush()
    return conversation


def is_human_handled(conversation: Conversation) -> bool:
    """The AI must stay silent once a person owns the thread."""
    return conversation.status in (ConversationStatus.QUEUED, ConversationStatus.HUMAN)


# --------------------------------------------------------------------------
# Agent-facing queries
# --------------------------------------------------------------------------
async def list_conversations(
    db: AsyncSession,
    status: str | None = None,
    assigned_agent_id: int | None = None,
    unassigned_only: bool = False,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    stmt: Select = select(Conversation).options(
        selectinload(Conversation.user), selectinload(Conversation.agent)
    )
    if status:
        stmt = stmt.where(Conversation.status == status)
    if assigned_agent_id is not None:
        stmt = stmt.where(Conversation.assigned_agent_id == assigned_agent_id)
    if unassigned_only:
        stmt = stmt.where(Conversation.assigned_agent_id.is_(None))

    sort_column = Conversation.last_message_at
    stmt = apply_cursor(stmt, sort_column, Conversation.id, cursor, descending=True)
    rows = (
        (
            await db.execute(
                stmt.order_by(sort_column.desc().nullslast(), Conversation.id.desc()).limit(
                    limit + 1
                )
            )
        )
        .scalars()
        .all()
    )
    items, next_cursor, has_more = build_page(rows, limit, "last_message_at")
    return {
        "items": [serialize_conversation(c) for c in items],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


async def queue_depth(db: AsyncSession) -> int:
    return int(
        await db.scalar(
            select(func.count(Conversation.id)).where(
                Conversation.status == ConversationStatus.QUEUED
            )
        )
        or 0
    )


async def breached_sla(db: AsyncSession) -> list[Conversation]:
    return list(
        (
            await db.execute(
                select(Conversation).where(
                    Conversation.status == ConversationStatus.QUEUED,
                    Conversation.sla_deadline.is_not(None),
                    Conversation.sla_deadline < utcnow(),
                )
            )
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------
def serialize_message(message: Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "direction": message.direction,
        "body": message.body,
        "message_type": message.message_type,
        "payload": message.payload,
        "ai_generated": message.ai_generated,
        "sent_by_agent_id": message.sent_by_agent_id,
        "delivery_status": message.delivery_status,
        "created_at": message.created_at,
    }


def serialize_conversation(
    conversation: Conversation, detail: bool = False
) -> dict[str, Any]:
    user: User | None = getattr(conversation, "user", None)
    agent: Agent | None = getattr(conversation, "agent", None)

    payload: dict[str, Any] = {
        "id": conversation.id,
        "user_id": conversation.user_id,
        "status": conversation.status,
        "user_name": user.display_name if user else None,
        "user_phone": user.phone if user else None,
        "assigned_agent_id": conversation.assigned_agent_id,
        "assigned_agent_name": agent.name if agent else None,
        "queue_position": conversation.queue_position,
        "sla_deadline": conversation.sla_deadline,
        "last_message_at": conversation.last_message_at,
        "created_at": conversation.created_at,
    }
    if detail:
        payload |= {
            "context": conversation.context,
            "escalation_reason": conversation.escalation_reason,
            "resolved_at": conversation.resolved_at,
            "resolved_by": conversation.resolved_by,
        }
    return payload
