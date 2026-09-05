from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import Intent, MessageType
from app.schemas.common import ORMModel


class MessageOut(ORMModel):
    id: int
    direction: str
    body: str | None = None
    message_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ai_generated: bool = False
    sent_by_agent_id: int | None = None
    delivery_status: str | None = None
    created_at: datetime


class ConversationSummary(ORMModel):
    id: int
    user_id: int
    status: str
    user_name: str | None = None
    user_phone: str | None = None
    assigned_agent_id: int | None = None
    assigned_agent_name: str | None = None
    queue_position: int | None = None
    sla_deadline: datetime | None = None
    last_message_at: datetime | None = None
    unread_count: int = 0
    created_at: datetime


class ConversationDetail(ConversationSummary):
    context: dict[str, Any] = Field(default_factory=dict)
    escalation_reason: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None


class AgentReplyRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4096)
    resolve: bool = False


class AssignConversationRequest(BaseModel):
    agent_id: int | None = None
    take_over: bool = False


class AIAction(BaseModel):
    """One executable step returned by the AI engine."""

    type: str
    product_id: int | None = None
    variant_id: int | None = None
    quantity: int | None = Field(default=None, ge=1, le=99)
    cart_item_id: int | None = None
    order_id: int | None = None
    order_number: str | None = None
    category_id: int | None = None
    query: str | None = None
    reason: str | None = None


class AIResponse(BaseModel):
    intent: Intent = Intent.HELP
    response_text: str = ""
    actions: list[AIAction] = Field(default_factory=list)
    message_type: MessageType = MessageType.TEXT
    buttons: list[str] = Field(default_factory=list, max_length=3)
    product_ids: list[int] = Field(default_factory=list, max_length=30)
    list_items: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    escalate: bool = False
    header: str | None = None


class NotificationOut(ORMModel):
    id: int
    type: str
    title: str
    message: str | None = None
    link: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    read: bool
    created_at: datetime
