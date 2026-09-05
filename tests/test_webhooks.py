"""Webhook security: signature verification, idempotency, and fast ACK.

These are the endpoints an attacker can reach without a token, so the tests
lean on the negative cases.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from base64 import b64encode

import pytest

from app.config import settings
from app.security import payload_hash, verify_cashfree_signature, verify_meta_signature


def meta_sig(body: bytes) -> str:
    digest = hmac.new(settings.whatsapp_app_secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def cashfree_sig(body: bytes, timestamp: str) -> str:
    digest = hmac.new(
        settings.cashfree_webhook_secret.encode(), timestamp.encode() + body, hashlib.sha256
    ).digest()
    return b64encode(digest).decode()


def wa_payload(wa_message_id: str = "wamid.TEST1") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1234567890"},
                            "contacts": [{"profile": {"name": "Asha"}, "wa_id": "919876543210"}],
                            "messages": [
                                {
                                    "from": "919876543210",
                                    "id": wa_message_id,
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": "hi"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


# ---- Signature primitives -------------------------------------------------
def test_meta_signature_accepts_a_correct_digest():
    body = b'{"hello":"world"}'
    assert verify_meta_signature(body, meta_sig(body)) is True


def test_meta_signature_rejects_a_tampered_body():
    body = b'{"hello":"world"}'
    signature = meta_sig(body)
    assert verify_meta_signature(b'{"hello":"evil"}', signature) is False


def test_meta_signature_rejects_missing_and_malformed_headers():
    body = b"{}"
    assert verify_meta_signature(body, None) is False
    assert verify_meta_signature(body, "deadbeef") is False
    assert verify_meta_signature(body, "sha256=") is False


def test_cashfree_signature_is_timestamp_bound():
    body = b'{"type":"PAYMENT_SUCCESS_WEBHOOK"}'
    assert verify_cashfree_signature(body, cashfree_sig(body, "111"), "111") is True
    # Same body, different timestamp — a replayed signature must not verify.
    assert verify_cashfree_signature(body, cashfree_sig(body, "111"), "222") is False


def test_cashfree_signature_requires_both_parts():
    body = b"{}"
    assert verify_cashfree_signature(body, None, "111") is False
    assert verify_cashfree_signature(body, "abc", None) is False


def test_payload_hash_is_stable_and_distinguishing():
    assert payload_hash(b"a") == payload_hash(b"a")
    assert payload_hash(b"a") != payload_hash(b"b")


# ---- Meta verification handshake -----------------------------------------
async def test_verification_challenge_is_echoed(client):
    resp = await client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": settings.whatsapp_verify_token,
            "hub.challenge": "42",
        },
    )
    assert resp.status_code == 200
    assert resp.text.strip('"') == "42"


async def test_verification_rejects_a_wrong_token(client):
    resp = await client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "42"},
    )
    assert resp.status_code == 403


# ---- Inbound message webhook ---------------------------------------------
async def test_unsigned_webhook_is_rejected(client):
    body = json.dumps(wa_payload()).encode()
    resp = await client.post(
        "/webhook", content=body, headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 401


async def test_signed_webhook_is_accepted_and_queued(client, captured_jobs):
    body = json.dumps(wa_payload()).encode()
    resp = await client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": meta_sig(body)},
    )
    assert resp.status_code == 200
    assert [j[0] for j in captured_jobs] == ["process_whatsapp_message"]


async def test_duplicate_delivery_is_not_processed_twice(client, captured_jobs):
    """Meta retries aggressively; the second delivery must ACK without re-queueing."""
    body = json.dumps(wa_payload("wamid.DUPE")).encode()
    headers = {"Content-Type": "application/json", "X-Hub-Signature-256": meta_sig(body)}

    first = await client.post("/webhook", content=body, headers=headers)
    second = await client.post("/webhook", content=body, headers=headers)

    assert first.status_code == 200 and second.status_code == 200
    assert len(captured_jobs) == 1


async def test_webhook_records_an_event_row(client, db):
    from sqlalchemy import select

    from app.models.webhook_event import WebhookEvent

    body = json.dumps(wa_payload("wamid.STORED")).encode()
    await client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": meta_sig(body)},
    )

    rows = (await db.execute(select(WebhookEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].payload_hash == payload_hash(body)


async def test_unrecognised_meta_event_still_acks(client):
    """An unknown change type must not 500 — Meta would retry it forever."""
    body = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()
    resp = await client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": meta_sig(body)},
    )
    assert resp.status_code == 200


# ---- Cashfree payment webhook --------------------------------------------
async def test_payment_webhook_rejects_a_bad_signature(client):
    body = json.dumps({"type": "PAYMENT_SUCCESS_WEBHOOK", "data": {}}).encode()
    resp = await client.post(
        "/webhook/cashfree",
        content=body,
        headers={
            "Content-Type": "application/json",
            "x-webhook-signature": "not-a-signature",
            "x-webhook-timestamp": "111",
        },
    )
    assert resp.status_code == 401


async def test_payment_webhook_queues_on_valid_signature(client, captured_jobs):
    payload = {
        "type": "PAYMENT_SUCCESS_WEBHOOK",
        "data": {
            "order": {"order_id": "ORD-2026-00001"},
            "payment": {"cf_payment_id": "cf-1", "payment_status": "SUCCESS",
                        "payment_amount": 4999.0},
        },
    }
    body = json.dumps(payload).encode()
    resp = await client.post(
        "/webhook/cashfree",
        content=body,
        headers={
            "Content-Type": "application/json",
            "x-webhook-signature": cashfree_sig(body, "111"),
            "x-webhook-timestamp": "111",
        },
    )
    assert resp.status_code == 200
    assert [j[0] for j in captured_jobs] == ["process_payment_event"]
