"""Cashfree payment links, webhook settlement, and refunds.

Card/UPI/bank details never reach this server — customers pay on Cashfree's
hosted page, so the PCI scope stays with them. We only ever hold identifiers,
amounts and statuses.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.errors import ConflictError, NotFoundError, PaymentError, ValidationError
from app.models.enums import OrderStatus, PaymentStatus, RefundStatus
from app.models.order import Order, Payment, Refund
from app.services.tax import q
from logging_config import get_logger

log = get_logger(__name__)

REQUEST_TIMEOUT = 20.0


def _headers() -> dict[str, str]:
    return {
        "x-client-id": settings.cashfree_app_id,
        "x-client-secret": settings.cashfree_secret_key,
        "x-api-version": settings.cashfree_api_version,
        "Content-Type": "application/json",
    }


def _configured() -> bool:
    return bool(settings.cashfree_app_id and settings.cashfree_secret_key)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _request(method: str, path: str, payload: dict | None = None) -> dict[str, Any]:
    url = f"{settings.cashfree_base_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.request(method, url, headers=_headers(), json=payload)
    except httpx.HTTPError as exc:
        log.error("cashfree_unreachable", path=path, error=str(exc))
        raise PaymentError("Payment service temporarily unavailable, please retry") from exc

    if response.status_code >= 400:
        detail = _safe_json(response)
        log.error(
            "cashfree_error",
            path=path,
            status=response.status_code,
            message=detail.get("message"),
        )
        raise PaymentError(
            detail.get("message") or "Payment service rejected the request"
        )

    return _safe_json(response)


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        return {}


# --------------------------------------------------------------------------
# Payment link creation
# --------------------------------------------------------------------------
async def create_payment_link(db: AsyncSession, order: Order, user: Any) -> Payment:
    """Create a Cashfree payment link for an order and persist the Payment row.

    `link_id` doubles as our idempotency key so a retried call cannot create a
    second live link for the same attempt.
    """
    if order.status not in (OrderStatus.PENDING_PAYMENT,):
        raise ConflictError("This order is not awaiting payment")

    idempotency_key = f"{order.order_number}-{uuid.uuid4().hex[:8]}"
    expires_at = _now() + timedelta(minutes=settings.payment_link_ttl_minutes)

    payment = Payment(
        order_id=order.id,
        idempotency_key=idempotency_key,
        cashfree_order_id=idempotency_key,
        amount=order.total,
        currency=order.currency,
        status=PaymentStatus.CREATED,
        expires_at=expires_at,
    )
    db.add(payment)
    await db.flush()

    if not _configured():
        # Local development without Cashfree keys: hand back a stub link so the
        # rest of the checkout flow stays exercisable.
        payment.payment_link = f"{settings.storefront_url}/mock-pay/{idempotency_key}"
        payment.status = PaymentStatus.PENDING
        log.warning("cashfree_not_configured_using_stub_link", order_id=order.id)
        await db.flush()
        return payment

    body = {
        "link_id": idempotency_key,
        "link_amount": float(order.total),
        "link_currency": order.currency,
        "link_purpose": f"Order {order.order_number}",
        "customer_details": {
            "customer_name": (user.name or "Customer")[:100],
            "customer_phone": _digits(user.phone) or "9999999999",
            "customer_email": user.email or "noreply@example.com",
        },
        "link_notify": {"send_sms": False, "send_email": False},
        "link_expiry_time": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "link_meta": {
            "return_url": f"{settings.storefront_url}/orders/{order.id}",
            "notify_url": f"{settings.api_base_url}/api/payments/cashfree-webhook",
        },
        "link_notes": {"order_number": order.order_number, "order_id": str(order.id)},
    }

    data = await _request("POST", "/links", body)

    payment.payment_link = data.get("link_url")
    payment.payment_session_id = data.get("cf_link_id") and str(data["cf_link_id"])
    payment.status = PaymentStatus.PENDING
    payment.raw_response = {k: v for k, v in data.items() if k != "customer_details"}
    await db.flush()

    log.info(
        "payment_link_created",
        order_id=order.id,
        payment_id=payment.id,
        expires_at=expires_at.isoformat(),
    )
    return payment


def _digits(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(c for c in value if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


async def active_payment_for(db: AsyncSession, order_id: int) -> Payment | None:
    stmt = (
        select(Payment)
        .where(Payment.order_id == order_id)
        .order_by(Payment.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def expire_stale_payments(db: AsyncSession) -> int:
    stmt = select(Payment).where(
        Payment.status.in_([PaymentStatus.CREATED, PaymentStatus.PENDING]),
        Payment.expires_at.is_not(None),
        Payment.expires_at <= _now(),
    )
    rows = (await db.execute(stmt)).scalars().all()
    for payment in rows:
        payment.status = PaymentStatus.EXPIRED
    if rows:
        await db.flush()
        log.info("payments_expired", count=len(rows))
    return len(rows)


# --------------------------------------------------------------------------
# Webhook settlement
# --------------------------------------------------------------------------
def extract_event_id(payload: dict[str, Any]) -> str:
    """Stable per-delivery ID used for webhook idempotency."""
    data = payload.get("data") or {}
    payment = data.get("payment") or {}
    order = data.get("order") or {}
    link = data.get("link") or {}
    refund = data.get("refund") or {}

    for candidate in (
        payment.get("cf_payment_id"),
        refund.get("cf_refund_id"),
        order.get("order_id"),
        link.get("link_id"),
    ):
        if candidate:
            return f"cashfree:{payload.get('type', 'event')}:{candidate}"

    return f"cashfree:{payload.get('type', 'event')}:{payload.get('event_time', uuid.uuid4().hex)}"


def _identifiers(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data") or {}
    order = data.get("order") or {}
    link = data.get("link") or {}
    candidates = [
        link.get("link_id"),
        order.get("order_id"),
        (order.get("order_tags") or {}).get("link_id"),
        (data.get("order_meta") or {}).get("link_id"),
    ]
    return [str(c) for c in candidates if c]


async def find_payment_for_event(db: AsyncSession, payload: dict[str, Any]) -> Payment | None:
    identifiers = _identifiers(payload)
    if not identifiers:
        return None

    stmt = (
        select(Payment)
        .options(selectinload(Payment.order))
        .where(
            (Payment.cashfree_order_id.in_(identifiers))
            | (Payment.idempotency_key.in_(identifiers))
        )
        .order_by(Payment.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def settle_payment(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply a Cashfree webhook to our payment + order state.

    Returns a result dict describing what happened so the worker can decide
    on follow-up notifications.
    """
    event_type = str(payload.get("type") or "")
    data = payload.get("data") or {}
    payment_data = data.get("payment") or {}

    payment = await find_payment_for_event(db, payload)
    if payment is None:
        log.warning("cashfree_webhook_unmatched", event_type=event_type)
        return {"matched": False, "reason": "no_matching_payment"}

    if payment.status == PaymentStatus.SUCCESS:
        return {"matched": True, "already_settled": True, "order_id": payment.order_id}

    status = str(payment_data.get("payment_status") or "").upper()
    paid_amount = _to_decimal(payment_data.get("payment_amount"))

    payment.cashfree_payment_id = _str_or_none(payment_data.get("cf_payment_id"))
    payment.payment_method = _payment_method(payment_data)
    payment.paid_amount = paid_amount
    payment.raw_response = payload

    if "FAILED" in event_type or status in ("FAILED", "USER_DROPPED", "CANCELLED"):
        payment.status = PaymentStatus.FAILED
        payment.failure_reason = str(payment_data.get("payment_message") or event_type)[:1000]
        await db.flush()
        log.info("payment_failed", order_id=payment.order_id, payment_id=payment.id)
        return {"matched": True, "settled": False, "order_id": payment.order_id}

    if status != "SUCCESS":
        payment.status = PaymentStatus.PENDING
        await db.flush()
        return {"matched": True, "settled": False, "order_id": payment.order_id, "pending": True}

    # Never trust the amount in the callback — compare against our own total.
    if paid_amount is None or q(paid_amount) != q(payment.amount):
        payment.status = PaymentStatus.FLAGGED
        payment.failure_reason = f"Amount mismatch: expected {payment.amount}, got {paid_amount}"
        await db.flush()
        log.error(
            "payment_amount_mismatch",
            order_id=payment.order_id,
            expected=str(payment.amount),
            received=str(paid_amount),
        )
        return {
            "matched": True,
            "settled": False,
            "flagged": True,
            "order_id": payment.order_id,
            "reason": payment.failure_reason,
        }

    payment.status = PaymentStatus.SUCCESS
    payment.paid_at = _parse_time(payment_data.get("payment_time")) or _now()
    await db.flush()

    log.info("payment_succeeded", order_id=payment.order_id, payment_id=payment.id)
    return {"matched": True, "settled": True, "order_id": payment.order_id}


