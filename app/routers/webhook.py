from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import WebhookEvent
from app.schemas import WebhookAck
from app.security import payload_hash, verify_ip_allowed, verify_webhook_hmac_sha256

router = APIRouter(tags=["webhook"])


@router.get("/webhook")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token:
        return int(challenge or "0")
    raise HTTPException(status_code=400, detail="Invalid verification request")


@router.post("/webhook", response_model=WebhookAck)
async def whatsapp_webhook(request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else None
    if not verify_ip_allowed(client_ip):
        raise HTTPException(status_code=403, detail="IP not allowed")

    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    from app.config import settings

    if settings.whatsapp_app_secret and not verify_webhook_hmac_sha256(body, signature, settings.whatsapp_app_secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(body or b"{}")
    event_id = payload.get("entry", [{}])[0].get("id") or payload.get("object") or payload_hash(body)
    existing = db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == str(event_id)))
    if existing:
        return WebhookAck(status="accepted")

    db.add(WebhookEvent(event_id=str(event_id), source="whatsapp", payload_hash=payload_hash(body), status="pending"))
    db.commit()
    return WebhookAck(status="accepted")
