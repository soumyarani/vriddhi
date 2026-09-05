from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, JSONType
from app.models.enums import ConversationStatus, MessageDirection, MessageType

if TYPE_CHECKING:
    from app.models.user import Agent, User

CONTEXT_MAX_BYTES = 4096


class Conversation(BaseModel):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_status_created", "status", "created_at"),)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default=ConversationStatus.AI, index=True, nullable=False
    )
    # Rolling AI state: {"summary": str, "recent": [...], "last_intent": str}
    context: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    assigned_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    queue_position: Mapped[int | None] = mapped_column(Integer)
    sla_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalation_reason: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(10))  # "ai" | "human"
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    user: Mapped["User"] = relationship(back_populates="conversations")
    agent: Mapped["Agent | None"] = relationship()
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(BaseModel):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"),)

    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(String(20), default=MessageType.TEXT, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    wa_message_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    sent_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    delivery_status: Mapped[str | None] = mapped_column(String(20))
    error: Mapped[str | None] = mapped_column(Text)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


class Notification(BaseModel):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_agent_read", "agent_id", "read"),)

    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    agent: Mapped["Agent | None"] = relationship(back_populates="notifications")


class AITokenUsage(BaseModel):
    """Per-request AI cost tracking for daily spend limits and dashboards."""

    __tablename__ = "ai_token_usage"
    __table_args__ = (Index("ix_ai_usage_user_created", "user_id", "created_at"),)

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
