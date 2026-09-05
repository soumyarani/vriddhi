"""AI chat engine.

Two properties are load-bearing and tested hardest: a shopper always gets a
reply (the model failing is not the shopper's problem), and the model can never
settle a payment or move stock on its own.
"""

from __future__ import annotations

import json

import pytest

from app.models.enums import Intent, MessageType
from app.services import ai_chat
from app.services import conversation as conversation_service


@pytest.fixture
async def conversation(db, user):
    return await conversation_service.get_or_create_conversation(db, user.id)


# ---- Response parsing -----------------------------------------------------
def test_parses_plain_json():
    parsed = ai_chat._parse_response(
        json.dumps({"intent": "browse", "response_text": "Here you go", "message_type": "text"})
    )
    assert parsed.intent == Intent.BROWSE
    assert parsed.response_text == "Here you go"


def test_parses_json_wrapped_in_a_code_fence():
    raw = '```json\n{"intent": "help", "response_text": "Sure"}\n```'
    assert ai_chat._parse_response(raw).response_text == "Sure"


def test_parses_json_buried_in_prose():
    raw = 'Certainly! {"intent": "help", "response_text": "Hi"} Hope that helps.'
    assert ai_chat._parse_response(raw).response_text == "Hi"


def test_unknown_intent_degrades_to_help():
    """A hallucinated intent must not crash the pipeline."""
    parsed = ai_chat._parse_response(
        json.dumps({"intent": "launch_missiles", "response_text": "..."})
    )
    assert parsed.intent == Intent.HELP


def test_unknown_message_type_degrades_to_text():
    parsed = ai_chat._parse_response(
        json.dumps({"intent": "help", "message_type": "hologram", "response_text": "hi"})
    )
    assert parsed.message_type == MessageType.TEXT


def test_non_object_json_is_rejected():
    with pytest.raises(ValueError):
        ai_chat._parse_response("[1, 2, 3]")


# ---- Fallback behaviour ---------------------------------------------------
async def test_missing_api_key_still_answers(db, user, conversation, monkeypatch):
    monkeypatch.setattr(ai_chat.settings, "openai_api_key", "")
    reply = await ai_chat.generate_reply(db, user, conversation, "hello")
    assert reply.response_text


async def test_model_exception_still_answers(db, user, conversation, monkeypatch):
    """An OpenAI outage degrades the experience; it must not break it."""
    monkeypatch.setattr(ai_chat.settings, "openai_api_key", "sk-test")

    async def _boom(messages):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(ai_chat, "_call_model", _boom)
    reply = await ai_chat.generate_reply(db, user, conversation, "where is my order")
    assert reply.response_text


async def test_open_circuit_skips_the_model(db, user, conversation, monkeypatch):
    monkeypatch.setattr(ai_chat.settings, "openai_api_key", "sk-test")

    called = False

    async def _should_not_run(messages):
        nonlocal called
        called = True
        raise AssertionError("model must not be called while the circuit is open")

    monkeypatch.setattr(ai_chat, "_call_model", _should_not_run)
    monkeypatch.setattr(ai_chat, "circuit_is_open", lambda: _true())

    reply = await ai_chat.generate_reply(db, user, conversation, "hi")
    assert reply.response_text
    assert called is False


async def _true():
    return True


async def test_repeated_failures_open_the_circuit(monkeypatch, fake_redis):
    monkeypatch.setattr(ai_chat.settings, "ai_circuit_breaker_threshold", 3)

    assert await ai_chat.circuit_is_open() is False
    for _ in range(3):
        await ai_chat._record_failure()
    assert await ai_chat.circuit_is_open() is True


async def test_success_resets_the_failure_count(monkeypatch, fake_redis):
    monkeypatch.setattr(ai_chat.settings, "ai_circuit_breaker_threshold", 3)

    await ai_chat._record_failure()
    await ai_chat._record_success()
    await ai_chat._record_failure()
    await ai_chat._record_failure()

    # Only two consecutive failures since the reset — still closed.
    assert await ai_chat.circuit_is_open() is False


# ---- Intent fallback classification --------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("show me your products", Intent.BROWSE),
        ("what is in my cart", Intent.VIEW_CART),
        ("where is my order", Intent.TRACK_ORDER),
        ("i want to cancel", Intent.CANCEL_ORDER),
        # Nothing recognisable falls back to help rather than guessing.
        ("mmm ok whatever", Intent.HELP),
    ],
)
def test_scripted_intent_classification(text, expected):
    assert ai_chat.classify_fallback_intent(text) == expected


async def test_unserviceable_intent_escalates_to_a_human(db, user):
    """The scripted path answers lookups and hands judgement calls to a person.

    A return is exactly that: without the model there is nobody to weigh the
    request, so it must reach an agent rather than get a canned answer.
    """
    reply = await ai_chat._fallback(db, user, "I want to return this saree", reason="test")
    assert reply.escalate is True
    assert reply.intent == Intent.ESCALATE


async def test_serviceable_intent_is_answered_without_a_human(db, user):
    reply = await ai_chat._fallback(db, user, "what is in my cart", reason="test")
    assert reply.escalate is False
    assert reply.response_text


# ---- Token budget ---------------------------------------------------------
def test_budget_trims_catalogue_before_dropping_the_cart():
    """The cart and summary are the last things sacrificed."""
    blocks = {
        "summary": "Customer is buying a saree for a wedding.",
        "cart": {"items": [{"name": "Saree", "qty": 1}]},
        "catalog": [{"id": i, "name": f"Product {i}" * 40} for i in range(200)],
        "recent_turns": [{"role": "user", "content": "hello " * 200} for _ in range(20)],
        "orders": [],
    }
    fitted = ai_chat._fit_to_budget(dict(blocks))

    assert fitted["cart"] == blocks["cart"]
    assert fitted["summary"] == blocks["summary"]
    assert len(fitted["catalog"]) < len(blocks["catalog"])


def test_token_estimate_scales_with_length():
    assert ai_chat._estimate_tokens("a" * 400) > ai_chat._estimate_tokens("a" * 40)


# ---- The model must not move money ---------------------------------------
async def test_checkout_action_only_produces_a_payment_link(db, user, product, variant, address,
                                                            monkeypatch):
    """A model-initiated checkout may start a payment, never settle one."""
    from app.models.enums import OrderStatus
    from app.schemas.conversation import AIAction, AIResponse
    from app.services import cart as cart_service

    async def _fake_link(db_, order, user_):
        order.payment_link = "https://payments.test/link/xyz"
        return type("Link", (), {"url": order.payment_link, "id": "cf-1"})()

    monkeypatch.setattr("app.services.payment.create_payment_link", _fake_link)
    await cart_service.add_item(db, user.id, product.id, variant.id, 1)

    await ai_chat.execute_actions(
        db, user, AIResponse(actions=[AIAction(type="checkout")])
    )

    from sqlalchemy import select

    from app.models.order import Order

    orders = (await db.execute(select(Order))).scalars().all()
    # An order may now exist, but it must still be awaiting payment: the model
    # can open a checkout, never close one.
    for order in orders:
        assert order.status == OrderStatus.PENDING_PAYMENT


async def test_unknown_action_is_a_no_op(db, user):
    """An invented action name must do nothing rather than raise or guess."""
    from app.schemas.conversation import AIAction, AIResponse

    results = await ai_chat.execute_actions(
        db, user, AIResponse(actions=[AIAction(type="delete_everything")])
    )
    assert results[0]["noop"] is True