def _payment_method(payment_data: dict[str, Any]) -> str | None:
    method = payment_data.get("payment_method")
    if isinstance(method, dict) and method:
        return next(iter(method))[:50]
    if isinstance(method, str):
        return method[:50]
    group = payment_data.get("payment_group")
    return str(group)[:50] if group else None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return q(Decimal(str(value)))
    except Exception:
        return None


def _str_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Refunds
# --------------------------------------------------------------------------
async def initiate_refund(
    db: AsyncSession,
    order: Order,
    amount: Decimal | None,
    reason: str,
    agent_id: int | None = None,
) -> Refund:
    payment = (
        await db.execute(
            select(Payment)
            .where(Payment.order_id == order.id, Payment.status == PaymentStatus.SUCCESS)
            .order_by(Payment.created_at.desc())
            .limit(1)
        )
    ).scalars().first()

    if payment is None:
        raise ValidationError("This order has no captured payment to refund")

    already_refunded = await _refunded_total(db, order.id)
    refundable = q(payment.amount - already_refunded)
    refund_amount = q(amount) if amount is not None else refundable

    if refund_amount <= 0:
        raise ValidationError("Refund amount must be greater than zero")
    if refund_amount > refundable:
        raise ValidationError(f"Only {refundable} is available to refund on this order")

    idempotency_key = f"REF-{order.order_number}-{uuid.uuid4().hex[:8]}"
    refund = Refund(
        order_id=order.id,
        payment_id=payment.id,
        idempotency_key=idempotency_key,
        amount=refund_amount,
        reason=reason,
        status=RefundStatus.PENDING,
        initiated_by_agent_id=agent_id,
    )
    db.add(refund)
    await db.flush()

    if not _configured():
        refund.status = RefundStatus.SUCCESS
        refund.processed_at = _now()
        log.warning("cashfree_not_configured_refund_marked_local", refund_id=refund.id)
        await db.flush()
        return refund

    body = {
        "refund_amount": float(refund_amount),
        "refund_id": idempotency_key,
        "refund_note": reason[:100],
    }
    try:
        data = await _request("POST", f"/orders/{payment.cashfree_order_id}/refunds", body)
    except PaymentError:
        refund.status = RefundStatus.FAILED
        await db.flush()
        raise

    refund.cashfree_refund_id = _str_or_none(data.get("cf_refund_id"))
    refund.raw_response = data

    status = str(data.get("refund_status") or "").upper()
    if status == "SUCCESS":
        refund.status = RefundStatus.SUCCESS
        refund.processed_at = _now()
    elif status in ("FAILED", "CANCELLED"):
        refund.status = RefundStatus.FAILED
    else:
        refund.status = RefundStatus.PENDING

    await db.flush()
    log.info(
        "refund_initiated",
        order_id=order.id,
        refund_id=refund.id,
        amount=str(refund_amount),
    )
    return refund


