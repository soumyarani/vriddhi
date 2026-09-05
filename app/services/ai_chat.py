"""The AI shopping assistant.

Three concerns live here, in order of importance:

1. **Never leave a shopper unanswered.** Every path — model down, malformed
   JSON, circuit open, no API key — ends in a reply. `_fallback` handles the
   intents we can serve from the database alone and escalates the rest.
2. **Stay inside the token budget.** The prompt is assembled from a compact
   catalogue, the live cart, open orders and a rolling summary, then trimmed to
   `ai_max_context_tokens`. Older turns are folded into a one-line summary
   rather than being carried forever.
3. **Never let the model move money.** The model proposes actions; this module
   executes them against the real services, which re-check stock, price and
   ownership. `checkout` in particular only ever returns a payment link — it
   cannot confirm or settle anything.

A circuit breaker trips after `ai_circuit_breaker_threshold` consecutive
failures and forces fallback mode for `ai_circuit_breaker_cooldown_seconds`, so
an upstream outage costs one timeout rather than one per shopper.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import CacheKeys, cache_delete, cache_get, cache_set
from app.config import settings
from app.errors import AppError
from app.models.conversation import AITokenUsage, Conversation
from app.models.enums import FALLBACK_SERVICEABLE_INTENTS, Intent, MessageType
from app.models.user import User
from app.schemas.conversation import AIAction, AIResponse
from logging_config import get_logger

log = get_logger(__name__)

# A token is ~4 characters of English. Good enough for budgeting; we are
# trimming a prompt, not billing anyone.
CHARS_PER_TOKEN = 4
MAX_CATALOG_PRODUCTS = 40
SUMMARY_TRIGGER_TURNS = 6

GREETING = (
    "Hi! I can help you browse our catalogue, track an order, or check your cart. "
    "What would you like to do?"
)

SYSTEM_PROMPT = """You are a friendly shopping assistant for {store} on WhatsApp.
You help customers browse products, manage their cart, check out, and track orders.

Rules:
- Reply ONLY with a single JSON object. No markdown, no code fences, no prose outside it.
- Keep response_text under 600 characters. WhatsApp is a chat, not a webpage.
- Prices are in Indian Rupees and already include GST. Never quote a price that is not in the catalogue below.
- Only reference product_id and variant_id values that appear in the catalogue. Never invent one.
- If the customer wants a human, is angry, or asks something you cannot do, set escalate to true.
- Never promise a delivery date, a refund, or a discount that is not in the data you were given.
- If a request is ambiguous (e.g. a product with several sizes), ask one short clarifying question.

Respond with this exact JSON shape:
{{
  "intent": "one of: browse, search, add_to_cart, remove_from_cart, view_cart, checkout, track_order, order_history, cancel_order, return_order, help, escalate",
  "response_text": "what to say to the customer",
  "actions": [{{"type": "add_to_cart", "product_id": 1, "variant_id": 2, "quantity": 1}}],
  "message_type": "text | buttons | list | product | product_list",
  "buttons": ["max 3, max 20 chars each"],
  "product_ids": [],
  "list_items": [{{"id": "opt_1", "title": "max 24 chars", "description": "max 72 chars"}}],
  "escalate": false,
  "header": null
}}

