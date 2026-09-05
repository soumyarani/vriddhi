"""Transactional email over SMTP.

Delivery is best-effort: every public entry point swallows transport failures so
an unreachable SMTP server can never roll back an order state transition. Every
attempt is recorded in `email_log`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.message import EmailMessage
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.enums import EmailType
from app.models.order import Order
from app.models.user import User
from app.models.webhook_event import EmailLog
from app.services.invoice import format_money
from logging_config import get_logger

log = get_logger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "email"

STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

SMTP_TIMEOUT_SECONDS = 20.0

SUBJECTS: dict[str, str] = {
    EmailType.ORDER_CONFIRMED: "Order {order_number} confirmed",
    EmailType.ORDER_SHIPPED: "Order {order_number} has shipped",
    EmailType.ORDER_DELIVERED: "Order {order_number} delivered",
    EmailType.REFUND_PROCESSED: "Refund processed for order {order_number}",
    EmailType.ACCOUNT_LINKED: "Your {store_name} account is linked",
}

# Autoescape is mandatory: product names, addresses and coupon codes are all
# user-influenced and land inside the rendered HTML.
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(default_for_string=True, default=True),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters["money"] = lambda value: f"₹{format_money(value)}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# HTML -> plaintext
# --------------------------------------------------------------------------
_BLOCK_TAGS = {
    "p", "div", "br", "tr", "table", "h1", "h2", "h3", "h4", "li", "ul", "ol", "hr",
}
_SKIP_TAGS = {"style", "script", "head", "title"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append(" ")

    # Self-closing tags default to firing start *and* end, which would double
    # every <br /> into a blank line.
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        # Source-formatting newlines inside a cell are layout, not content; only
        # tags are allowed to introduce line breaks in the plaintext part.
        if not self._skip_depth:
            self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = unescape("".join(parser.parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _base_context() -> dict[str, Any]:
    return {
        "store_name": settings.email_from_name,
        "storefront_url": settings.storefront_url,
        "support_email": settings.email_from,
        "currency": settings.currency,
        "year": _utcnow().year,
    }


def render_template(name: str, context: dict) -> tuple[str, str]:
    """Render `<name>.html` and derive its plaintext fallback."""
    template = _env.get_template(f"{name}.html")
    html = template.render({**_base_context(), **context})
    return html, html_to_text(html)


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------
def _build_message(to: str, subject: str, html: str, text: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = f"{settings.email_from_name} <{settings.email_from}>"
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


async def _deliver(to: str, subject: str, html: str, text: str) -> tuple[bool, str | None]:
    message = _build_message(to, subject, html, text)

    # Port 465 is implicit TLS; issuing STARTTLS on it fails, and aiosmtplib
    # rejects both flags being set at once.
    implicit_tls = settings.smtp_port == 465
    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username or None,
            password=settings.smtp_password or None,
            use_tls=implicit_tls,
            start_tls=settings.smtp_use_tls and not implicit_tls,
            timeout=SMTP_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, None


async def send_email(to: str, subject: str, html: str, text: str | None = None) -> bool:
    if not settings.email_enabled:
        log.info("email_disabled_skipped", email=to, subject=subject)
        return True

    body = text if text is not None else html_to_text(html)
    delivered, error = await _deliver(to, subject, html, body)
    if delivered:
        log.info("email_sent", email=to, subject=subject)
    else:
        log.warning("email_send_failed", email=to, subject=subject, error=error)
    return delivered


# --------------------------------------------------------------------------
# Order emails
# --------------------------------------------------------------------------
def _address_text(address: dict[str, Any]) -> str:
    city_bits = [address.get("city"), address.get("state"), address.get("pincode")]
    lines = [
        address.get("line1"),
        address.get("line2"),
        ", ".join(str(bit) for bit in city_bits if bit),
        address.get("country"),
    ]
    return "\n".join(str(line) for line in lines if line)


def build_order_context(order: Order, user: User | None) -> dict[str, Any]:
    address = dict(order.address_snapshot or {})
    refund_amount = None
    refunds = [r for r in (order.refunds or []) if r.status == "success"] or list(
        order.refunds or []
    )
    if refunds:
        refund_amount = max(refunds, key=lambda r: r.id).amount

    return {
        "order": {
            "number": order.order_number,
            "status": order.status,
            "subtotal": order.subtotal,
            "discount": order.discount_amount,
            "tax": order.tax_amount,
            "shipping": order.shipping_cost,
            "total": order.total,
            "currency": order.currency,
            "coupon_code": order.coupon_code,
            "tracking_number": order.tracking_number,
            "tracking_url": order.tracking_url,
            "delivery_eta": order.delivery_eta.strftime("%d %b %Y") if order.delivery_eta else None,
            "placed_at": order.created_at.strftime("%d %b %Y") if order.created_at else None,
            "url": f"{settings.storefront_url.rstrip('/')}/orders/{order.order_number}",
        },
        "items": [
            {
                "name": item.product_name,
                "variant": item.variant_name,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "line_total": item.line_total,
            }
            for item in order.items
        ],
        "customer": {
            "name": (user.display_name if user else None)
            or address.get("recipient_name")
            or "there",
            "email": user.email if user else None,
        },
        "address_text": _address_text(address),
        "refund_amount": refund_amount,
    }


async def _record(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: int | None,
    email_type: str,
    recipient: str,
    subject: str,
    status: str,
    error: str | None = None,
) -> None:
    db.add(
        EmailLog(
            user_id=user_id,
            order_id=order_id,
            type=email_type,
            recipient=recipient,
            subject=subject,
            status=status,
            error=error,
            sent_at=_utcnow(),
        )
    )
    await db.flush()


async def _already_sent(db: AsyncSession, order_id: int, email_type: str) -> bool:
    result = await db.execute(
        select(EmailLog.id)
        .where(
            EmailLog.order_id == order_id,
            EmailLog.type == email_type,
            EmailLog.status == STATUS_SENT,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def send_order_email(db: AsyncSession, order_id: int, email_type: EmailType) -> bool:
    type_value = str(email_type)
    try:
        if await _already_sent(db, order_id, type_value):
            log.info("email_already_sent", order_id=order_id, type=type_value)
            return True

        result = await db.execute(
            select(Order)
            .options(selectinload(Order.items), selectinload(Order.refunds))
            .where(Order.id == order_id)
        )
        order = result.scalar_one_or_none()
        if order is None:
            log.warning("email_order_missing", order_id=order_id, type=type_value)
            return False

        user = (
            await db.execute(select(User).where(User.id == order.user_id))
        ).scalar_one_or_none()
        recipient = user.email if user else None
        if not recipient:
            log.info("email_no_recipient", order_id=order_id, type=type_value)
            return False

        context = build_order_context(order, user)
        subject = SUBJECTS.get(email_type, "Update on order {order_number}").format(
            order_number=order.order_number, store_name=settings.email_from_name
        )
        html, text = render_template(type_value, context)

        if not settings.email_enabled:
            log.info("email_disabled_skipped", order_id=order_id, type=type_value)
            await _record(
                db,
                user_id=order.user_id,
                order_id=order_id,
                email_type=type_value,
                recipient=recipient,
                subject=subject,
                status=STATUS_SKIPPED,
            )
            return True

        delivered, error = await _deliver(recipient, subject, html, text)
        await _record(
            db,
            user_id=order.user_id,
            order_id=order_id,
            email_type=type_value,
            recipient=recipient,
            subject=subject,
            status=STATUS_SENT if delivered else STATUS_FAILED,
            error=error,
        )
        if delivered:
            log.info("email_sent", order_id=order_id, type=type_value)
        else:
            log.warning("email_send_failed", order_id=order_id, type=type_value, error=error)
        return delivered

    except TemplateNotFound as exc:
        log.error("email_template_missing", order_id=order_id, type=type_value, error=str(exc))
        return False
    except Exception as exc:
        log.error(
            "email_send_error",
            order_id=order_id,
            type=type_value,
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
