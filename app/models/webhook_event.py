from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel, JSONType
from app.models.enums import EmailType, WebhookStatus


class WebhookEvent(BaseModel):
    """Idempotency ledger. A duplicate event_id short-circuits reprocessing."""

    __tablename__ = "webhook_events"
    __table_args__ = (Index("ix_webhook_events_source_status", "source", "status"),)

    event_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(80))
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(
        String(20), default=WebhookStatus.PENDING, index=True, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailLog(BaseModel):
    __tablename__ = "email_log"
    __table_args__ = (Index("ix_email_log_order_type", "order_id", "type"),)

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="sent", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(BaseModel):
    """Append-only trail for merges, refunds, and other sensitive admin actions."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)

    action: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    actor_type: Mapped[str] = mapped_column(String(20), default="system", nullable=False)
    actor_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


__all__ = ["WebhookEvent", "EmailLog", "AuditLog", "EmailType", "WebhookStatus"]