Action types you may emit: add_to_cart, remove_from_cart, view_cart, checkout,
track_order, order_history, cancel_order, return_order, search, browse.
Emit an empty actions list when you are only talking."""


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------
async def circuit_is_open() -> bool:
    return bool(await cache_get(CacheKeys.AI_CIRCUIT))


async def _record_failure() -> None:
    failures = int(await cache_get(CacheKeys.AI_FAILURES) or 0) + 1
    await cache_set(CacheKeys.AI_FAILURES, failures, 300)

    if failures >= settings.ai_circuit_breaker_threshold:
        await cache_set(
            CacheKeys.AI_CIRCUIT, True, settings.ai_circuit_breaker_cooldown_seconds
        )
        log.error("ai_circuit_opened", failures=failures)


async def _record_success() -> None:
    await cache_delete(CacheKeys.AI_FAILURES)


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------
def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


async def build_context_blocks(
    db: AsyncSession, user: User, conversation: Conversation
) -> dict[str, Any]:
    """Everything the model is allowed to know, as compact JSON-able blocks."""
    from app.services import cart as cart_service
    from app.services import order as order_service
    from app.services import product as product_service

    catalog = await product_service.build_ai_catalog_context(
        db, max_products=MAX_CATALOG_PRODUCTS
    )

    cart_block: dict[str, Any] | None = None
    active_cart = await cart_service.get_active_cart(db, user.id)
    if active_cart is not None and active_cart.items:
        cart_block = {
            "items": [
                {
                    "cart_item_id": item.id,
                    "product_id": item.product_id,
                    "variant_id": item.variant_id,
                    "name": item.variant.product.name if item.variant else None,
                    "qty": item.quantity,
                }
                for item in active_cart.items
            ]
        }

    orders = await order_service.active_orders_for_ai(db, user.id, limit=3)
    context = conversation.context or {}

    return {
        "catalog": catalog,
        "cart": cart_block,
        "orders": orders,
        "summary": context.get("summary") or "",
        "recent": context.get("recent") or [],
        "customer": {"name": user.name, "has_address": bool(user.addresses)},
    }


def _fit_to_budget(blocks: dict[str, Any]) -> dict[str, Any]:
    """Drop the least valuable blocks until the prompt fits the token budget.

    Order of sacrifice: catalogue size first (the model can still search), then
    older turns, then open orders. The summary and the cart always survive —
    losing those makes the assistant visibly forgetful.
    """
    budget = settings.ai_max_context_tokens
    blocks = {**blocks}

    def size() -> int:
        return _estimate_tokens(json.dumps(blocks, default=str))

    catalog = list(blocks.get("catalog") or [])
    while size() > budget and len(catalog) > 5:
        catalog = catalog[: max(5, len(catalog) // 2)]
        blocks["catalog"] = catalog

    recent = list(blocks.get("recent") or [])
    while size() > budget and recent:
        recent.pop(0)
        blocks["recent"] = recent

    if size() > budget:
        blocks["orders"] = (blocks.get("orders") or [])[:1]

    return blocks


def _build_messages(blocks: dict[str, Any], user_text: str) -> list[dict[str, str]]:
    system = SYSTEM_PROMPT.format(store=settings.app_name)

    context_lines = [f"CATALOGUE: {json.dumps(blocks['catalog'], default=str)}"]
    if blocks.get("cart"):
        context_lines.append(f"CURRENT CART: {json.dumps(blocks['cart'], default=str)}")
    else:
        context_lines.append("CURRENT CART: empty")
    if blocks.get("orders"):
        context_lines.append(f"OPEN ORDERS: {json.dumps(blocks['orders'], default=str)}")
    if blocks.get("summary"):
        context_lines.append(f"CONVERSATION SO FAR: {blocks['summary']}")

    messages = [
        {"role": "system", "content": system},
        {"role": "system", "content": "\n".join(context_lines)},
    ]
    for turn in blocks.get("recent") or []:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        messages.append({"role": role, "content": str(turn.get("text") or "")})

    messages.append({"role": "user", "content": user_text})
    return messages


# --------------------------------------------------------------------------
# Model call
# --------------------------------------------------------------------------
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_response(raw: str) -> AIResponse:
    """Parse the model's JSON, tolerating code fences and stray prose."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(text)
        if not match:
            raise
        data = json.loads(match.group(0))

    if not isinstance(data, dict):
        raise ValueError("AI response was not a JSON object")

    # An unknown intent is a model error, not a crash — fall back to help.
    if data.get("intent") not in set(Intent):
        data["intent"] = Intent.HELP
    if data.get("message_type") not in set(MessageType):
        data["message_type"] = MessageType.TEXT

    return AIResponse.model_validate(data)


async def _call_model(messages: list[dict[str, str]]) -> tuple[AIResponse, dict[str, int]]:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=settings.openai_api_key, timeout=settings.openai_timeout_seconds
    )
    completion = await client.chat.completions.create(
        model=settings.openai_model,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.3,
        max_tokens=800,
    )

    usage = completion.usage
    counts = {
        "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
    }
    return _parse_response(completion.choices[0].message.content or ""), counts


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
async def generate_reply(
    db: AsyncSession, user: User, conversation: Conversation, user_text: str
) -> AIResponse:
    """Produce a reply for one inbound message. Never raises."""
    started = time.monotonic()

    if not settings.openai_api_key:
        return await _fallback(db, user, user_text, reason="no_api_key")
    if await circuit_is_open():
        log.warning("ai_circuit_open_fallback", user_id=user.id)
        return await _fallback(db, user, user_text, reason="circuit_open")

    try:
        blocks = await build_context_blocks(db, user, conversation)
        messages = _build_messages(_fit_to_budget(blocks), user_text)
        response, counts = await _call_model(messages)
    except Exception as exc:
        await _record_failure()
        log.warning("ai_call_failed", user_id=user.id, error=str(exc))
        return await _fallback(db, user, user_text, reason="model_error")

    await _record_success()
    await _track_usage(
        db,
        user_id=user.id,
        conversation_id=conversation.id,
        counts=counts,
        latency_ms=int((time.monotonic() - started) * 1000),
        fallback=False,
    )
    return response