async def _refunded_total(db: AsyncSession, order_id: int) -> Decimal:
    rows = (
        (
            await db.execute(
                select(Refund.amount).where(
                    Refund.order_id == order_id,
                    Refund.status.in_([RefundStatus.PENDING, RefundStatus.SUCCESS]),
                )
            )
        )
        .scalars()
        .all()
    )
    return q(sum(rows, Decimal("0.00")))


async def settle_refund(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    data = (payload.get("data") or {}).get("refund") or {}
    refund_id = data.get("refund_id")
    if not refund_id:
        return {"matched": False}

    refund = (
        await db.execute(select(Refund).where(Refund.idempotency_key == str(refund_id)))
    ).scalars().first()
    if refund is None:
        return {"matched": False}

    status = str(data.get("refund_status") or "").upper()
    if status == "SUCCESS":
        refund.status = RefundStatus.SUCCESS
        refund.processed_at = _now()
    elif status in ("FAILED", "CANCELLED"):
        refund.status = RefundStatus.FAILED

    refund.raw_response = payload
    await db.flush()
    return {"matched": True, "order_id": refund.order_id, "status": refund.status}


async def get_order_or_404(db: AsyncSession, order_id: int) -> Order:
    order = (
        await db.execute(
            select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
        )
    ).scalar_one_or_none()
    if order is None:
        raise NotFoundError("Order not found")
    return order
