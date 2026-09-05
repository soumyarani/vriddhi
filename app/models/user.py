from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, SoftDeleteMixin
from app.models.enums import AgentRole, AuthProvider

if TYPE_CHECKING:
    from app.models.cart import Cart
    from app.models.conversation import Conversation, Notification
    from app.models.order import Order


class User(BaseModel, SoftDeleteMixin):
    __tablename__ = "users"

    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    google_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    picture_url: Mapped[str | None] = mapped_column(Text)
    auth_provider: Mapped[str] = mapped_column(String(20), default=AuthProvider.WHATSAPP)
    whatsapp_opt_in: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    gstin: Mapped[str | None] = mapped_column(String(15))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when this account is absorbed into another during an account merge.
    merged_into_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    addresses: Mapped[list["Address"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    orders: Mapped[list["Order"]] = relationship(back_populates="user")
    carts: Mapped[list["Cart"]] = relationship(back_populates="user")
    conversations: Mapped[list["Conversation"]] = relationship(back_populates="user")

    @property
    def display_name(self) -> str:
        return self.name or self.email or self.phone or f"user-{self.id}"


class Address(BaseModel, SoftDeleteMixin):
    __tablename__ = "addresses"
    __table_args__ = (Index("ix_addresses_user_default", "user_id", "is_default"),)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    label: Mapped[str] = mapped_column(String(50), default="Home")
    recipient_name: Mapped[str | None] = mapped_column(String(255))
    recipient_phone: Mapped[str | None] = mapped_column(String(20))
    line1: Mapped[str] = mapped_column(String(255), nullable=False)
    line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(100), nullable=False)
    pincode: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    country: Mapped[str] = mapped_column(String(100), default="India")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_serviceable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped["User"] = relationship(back_populates="addresses")

    def snapshot(self) -> dict:
        """Frozen copy stored on the order so later edits don't rewrite history."""
        return {
            "label": self.label,
            "recipient_name": self.recipient_name,
            "recipient_phone": self.recipient_phone,
            "line1": self.line1,
            "line2": self.line2,
            "city": self.city,
            "state": self.state,
            "pincode": self.pincode,
            "country": self.country,
        }

    def as_text(self) -> str:
        parts = [self.line1, self.line2, self.city, f"{self.state} {self.pincode}", self.country]
        return ", ".join(p for p in parts if p)


class RefreshToken(BaseModel):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    # Deterministic SHA-256 lookup key; `token_hash` holds the bcrypt verifier.
    lookup_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    device_info: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when rotated, so reuse of an old token can be detected.
    replaced_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )

    user: Mapped["User | None"] = relationship(back_populates="refresh_tokens")


class Agent(BaseModel):
    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    google_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    picture_url: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default=AgentRole.AGENT, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    notifications: Mapped[list["Notification"]] = relationship(back_populates="agent")

    @property
    def is_admin(self) -> bool:
        return self.role == AgentRole.ADMIN