async def _track_usage(
    db: AsyncSession,
    user_id: int | None,
    conversation_id: int | None,
    counts: dict[str, int],
    latency_ms: int,
    fallback: bool,
) -> None:
    db.add(
        AITokenUsage(
            user_id=user_id,
            conversation_id=conversation_id,
            model=settings.openai_model,
            input_tokens=counts.get("input_tokens", 0),
            output_tokens=counts.get("output_tokens", 0),
            latency_ms=latency_ms,
            fallback_used=fallback,
        )
    )
    await db.flush()


# --------------------------------------------------------------------------
# Fallback — scripted, database-only replies
# --------------------------------------------------------------------------
_KEYWORDS: list[tuple[Intent, tuple[str, ...]]] = [
    (Intent.VIEW_CART, ("cart", "basket", "bag")),
    (Intent.TRACK_ORDER, ("track", "where is", "status", "delivery", "shipped")),
    (Intent.ORDER_HISTORY, ("my orders", "order history", "past order", "previous order")),
    (Intent.CHECKOUT, ("checkout", "pay", "buy now", "place order")),
    (Intent.CANCEL_ORDER, ("cancel",)),
    (Intent.RETURN_ORDER, ("return", "refund", "exchange")),
    (Intent.BROWSE, ("browse", "show", "catalog", "catalogue", "products", "categories")),
    (Intent.HELP, ("help", "hi", "hello", "hey", "start", "menu")),
]


def classify_fallback_intent(text: str) -> Intent:
    lowered = (text or "").lower()
    for intent, keywords in _KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return intent
    return Intent.HELP


async def _fallback(
    db: AsyncSession, user: User, user_text: str, reason: str
) -> AIResponse:
    """Serve what we can from the database; escalate anything that needs judgement."""
    intent = classify_fallback_intent(user_text)
    log.info("ai_fallback", user_id=user.id, intent=intent, reason=reason)

    if intent not in FALLBACK_SERVICEABLE_INTENTS:
        return AIResponse(
            intent=Intent.ESCALATE,
            response_text=(
                "Let me get a team member to help you with that — "
                "someone will reply here shortly."
            ),
            escalate=True,
        )

    from app.services import cart as cart_service
    from app.services import order as order_service
    from app.services import product as product_service

    if intent == Intent.VIEW_CART:
        cart = await cart_service.get_active_cart(db, user.id)
        if cart is None or not cart.items:
            return AIResponse(
                intent=intent,
                response_text="Your cart is empty. Want to see what's in stock?",
                message_type=MessageType.BUTTONS,
                buttons=["Browse products", "Track order"],
            )
        priced = await cart_service.price_cart(db, cart)
        lines = "\n".join(
            f"- {i['name']} x{i['quantity']} — Rs.{i['line_total']}"
            for i in priced.get("items", [])[:10]
        )
        return AIResponse(
            intent=intent,
            response_text=f"Your cart:\n{lines}\n\nTotal: Rs.{priced.get('total')}",
            message_type=MessageType.BUTTONS,
            buttons=["Checkout", "Browse products"],
        )

    if intent in (Intent.TRACK_ORDER, Intent.ORDER_HISTORY):
        orders = await order_service.active_orders_for_ai(db, user.id, limit=3)
        if not orders:
            return AIResponse(
                intent=intent,
                response_text="I couldn't find any recent orders on this number.",
                message_type=MessageType.BUTTONS,
                buttons=["Browse products"],
            )
        lines = "\n".join(
            f"- {o.get('order_number')}: {str(o.get('status', '')).replace('_', ' ')}"
            for o in orders
        )
        return AIResponse(intent=intent, response_text=f"Your recent orders:\n{lines}")

    if intent == Intent.BROWSE:
        categories = await product_service.list_categories(db)
        if categories:
            return AIResponse(
                intent=intent,
                response_text="Here's what we stock. Pick a category to see items.",
                message_type=MessageType.LIST,
                header="Browse",
                list_items=[
                    {
                        "id": f"category_{c['id']}",
                        "title": str(c["name"])[:24],
                        "description": str(c.get("description") or "")[:72],
                    }
                    for c in categories[:10]
                ],
            )

    return AIResponse(
        intent=Intent.HELP,
        response_text=GREETING,
        message_type=MessageType.BUTTONS,
        buttons=["Browse products", "View cart", "Track order"],
    )


# --------------------------------------------------------------------------
# Action execution
# --------------------------------------------------------------------------
async def execute_actions(
    db: AsyncSession, user: User, response: AIResponse
) -> list[dict[str, Any]]:
    """Run the model's proposed actions against the real services.

    Each action is independently guarded: a failure becomes a message the
    shopper can act on, not an exception that swallows the whole reply.
    """
    results: list[dict[str, Any]] = []
    for action in response.actions:
        try:
            results.append(await _execute_one(db, user, action))
        except AppError as exc:
            log.info("ai_action_rejected", type=action.type, error=exc.message)
            results.append({"type": action.type, "ok": False, "message": exc.message})
        except Exception as exc:
            log.warning("ai_action_failed", type=action.type, error=str(exc))
            results.append(
                {
                    "type": action.type,
                    "ok": False,
                    "message": "Something went wrong with that — let me get someone to help.",
                    "escalate": True,
                }
            )
    return results


