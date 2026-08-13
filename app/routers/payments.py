from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Order, Payment, WebhookEvent
from app.schemas import WebhookAck
from app.security import payload_hash, verify_ip_allowed, verify_webhook_hmac_sha256

router = APIRouter(prefix="/api/payments", tags=["payments"])


@router.post("/cashfree-webhook", response_model=WebhookAck)
async def cashfree_webhook(request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else None
    if not verify_ip_allowed(client_ip):
        raise HTTPException(status_code=403, detail="IP not allowed")

    body = await request.body()
    payload = json.loads(body or b"{}")

    event_id = str(payload.get("cf_payment_id") or payload.get("event_id") or payload_hash(body))
    existing = db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == event_id))
    if existing:
        return WebhookAck(status="accepted")

    from app.config import settings

    signature = request.headers.get("x-webhook-signature")
    if settings.cashfree_webhook_secret and not verify_webhook_hmac_sha256(body, signature, settings.cashfree_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    db.add(WebhookEvent(event_id=event_id, source="cashfree", payload_hash=payload_hash(body), status="pending"))

    order_id = payload.get("order_id")
    payment = db.scalar(select(Payment).where(Payment.cashfree_order_id == str(order_id)))
    if payment:
        paid_amount = Decimal(str(payload.get("order_amount") or payment.amount))
        if paid_amount == payment.amount and str(payload.get("payment_status", "")).upper() in {"SUCCESS", "PAID"}:
            payment.status = "paid"
            payment.cashfree_payment_id = payload.get("cf_payment_id")
            order = db.get(Order, payment.order_id)
            if order:
                order.status = "processing"
        elif str(payload.get("payment_status", "")).upper() in {"FAILED", "EXPIRED"}:
            payment.status = "failed"
            order = db.get(Order, payment.order_id)
            if order and order.status == "pending_payment":
                order.status = "payment_failed"
        else:
            payment.status = "under_review"

    db.commit()
    return WebhookAck(status="accepted")
