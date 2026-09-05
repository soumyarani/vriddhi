"""Meta Cloud API outbound sender and inbound webhook parser.

Every sender returns Meta's parsed JSON and raises UpstreamError on failure so
the arq worker can retry. Parsing is pure so the webhook router can normalise a
payload before touching the database.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.config import settings
from app.errors import UpstreamError, ValidationError
from app.models.enums import MessageType
from logging_config import get_logger

log = get_logger(__name__)

WHATSAPP_TEXT_LIMIT = 4096
MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20
MAX_LIST_ROWS = 10
MAX_LIST_ROW_TITLE = 24
MAX_LIST_ROW_DESCRIPTION = 72
MAX_SECTION_TITLE = 24
MAX_HEADER = 60
MAX_CAPTION = 1024
MAX_PRODUCTS = 30
MAX_REPLY_ID = 256

_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
_NON_DIGITS = re.compile(r"\D")
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def _normalise_phone(to: str) -> str:
    digits = _NON_DIGITS.sub("", to or "")
    if not digits:
        raise ValidationError("Invalid recipient phone number")
    return digits


def _slug_id(value: str, index: int) -> str:
    slug = _NON_SLUG.sub("_", value.strip().lower()).strip("_")
    return _truncate(slug or f"opt_{index}", MAX_REPLY_ID)


def _envelope(to: str, **fields: Any) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _normalise_phone(to),
        **fields,
    }


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        parsed = response.json()
    except ValueError:
        return {"raw": response.text[:500]}
    return parsed if isinstance(parsed, dict) else {"raw": parsed}


async def _post(payload: dict[str, Any]) -> dict[str, Any]:
    if not settings.whatsapp_token or not settings.whatsapp_phone_number_id:
        log.warning("whatsapp_send_skipped", reason="missing_credentials", type=payload.get("type"))
        return {"skipped": True}

    url = f"{settings.graph_base}/{settings.whatsapp_phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("whatsapp_send_failed", type=payload.get("type"), error=str(exc))
        raise UpstreamError("WhatsApp request failed", detail=str(exc)) from exc

    body = _safe_json(response)
    if response.status_code >= 400:
        meta_error = body.get("error", {}) if isinstance(body.get("error"), dict) else {}
        log.warning(
            "whatsapp_send_rejected",
            status=response.status_code,
            type=payload.get("type"),
            meta_code=meta_error.get("code"),
            meta_message=meta_error.get("message"),
        )
        raise UpstreamError(
            meta_error.get("message") or "WhatsApp send failed",
            detail={"status": response.status_code, "body": body},
        )

    log.info(
        "whatsapp_sent",
        type=payload.get("type"),
        wa_message_id=(body.get("messages") or [{}])[0].get("id"),
    )
    return body


async def send_text(to: str, body: str, preview_url: bool = False) -> dict:
    payload = _envelope(
        to,
        type="text",
        text={"body": _truncate(body, WHATSAPP_TEXT_LIMIT), "preview_url": preview_url},
    )
    return await _post(payload)


async def send_buttons(
    to: str, body: str, buttons: list[str], header: str | None = None
) -> dict:
    titles = [b for b in buttons if b and b.strip()]
    if len(titles) > MAX_BUTTONS:
        log.warning("whatsapp_buttons_truncated", requested=len(titles), sent=MAX_BUTTONS)
        titles = titles[:MAX_BUTTONS]
    if not titles:
        raise ValidationError("At least one button is required")

    interactive: dict[str, Any] = {
        "type": "button",
        "body": {"text": _truncate(body, WHATSAPP_TEXT_LIMIT)},
        "action": {
            "buttons": [
                {
                    "type": "reply",
                    "reply": {
                        "id": _slug_id(title, index),
                        "title": _truncate(title.strip(), MAX_BUTTON_TITLE),
                    },
                }
                for index, title in enumerate(titles)
            ]
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": _truncate(header, MAX_HEADER)}

    return await _post(_envelope(to, type="interactive", interactive=interactive))


async def send_list(
    to: str,
    body: str,
    button_text: str,
    sections: list[dict],
    header: str | None = None,
) -> dict:
    normalised: list[dict[str, Any]] = []
    remaining = MAX_LIST_ROWS
    dropped = 0

    for index, section in enumerate(sections):
        rows = section.get("rows") or []
        if remaining <= 0:
            dropped += len(rows)
            continue
        kept = rows[:remaining]
        dropped += len(rows) - len(kept)
        remaining -= len(kept)
        normalised.append(
            {
                "title": _truncate(str(section.get("title") or f"Options {index + 1}"), MAX_SECTION_TITLE),
                "rows": [
                    {
                        "id": _truncate(str(row.get("id") or _slug_id(str(row.get("title", "")), i)), MAX_REPLY_ID),
                        "title": _truncate(str(row.get("title", "")), MAX_LIST_ROW_TITLE),
                        **(
                            {"description": _truncate(str(row["description"]), MAX_LIST_ROW_DESCRIPTION)}
                            if row.get("description")
                            else {}
                        ),
                    }
                    for i, row in enumerate(kept)
                ],
            }
        )

    if dropped:
        log.warning("whatsapp_list_rows_truncated", dropped=dropped, limit=MAX_LIST_ROWS)
    if not normalised:
        raise ValidationError("List message requires at least one row")

    interactive: dict[str, Any] = {
        "type": "list",
        "body": {"text": _truncate(body, WHATSAPP_TEXT_LIMIT)},
        "action": {
            "button": _truncate(button_text, MAX_BUTTON_TITLE),
            "sections": normalised,
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": _truncate(header, MAX_HEADER)}

    return await _post(_envelope(to, type="interactive", interactive=interactive))


async def send_product(to: str, product_retailer_id: str, body: str | None = None) -> dict:
    if not settings.whatsapp_catalog_id:
        log.warning("whatsapp_product_send_skipped", reason="missing_catalog_id")
        return {"skipped": True}

    interactive: dict[str, Any] = {
        "type": "product",
        "action": {
            "catalog_id": settings.whatsapp_catalog_id,
            "product_retailer_id": product_retailer_id,
        },
    }
    if body:
        interactive["body"] = {"text": _truncate(body, WHATSAPP_TEXT_LIMIT)}

    return await _post(_envelope(to, type="interactive", interactive=interactive))


async def send_product_list(to: str, header: str, body: str, sections: list[dict]) -> dict:
    if not settings.whatsapp_catalog_id:
        log.warning("whatsapp_product_list_skipped", reason="missing_catalog_id")
        return {"skipped": True}

    normalised: list[dict[str, Any]] = []
    remaining = MAX_PRODUCTS
    dropped = 0

    for index, section in enumerate(sections):
        items = section.get("product_items") or []
        if remaining <= 0:
            dropped += len(items)
            continue
        kept = items[:remaining]
        dropped += len(items) - len(kept)
        remaining -= len(kept)
        normalised.append(
            {
                "title": _truncate(str(section.get("title") or f"Products {index + 1}"), MAX_SECTION_TITLE),
                "product_items": [
                    {"product_retailer_id": str(item["product_retailer_id"])}
                    for item in kept
                    if item.get("product_retailer_id")
                ],
            }
        )

    if dropped:
        log.warning("whatsapp_product_list_truncated", dropped=dropped, limit=MAX_PRODUCTS)

    normalised = [section for section in normalised if section["product_items"]]
    if not normalised:
        raise ValidationError("Product list requires at least one product")

    interactive = {
        "type": "product_list",
        "header": {"type": "text", "text": _truncate(header, MAX_HEADER)},
        "body": {"text": _truncate(body, WHATSAPP_TEXT_LIMIT)},
        "action": {
            "catalog_id": settings.whatsapp_catalog_id,
            "sections": normalised,
        },
    }
    return await _post(_envelope(to, type="interactive", interactive=interactive))


async def send_image(to: str, image_url: str, caption: str | None = None) -> dict:
    image: dict[str, Any] = {"link": image_url}
    if caption:
        image["caption"] = _truncate(caption, MAX_CAPTION)
    return await _post(_envelope(to, type="image", image=image))


async def mark_read(wa_message_id: str) -> None:
    # Best effort: a failed read receipt must not abort message processing.
    try:
        await _post(
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": wa_message_id,
            }
        )
    except UpstreamError as exc:
        log.warning("whatsapp_mark_read_failed", wa_message_id=wa_message_id, error=str(exc))


def _epoch(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _order_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    order = message.get("order") or {}
    items = []
    for item in order.get("product_items") or []:
        items.append(
            {
                "product_retailer_id": item.get("product_retailer_id"),
                "quantity": _epoch(item.get("quantity")) or 0,
                "item_price": _decimal(item.get("item_price")),
                "currency": item.get("currency"),
            }
        )
    return items


def _interactive_reply(message: dict[str, Any]) -> tuple[str | None, str | None, str]:
    """Returns (reply_id, reply_title, message_type)."""
    interactive = message.get("interactive") or {}
    kind = interactive.get("type")
    if kind == "button_reply":
        reply = interactive.get("button_reply") or {}
        return reply.get("id"), reply.get("title"), MessageType.BUTTONS.value
    if kind == "list_reply":
        reply = interactive.get("list_reply") or {}
        return reply.get("id"), reply.get("title"), MessageType.LIST.value
    return None, None, "interactive"


def parse_incoming(payload: dict) -> list[dict]:
    parsed: list[dict[str, Any]] = []

    for entry in (payload or {}).get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            messages = value.get("messages") or []
            if not messages:
                continue

            profiles = {
                contact.get("wa_id"): (contact.get("profile") or {}).get("name")
                for contact in value.get("contacts") or []
            }

            for message in messages:
                raw_type = message.get("type") or "unknown"
                from_phone = message.get("from")
                text: str | None = None
                reply_id: str | None = None
                reply_title: str | None = None
                order_items: list[dict[str, Any]] = []
                message_type = raw_type

                if raw_type == "text":
                    text = (message.get("text") or {}).get("body")
                    message_type = MessageType.TEXT.value
                elif raw_type == "interactive":
                    reply_id, reply_title, message_type = _interactive_reply(message)
                    text = reply_title
                elif raw_type == "button":
                    # Template quick-replies arrive as type "button", not "interactive".
                    button = message.get("button") or {}
                    reply_id = button.get("payload")
                    reply_title = button.get("text")
                    text = reply_title
                    message_type = MessageType.BUTTONS.value
                elif raw_type == "order":
                    order_items = _order_items(message)
                    text = (message.get("order") or {}).get("text")
                elif raw_type == "image":
                    text = (message.get("image") or {}).get("caption")
                    message_type = MessageType.IMAGE.value

                parsed.append(
                    {
                        "wa_message_id": message.get("id"),
                        "from_phone": from_phone,
                        "type": message_type,
                        "text": text,
                        "interactive_reply_id": reply_id,
                        "interactive_reply_title": reply_title,
                        "timestamp": _epoch(message.get("timestamp")),
                        "profile_name": profiles.get(from_phone),
                        "order_items": order_items,
                    }
                )

    return parsed


def parse_statuses(payload: dict) -> list[dict]:
    statuses: list[dict[str, Any]] = []

    for entry in (payload or {}).get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for status in value.get("statuses") or []:
                statuses.append(
                    {
                        "wa_message_id": status.get("id"),
                        "status": status.get("status"),
                        "recipient": status.get("recipient_id"),
                    }
                )

    return statuses