async def _execute_one(db: AsyncSession, user: User, action: AIAction) -> dict[str, Any]:
    from app.services import cart as cart_service
    from app.services import order as order_service

    kind = (action.type or "").strip().lower()

    if kind == "add_to_cart":
        if not action.product_id or not action.variant_id:
            return {"type": kind, "ok": False, "message": "Which size or colour would you like?"}
        cart = await cart_service.add_item(
            db,
            user_id=user.id,
            product_id=action.product_id,
            variant_id=action.variant_id,
            quantity=action.quantity or 1,
        )
        return {"type": kind, "ok": True, "cart_items": len(cart.items)}

    if kind == "remove_from_cart":
        if not action.cart_item_id:
            return {"type": kind, "ok": False, "message": "Which item should I remove?"}
        cart = await cart_service.remove_item(db, user.id, action.cart_item_id)
        return {"type": kind, "ok": True, "cart_items": len(cart.items)}

    if kind == "view_cart":
        cart = await cart_service.get_active_cart(db, user.id)
        if cart is None or not cart.items:
            return {"type": kind, "ok": True, "cart": None}
        return {"type": kind, "ok": True, "cart": await cart_service.serialize_cart(db, cart)}

    if kind == "checkout":
        # Deliberately produces a payment link only. The model can start a
        # checkout; it can never settle one.
        order, payment = await order_service.checkout(db, user, channel="whatsapp")
        return {
            "type": kind,
            "ok": True,
            "order_number": order.order_number,
            "payment_link": getattr(payment, "payment_link", None),
            "total": str(order.total),
        }

    if kind in ("track_order", "order_history"):
        orders = await order_service.active_orders_for_ai(db, user.id, limit=5)
        return {"type": kind, "ok": True, "orders": orders}

    if kind == "cancel_order":
        order = await _resolve_order(db, user, action)
        await order_service.cancel_order(db, order, reason="Cancelled from WhatsApp chat")
        return {"type": kind, "ok": True, "order_number": order.order_number}

    if kind == "return_order":
        order = await _resolve_order(db, user, action)
        await order_service.request_return(
            db, order, reason=action.reason or "Requested from WhatsApp chat"
        )
        return {"type": kind, "ok": True, "order_number": order.order_number}

    return {"type": kind, "ok": True, "noop": True}


async def _resolve_order(db: AsyncSession, user: User, action: AIAction):
    from app.errors import NotFoundError
    from app.services import order as order_service

    if action.order_id:
        return await order_service.get_user_order(db, user.id, action.order_id)
    if action.order_number:
        order = await order_service.get_order_by_number(db, action.order_number)
        # Same error either way, so order numbers can't be probed.
        if order is None or order.user_id != user.id:
            raise NotFoundError("Order not found")
        return order
    raise NotFoundError("Which order did you mean?")


# --------------------------------------------------------------------------
# Summarisation
# --------------------------------------------------------------------------
async def maybe_summarize(
    db: AsyncSession, conversation: Conversation
) -> str | None:
    """Fold older turns into a one-line summary once the thread gets long.

    Returns the new summary, or None if nothing needed doing.
    """
    context = conversation.context or {}
    recent = context.get("recent") or []
    if len(recent) < SUMMARY_TRIGGER_TURNS or not settings.openai_api_key:
        return None
    if await circuit_is_open():
        return None

    transcript = "\n".join(f"{t.get('role')}: {t.get('text')}" for t in recent)
    prompt = (
        "Summarise this shopping conversation in at most two sentences. "
        "Keep product names, sizes, quantities and any order number. "
        "Drop greetings and small talk.\n\n"
        f"Previous summary: {context.get('summary') or 'none'}\n\n{transcript}"
    )

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=settings.openai_api_key, timeout=settings.openai_timeout_seconds
        )
        completion = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=settings.ai_summary_token_budget,
            temperature=0.2,
        )
    except Exception as exc:
        # A failed summary is not worth failing a reply over.
        log.warning("ai_summary_failed", conversation_id=conversation.id, error=str(exc))
        return None

    summary = (completion.choices[0].message.content or "").strip()
    if not summary:
        return None

    from app.services.conversation import update_context

    # Older turns are now represented by the summary, so drop all but the last pair.
    context = dict(conversation.context or {})
    context["recent"] = recent[-2:]
    conversation.context = context
    await update_context(db, conversation, summary=summary)

    log.info("ai_context_summarized", conversation_id=conversation.id)
    return summary
