"""Staff-facing API.

Two authorisation tiers, taken from `app.dependencies`:

* `CurrentAgent` — any active staff member. Day-to-day work: reading the
  catalogue, working the conversation queue, moving orders through
  fulfilment.
* `CurrentAdmin` — admins only. Anything that changes configuration
  (products, coupons, shipping rates), destroys data (soft deletes, account
  merges), moves money (refunds) or exposes business performance
  (dashboard revenue).

Handlers stay thin: they translate HTTP into a service call and back. They
raise the domain exceptions from `app.errors` (never `HTTPException`) because
the same service functions run inside arq workers, where an HTTP exception
would be meaningless. `main.py` installs the handler that renders them.

Services `flush()` but never `commit()` — the unit of work belongs to the
request — so every mutating handler commits explicitly before serialising its
response.
"""

# NOTE: no `from __future__ import annotations` here, unlike the rest of the
# package. `limiter.limit` wraps each handler with functools.wraps, which copies
# `__annotations__` but not `__globals__`; FastAPI would then try to resolve the
# string annotations against slowapi's module namespace and treat `CurrentAgent`
# and friends as undefined query parameters.
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Body, Query, Request, status
from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.dependencies import CurrentAdmin, CurrentAgent, DbSession, PageLimit
from app.errors import NotFoundError, PermissionError_, UpstreamError, ValidationError
from app.redis import enqueue
from app.models.base import utcnow
from app.models.cart import Cart
from app.models.conversation import AITokenUsage, Conversation
from app.models.coupon import Coupon, CouponUsage
from app.models.enums import (
    AgentRole,
    CartStatus,
    ConversationStatus,
    EmailType,
    OrderStatus,
)
from app.models.order import Order, OrderItem
from app.models.product import Product, ProductVariant
from app.models.review import Review
from app.models.user import User
from app.pagination import apply_cursor, encode_cursor
from app.rate_limit import limiter
from app.schemas.common import STAFF_RESPONSES, MessageResponse, Page
from app.schemas.conversation import (
    AgentReplyRequest,
    AssignConversationRequest,
    ConversationDetail,
    ConversationSummary,
    MessageOut,
    NotificationOut,
)
from app.schemas.coupon import CouponCreate, CouponOut, CouponUpdate
from app.schemas.dashboard import (
    ChartPoint,
    DashboardStats,
    OrdersChart,
    PopularProduct,
    StatusBreakdown,
)
from app.schemas.order import (
    CancelOrderRequest,
    ConfirmOrderRequest,
    OrderDetail,
    OrderSummary,
    RefundOut,
    RefundRequest,
    UpdateOrderStatusRequest,
)
from app.schemas.product import (
    CategoryCreate,
    CategoryOut,
    ProductDetail,
    ProductFilters,
    ProductImportRequest,
    ProductImportResult,
    ProductSummary,
    ProductCreate,
    ProductUpdate,
    VariantCreate,
    VariantOut,
    VariantUpdate,
)
from app.schemas.review import ReviewAdminOut
from app.schemas.shipping import (
    PincodeUploadRequest,
    PincodeUploadResult,
    ShippingConfigCreate,
    ShippingConfigOut,
    ShippingConfigUpdate,
)
from app.schemas.user import AdminUserSummary
from app.services import account_merge as merge_service
from app.services import conversation as conversation_service
from app.services import coupon as coupon_service
from app.services import email as email_service
from app.services import notification as notification_service
from app.services import order as order_service
from app.services import payment as payment_service
from app.services import product as product_service
from app.services import review as review_service
from app.services import shipping as shipping_service
from app.services import user as user_service
from app.services import whatsapp as whatsapp_service
from logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"], responses=STAFF_RESPONSES)

# One limit for the whole staff surface. Applied per-agent, not per-IP: the
# rate-limit key is set to `agent:<id>` by the auth dependency, so a shared
# office NAT can't throttle the whole team.
ADMIN_LIMIT = settings.rate_limit_admin

# Statuses where the customer's money has actually been captured. Used for
# every revenue figure so pending-payment carts and refunds don't inflate it.
REVENUE_STATUSES: tuple[str, ...] = tuple(
    s.value
    for s in OrderStatus
    if s
    not in (OrderStatus.PENDING_PAYMENT, OrderStatus.CANCELLED, OrderStatus.REFUNDED)
)

OPEN_CONVERSATION_STATUSES: tuple[str, ...] = (
    ConversationStatus.AI.value,
    ConversationStatus.QUEUED.value,
    ConversationStatus.HUMAN.value,
)

LOW_STOCK_THRESHOLD = 5


# ==========================================================================
# Helpers
# ==========================================================================
def _start_of_today() -> datetime:
    now = utcnow()
    return datetime.combine(now.date(), time.min, tzinfo=timezone.utc)


def _wa_message_id(response: dict[str, Any]) -> str | None:
    messages = response.get("messages") if isinstance(response, dict) else None
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        return messages[0].get("id")
    return None


async def _notify_customer_whatsapp(order: Order, body: str) -> None:
    """Best-effort courtesy ping. A Meta outage must not fail the agent's action."""
    customer = getattr(order, "user", None)
    if customer is None or not customer.phone or not customer.whatsapp_opt_in:
        return
    try:
        await whatsapp_service.send_text(customer.phone, body)
    except Exception as exc:
        log.warning(
            "admin_customer_whatsapp_failed", order_id=order.id, error=str(exc)
        )


async def _notify_customer_email(db, order_id: int, email_type: EmailType) -> None:
    try:
        await email_service.send_order_email(db, order_id, email_type)
    except Exception as exc:
        log.warning(
            "admin_customer_email_failed", order_id=order_id, error=str(exc)
        )


# ==========================================================================
# Conversations — agent-level: this is the queue staff work all day.
# ==========================================================================
@router.get(
    "/conversations",
    response_model=Page[ConversationSummary],
    summary="List conversations",
)
@limiter.limit(ADMIN_LIMIT)
async def list_conversations(
    request: Request,
    agent: CurrentAgent,
    db: DbSession,
    limit: PageLimit,
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="ai | queued | human | resolved",
    ),
    assigned_agent_id: int | None = Query(default=None),
    mine: bool = Query(default=False, description="Only conversations assigned to me"),
    unassigned: bool = Query(default=False),
    cursor: str | None = Query(default=None),
) -> Any:
    if status_filter is not None and status_filter not in set(ConversationStatus):
        raise ValidationError(f"Unknown conversation status: {status_filter}")

    return await conversation_service.list_conversations(
        db,
        status=status_filter,
        assigned_agent_id=agent.id if mine else assigned_agent_id,
        unassigned_only=unassigned,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
    summary="Conversation detail",
)
@limiter.limit(ADMIN_LIMIT)
async def get_conversation(
    request: Request,
    conversation_id: int,
    agent: CurrentAgent,
    db: DbSession,
) -> Any:
    conversation = await conversation_service.get_conversation(db, conversation_id)
    return conversation_service.serialize_conversation(conversation, detail=True)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=Page[MessageOut],
    summary="Message history",
)
@limiter.limit(ADMIN_LIMIT)
async def list_conversation_messages(
    request: Request,
    conversation_id: int,
    agent: CurrentAgent,
    db: DbSession,
    limit: PageLimit,
    cursor: str | None = Query(default=None),
) -> Any:
    # 404s before paging so a bad id doesn't look like an empty thread.
    await conversation_service.get_conversation(db, conversation_id)
    return await conversation_service.list_messages(
        db, conversation_id, cursor=cursor, limit=limit
    )


@router.post(
    "/conversations/{conversation_id}/assign",
    response_model=ConversationDetail,
    summary="Assign or take over a conversation",
)
@limiter.limit(ADMIN_LIMIT)
async def assign_conversation(
    request: Request,
    conversation_id: int,
    payload: AssignConversationRequest,
    agent: CurrentAgent,
    db: DbSession,
) -> Any:
    target_agent_id = payload.agent_id or agent.id

    # Claiming work for yourself is routine; routing it to someone else is a
    # supervisory act, so that variant is admin-only.
    if target_agent_id != agent.id and agent.role != AgentRole.ADMIN:
        raise PermissionError_("Only an admin can assign a conversation to another agent")

    await conversation_service.assign(
        db, conversation_id, target_agent_id, take_over=payload.take_over
    )
    await db.commit()

    # Re-read so `assigned_agent_name` reflects the new owner rather than the
    # relationship that was loaded before the write.
    conversation = await conversation_service.get_conversation(db, conversation_id)
    return conversation_service.serialize_conversation(conversation, detail=True)


@router.post(
    "/conversations/{conversation_id}/unassign",
    response_model=ConversationDetail,
    summary="Return a conversation to the queue",
)
@limiter.limit(ADMIN_LIMIT)
async def unassign_conversation(
    request: Request,
    conversation_id: int,
    agent: CurrentAgent,
    db: DbSession,
) -> Any:
    conversation = await conversation_service.get_conversation(db, conversation_id)
    if (
        conversation.assigned_agent_id is not None
        and conversation.assigned_agent_id != agent.id
        and agent.role != AgentRole.ADMIN
    ):
        raise PermissionError_("Only an admin can unassign another agent's conversation")

    await conversation_service.unassign(db, conversation_id)
    await db.commit()

    conversation = await conversation_service.get_conversation(db, conversation_id)
    return conversation_service.serialize_conversation(conversation, detail=True)


@router.post(
    "/conversations/{conversation_id}/reply",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Send an agent reply over WhatsApp",
)
@limiter.limit(ADMIN_LIMIT)
async def reply_to_conversation(
    request: Request,
    conversation_id: int,
    payload: AgentReplyRequest,
    agent: CurrentAgent,
    db: DbSession,
) -> Any:
    conversation = await conversation_service.get_conversation(db, conversation_id)

    # Replying implies ownership: the AI must fall silent the moment a person
    # speaks. `assign` raises ConflictError if someone else already owns it.
    if (
        conversation.status != ConversationStatus.HUMAN
        or conversation.assigned_agent_id != agent.id
    ):
        conversation = await conversation_service.assign(
            db, conversation_id, agent.id, take_over=False
        )

    customer = conversation.user
    if customer is None or not customer.phone:
        raise ValidationError("This customer has no WhatsApp number on file")

    try:
        wa_response = await whatsapp_service.send_text(customer.phone, payload.body)
    except UpstreamError:
        # Persist the attempt so the thread shows what the agent tried to send,
        # then surface the delivery failure. Committing first is deliberate:
        # the session would otherwise be rolled back on the way out.
        message = await conversation_service.record_outbound(
            db,
            conversation,
            payload.body,
            ai_generated=False,
            sent_by_agent_id=agent.id,
            error="whatsapp_send_failed",
        )
        message.delivery_status = "failed"
        await db.commit()
        raise

    message = await conversation_service.record_outbound(
        db,
        conversation,
        payload.body,
        ai_generated=False,
        sent_by_agent_id=agent.id,
        wa_message_id=_wa_message_id(wa_response),
    )

    if payload.resolve:
        await conversation_service.resolve(db, conversation_id, resolved_by="human")

    await db.commit()
    return conversation_service.serialize_message(message)


@router.post(
    "/conversations/{conversation_id}/resolve",
    response_model=ConversationDetail,
    summary="Mark a conversation resolved",
)
@limiter.limit(ADMIN_LIMIT)
async def resolve_conversation(
    request: Request,
    conversation_id: int,
    agent: CurrentAgent,
    db: DbSession,
) -> Any:
    await conversation_service.resolve(db, conversation_id, resolved_by="human")
    await db.commit()

    conversation = await conversation_service.get_conversation(db, conversation_id)
    return conversation_service.serialize_conversation(conversation, detail=True)


@router.post(
    "/conversations/{conversation_id}/return-to-ai",
    response_model=ConversationDetail,
    summary="Hand a conversation back to the AI",
)
@limiter.limit(ADMIN_LIMIT)
async def return_conversation_to_ai(
    request: Request,
    conversation_id: int,
    agent: CurrentAgent,
    db: DbSession,
) -> Any:
    await conversation_service.return_to_ai(db, conversation_id)
    await db.commit()

    conversation = await conversation_service.get_conversation(db, conversation_id)
    return conversation_service.serialize_conversation(conversation, detail=True)


# ==========================================================================
# Orders — agent-level fulfilment, admin-only for refunds (money leaves).
# ==========================================================================
@router.get("/orders", response_model=Page[OrderSummary], summary="List orders")
@limiter.limit(ADMIN_LIMIT)
async def list_orders(
    request: Request,
    agent: CurrentAgent,
    db: DbSession,
    limit: PageLimit,
    status_filter: str | None = Query(default=None, alias="status"),
    user_id: int | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> Any:
    if status_filter is not None and status_filter not in set(OrderStatus):
        raise ValidationError(f"Unknown order status: {status_filter}")
    if date_from and date_to and date_from > date_to:
        raise ValidationError("date_from must be before date_to")

    return await order_service.list_orders(
        db,
        user_id=user_id,
        status=status_filter,
        cursor=cursor,
        limit=limit,
        date_from=date_from,
        date_to=date_to,
    )


@router.get(
    "/orders/{order_id}", response_model=OrderDetail, summary="Order detail"
)
@limiter.limit(ADMIN_LIMIT)
async def get_order(
    request: Request, order_id: int, agent: CurrentAgent, db: DbSession
) -> Any:
    order = await order_service.get_order(db, order_id)
    return await order_service.serialize_order_detail(db, order)


@router.post(
    "/orders/{order_id}/confirm",
    response_model=OrderDetail,
    summary="Confirm an order with a delivery ETA",
)
@limiter.limit(ADMIN_LIMIT)
async def confirm_order(
    request: Request,
    order_id: int,
    payload: ConfirmOrderRequest,
    agent: CurrentAgent,
    db: DbSession,
) -> Any:
    """The human-in-the-loop step: a person promises a delivery date.

    Deliberately agent-level — this is the core daily job, not a privileged
    configuration change.
    """
    if payload.delivery_eta is not None:
        eta = payload.delivery_eta
        if eta.tzinfo is None:
            eta = eta.replace(tzinfo=timezone.utc)
        if eta < utcnow():
            raise ValidationError("Delivery ETA cannot be in the past")

    order = await order_service.confirm_order(
        db,
        order_id,
        delivery_eta=payload.delivery_eta,
        tracking_number=payload.tracking_number,
        tracking_url=payload.tracking_url,
        note=payload.note,
    )
    await db.commit()

    if payload.notify_customer:
        eta_text = (
            f" Expected delivery: {payload.delivery_eta:%d %b %Y}."
            if payload.delivery_eta
            else ""
        )
        tracking_text = (
            f" Tracking: {payload.tracking_number}." if payload.tracking_number else ""
        )
        await _notify_customer_whatsapp(
            order,
            f"Good news! Your order {order.order_number} is confirmed."
            f"{eta_text}{tracking_text}",
        )
        await _notify_customer_email(db, order.id, EmailType.ORDER_CONFIRMED)

    log.info("admin_order_confirmed", order_id=order.id, agent_id=agent.id)
    order = await order_service.get_order(db, order_id)
    return await order_service.serialize_order_detail(db, order)


@router.post(
    "/orders/{order_id}/status",
    response_model=OrderDetail,
    summary="Move an order to the next fulfilment status",
)
@limiter.limit(ADMIN_LIMIT)
async def update_order_status(
    request: Request,
    order_id: int,
    payload: UpdateOrderStatusRequest,
    agent: CurrentAgent,
    db: DbSession,
) -> Any:
    order = await order_service.update_status(
        db,
        order_id,
        payload.status,
        tracking_number=payload.tracking_number,
        tracking_url=payload.tracking_url,
        note=payload.note,
    )
    await db.commit()

    if payload.notify_customer:
        await _notify_customer_whatsapp(
            order, f"Update on order {order.order_number}: {order.status}."
        )
        email_type = {
            OrderStatus.SHIPPED.value: EmailType.ORDER_SHIPPED,
            OrderStatus.DELIVERED.value: EmailType.ORDER_DELIVERED,
        }.get(order.status)
        if email_type is not None:
            await _notify_customer_email(db, order.id, email_type)

    log.info(
        "admin_order_status_updated",
        order_id=order.id,
        status=order.status,
        agent_id=agent.id,
    )
    order = await order_service.get_order(db, order_id)
    return await order_service.serialize_order_detail(db, order)


@router.post(
    "/orders/{order_id}/cancel",
    response_model=OrderDetail,
    summary="Cancel an order",
)
@limiter.limit(ADMIN_LIMIT)
async def cancel_order(
    request: Request,
    order_id: int,
    payload: CancelOrderRequest,
    agent: CurrentAgent,
    db: DbSession,
) -> Any:
    order = await order_service.get_order(db, order_id)
    await order_service.cancel_order(db, order, payload.reason, by_agent=True)
    await db.commit()

    await _notify_customer_whatsapp(
        order,
        f"Your order {order.order_number} has been cancelled. Reason: {payload.reason}",
    )

    log.info("admin_order_cancelled", order_id=order.id, agent_id=agent.id)
    order = await order_service.get_order(db, order_id)
    return await order_service.serialize_order_detail(db, order)


@router.post(
    "/orders/{order_id}/refund",
    response_model=RefundOut,
    status_code=status.HTTP_201_CREATED,
    summary="Refund an order",
)
@limiter.limit(ADMIN_LIMIT)
async def refund_order(
    request: Request,
    order_id: int,
    payload: RefundRequest,
    admin: CurrentAdmin,
    db: DbSession,
) -> Any:
    """Admin-only: this is the one staff action that moves money outward."""
    order = await order_service.get_order(db, order_id)
    refund = await payment_service.initiate_refund(
        db, order, payload.amount, payload.reason, agent_id=admin.id
    )
    await db.commit()

    log.info(
        "admin_refund_initiated",
        order_id=order.id,
        refund_id=refund.id,
        admin_id=admin.id,
    )
    return refund


# ==========================================================================
# Users
# ==========================================================================
@router.get(
    "/users", response_model=Page[AdminUserSummary], summary="List / search customers"
)
@limiter.limit(ADMIN_LIMIT)
async def list_users(
    request: Request,
    agent: CurrentAgent,
    db: DbSession,
    limit: PageLimit,
    q: str | None = Query(default=None, max_length=200, description="Name, email or phone"),
    cursor: str | None = Query(default=None),
) -> Any:
    # Aggregate spend in a single grouped subquery rather than one query per
    # user — the list is the most-hit admin screen there is.
    spend = (
        select(
            Order.user_id.label("user_id"),
            func.count(Order.id).label("order_count"),
            func.coalesce(func.sum(Order.total), 0).label("total_spent"),
        )
        .where(Order.status.in_(REVENUE_STATUSES))
        .group_by(Order.user_id)
        .subquery()
    )

    stmt: Select = (
        select(
            User,
            func.coalesce(spend.c.order_count, 0),
            func.coalesce(spend.c.total_spent, 0),
        )
        .outerjoin(spend, spend.c.user_id == User.id)
        .where(User.deleted_at.is_(None))
    )

    if q:
        needle = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                User.name.ilike(needle),
                User.email.ilike(needle),
                User.phone.ilike(needle),
            )
        )

    stmt = apply_cursor(stmt, User.created_at, User.id, cursor, descending=True)
    rows = (await db.execute(stmt.limit(limit + 1))).all()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = (
        encode_cursor(page_rows[-1][0].created_at, page_rows[-1][0].id)
        if has_more and page_rows
        else None
    )

    return {
        "items": [
            {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "phone": user.phone,
                "auth_provider": user.auth_provider,
                "whatsapp_opt_in": user.whatsapp_opt_in,
                "order_count": int(order_count or 0),
                "total_spent": float(total_spent or 0),
            }
            for user, order_count, total_spent in page_rows
        ],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@router.post(
    "/users/merge",
    summary="Merge one customer account into another",
)
@limiter.limit(ADMIN_LIMIT)
async def merge_users(
    request: Request,
    admin: CurrentAdmin,
    db: DbSession,
    source_id: int = Body(..., description="Account to absorb (will be soft-deleted)"),
    target_id: int = Body(..., description="Account that survives"),
) -> dict[str, Any]:
    """Admin-only and destructive: the source account is retired.

    The service is idempotent, so a retried call reports
    `{"merged": false, "reason": "already_merged"}` rather than erroring.
    """
    result = await merge_service.merge_accounts(db, source_id, target_id)
    await db.commit()
    log.info(
        "admin_accounts_merged",
        source_id=source_id,
        target_id=target_id,
        admin_id=admin.id,
    )
    return result


@router.get("/users/{user_id}", summary="Customer detail with order history")
@limiter.limit(ADMIN_LIMIT)
async def get_user(
    request: Request,
    user_id: int,
    agent: CurrentAgent,
    db: DbSession,
    order_limit: PageLimit,
) -> dict[str, Any]:
    user = await user_service.get_user(db, user_id)

    orders = await order_service.list_orders(db, user_id=user_id, limit=order_limit)
    totals = (
        await db.execute(
            select(
                func.count(Order.id),
                func.coalesce(func.sum(Order.total), 0),
            ).where(Order.user_id == user_id, Order.status.in_(REVENUE_STATUSES))
        )
    ).one()

    payload = user_service.serialize_user(user)
    payload.update(
        {
            "order_count": int(totals[0] or 0),
            "total_spent": float(totals[1] or 0),
            "merged_into_user_id": user.merged_into_user_id,
            "last_login_at": user.last_login_at,
            "addresses": [
                user_service.serialize_address(a)
                for a in await user_service.list_addresses(db, user_id)
            ],
            "orders": orders,
        }
    )
    return payload


# ==========================================================================
# Products & catalogue — reads are agent-level, writes are admin-only.
# ==========================================================================
@router.get(
    "/products", response_model=Page[ProductSummary], summary="List products"
)
@limiter.limit(ADMIN_LIMIT)
async def list_products(
    request: Request,
    agent: CurrentAgent,
    db: DbSession,
    limit: PageLimit,
    q: str | None = Query(default=None, max_length=200),
    category_id: int | None = Query(default=None),
    include_inactive: bool = Query(
        default=True, description="Staff see drafts and deactivated products by default"
    ),
    cursor: str | None = Query(default=None),
) -> Any:
    filters = ProductFilters(q=q, category_id=category_id)
    return await product_service.list_products(
        db, filters, cursor=cursor, limit=limit, include_inactive=include_inactive
    )


@router.post(
    "/products",
    response_model=ProductDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a product",
)
@limiter.limit(ADMIN_LIMIT)
async def create_product(
    request: Request, payload: ProductCreate, admin: CurrentAdmin, db: DbSession
) -> Any:
    product = await product_service.create_product(db, payload)
    await db.commit()
    log.info("admin_product_created", product_id=product.id, admin_id=admin.id)
    return await product_service.get_product_detail(db, product.id, use_cache=False)


@router.post(
    "/products/import",
    response_model=ProductImportResult,
    summary="Bulk-import products",
)
@limiter.limit(ADMIN_LIMIT)
async def import_products(
    request: Request,
    payload: ProductImportRequest,
    admin: CurrentAdmin,
    db: DbSession,
) -> Any:
    result = await product_service.bulk_import(
        db, payload.products, payload.update_existing
    )
    await db.commit()
    log.info(
        "admin_products_imported",
        created=result["created"],
        updated=result["updated"],
        admin_id=admin.id,
    )
    return result


@router.get(
    "/products/{product_id}", response_model=ProductDetail, summary="Product detail"
)
@limiter.limit(ADMIN_LIMIT)
async def get_product(
    request: Request, product_id: int, agent: CurrentAgent, db: DbSession
) -> Any:
    # Staff always read through to the database; a cached copy could hide a
    # colleague's edit made seconds ago.
    return await product_service.get_product_detail(db, product_id, use_cache=False)


@router.patch(
    "/products/{product_id}", response_model=ProductDetail, summary="Update a product"
)
@limiter.limit(ADMIN_LIMIT)
async def update_product(
    request: Request,
    product_id: int,
    payload: ProductUpdate,
    admin: CurrentAdmin,
    db: DbSession,
) -> Any:
    await product_service.update_product(db, product_id, payload)
    await db.commit()
    log.info("admin_product_updated", product_id=product_id, admin_id=admin.id)
    return await product_service.get_product_detail(db, product_id, use_cache=False)


@router.delete(
    "/products/{product_id}",
    response_model=MessageResponse,
    summary="Soft-delete a product",
)
@limiter.limit(ADMIN_LIMIT)
async def delete_product(
    request: Request, product_id: int, admin: CurrentAdmin, db: DbSession
) -> Any:
    await product_service.soft_delete_product(db, product_id)
    await db.commit()

    # Retire it from the WhatsApp catalogue too, otherwise shoppers can still
    # tap a product that no longer exists. Queued, not inline: the delete above
    # is already committed and Meta being unreachable must not fail this
    # response. If the enqueue is lost the nightly sweep still reconciles.
    await enqueue("delete_product_task", product_id)

    log.info("admin_product_deleted", product_id=product_id, admin_id=admin.id)
    return MessageResponse(detail="Product deleted")


@router.post(
    "/products/{product_id}/variants",
    response_model=VariantOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a variant",
)
@limiter.limit(ADMIN_LIMIT)
async def add_variant(
    request: Request,
    product_id: int,
    payload: VariantCreate,
    admin: CurrentAdmin,
    db: DbSession,
) -> Any:
    variant = await product_service.add_variant(db, product_id, payload)
    await db.commit()
    product = await product_service.get_product(db, product_id)
    return _variant_out(variant, product.base_price)


@router.patch(
    "/products/{product_id}/variants/{variant_id}",
    response_model=VariantOut,
    summary="Update a variant",
)
@limiter.limit(ADMIN_LIMIT)
async def update_variant(
    request: Request,
    product_id: int,
    variant_id: int,
    payload: VariantUpdate,
    admin: CurrentAdmin,
    db: DbSession,
) -> Any:
    variant = await product_service.update_variant(db, product_id, variant_id, payload)
    await db.commit()
    product = await product_service.get_product(db, product_id)
    return _variant_out(variant, product.base_price)


def _variant_out(variant: ProductVariant, base_price: Decimal | None) -> dict[str, Any]:
    """`VariantOut.price` is the resolved price, which lives on the product.

    `base_price` is passed in rather than read from `variant.product`: the
    service returns a freshly-added variant whose relationship was never
    loaded, and a lazy load on an AsyncSession raises MissingGreenlet.
    """
    price = variant.price_override if variant.price_override is not None else base_price
    return {
        "id": variant.id,
        "sku": variant.sku,
        "name": variant.name,
        "attributes": variant.attributes or {},
        "price": price if price is not None else Decimal("0.00"),
        "price_override": variant.price_override,
        "stock": variant.stock,
        "in_stock": variant.stock > 0,
        "active": variant.active,
        "meta_retailer_id": variant.meta_retailer_id,
    }


@router.get(
    "/categories", response_model=list[CategoryOut], summary="List categories"
)
@limiter.limit(ADMIN_LIMIT)
async def list_categories(
    request: Request, agent: CurrentAgent, db: DbSession
) -> Any:
    return await product_service.list_categories(db, use_cache=False)


@router.post(
    "/categories",
    response_model=CategoryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a category",
)
@limiter.limit(ADMIN_LIMIT)
async def create_category(
    request: Request, payload: CategoryCreate, admin: CurrentAdmin, db: DbSession
) -> Any:
    category = await product_service.create_category(db, payload)
    await db.commit()
    return {
        "id": category.id,
        "name": category.name,
        "slug": category.slug,
        "description": category.description,
        "image_url": category.image_url,
        "sort_order": category.sort_order,
        "active": category.active,
        "product_count": 0,
    }


# Both sync routes hand off to arq rather than calling the sync services
# directly. Those services commit and roll back the session they are given,
# which would end this request's transaction underneath the router — and a
# batch of Meta API calls has no business blocking an admin HTTP request.
@router.post("/catalog/sync", summary="Queue a sync of pending products")
@limiter.limit(ADMIN_LIMIT)
async def sync_catalog(
    request: Request,
    admin: CurrentAdmin,
    db: DbSession,
) -> dict[str, Any]:
    queued = await enqueue("sync_pending_task")
    log.info("admin_catalog_sync_queued", admin_id=admin.id, queued=queued)
    if not queued:
        raise UpstreamError("Could not reach the job queue; sync was not scheduled")
    return {"queued": True}


@router.post(
    "/catalog/sync/{product_id}", summary="Queue a sync of one product"
)
@limiter.limit(ADMIN_LIMIT)
async def sync_catalog_product(
    request: Request, product_id: int, admin: CurrentAdmin, db: DbSession
) -> dict[str, Any]:
    # 404 early rather than queueing work for a product that does not exist.
    await product_service.get_product(db, product_id)
    queued = await enqueue("sync_product_task", product_id)
    if not queued:
        raise UpstreamError("Could not reach the job queue; sync was not scheduled")
    return {"product_id": product_id, "queued": True}


# ==========================================================================
# Coupons — pricing configuration, so admin-only for every write.
# ==========================================================================
@router.get("/coupons", response_model=Page[CouponOut], summary="List coupons")
@limiter.limit(ADMIN_LIMIT)
async def list_coupons(
    request: Request,
    agent: CurrentAgent,
    db: DbSession,
    limit: PageLimit,
    q: str | None = Query(default=None, max_length=50),
    active: bool | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> Any:
    stmt: Select = select(Coupon).where(Coupon.deleted_at.is_(None))
    if q:
        stmt = stmt.where(Coupon.code.ilike(f"%{q.strip()}%"))
    if active is not None:
        stmt = stmt.where(Coupon.active.is_(active))

    stmt = apply_cursor(stmt, Coupon.created_at, Coupon.id, cursor, descending=True)
    rows = (await db.execute(stmt.limit(limit + 1))).scalars().all()

    has_more = len(rows) > limit
    coupons = list(rows[:limit])
    next_cursor = (
        encode_cursor(coupons[-1].created_at, coupons[-1].id)
        if has_more and coupons
        else None
    )

    # One grouped COUNT for the whole page instead of `serialize_coupon`'s
    # per-coupon count, which would be N+1 across the list.
    usage: dict[int, int] = {}
    if coupons:
        usage = {
            coupon_id: int(count)
            for coupon_id, count in (
                await db.execute(
                    select(CouponUsage.coupon_id, func.count(CouponUsage.id))
                    .where(CouponUsage.coupon_id.in_([c.id for c in coupons]))
                    .group_by(CouponUsage.coupon_id)
                )
            ).all()
        }

    items = []
    for coupon in coupons:
        out = CouponOut.model_validate(coupon)
        out.times_used = usage.get(coupon.id, 0)
        items.append(out)

    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


@router.post(
    "/coupons",
    response_model=CouponOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a coupon",
)
@limiter.limit(ADMIN_LIMIT)
async def create_coupon(
    request: Request, payload: CouponCreate, admin: CurrentAdmin, db: DbSession
) -> Any:
    coupon = await coupon_service.create_coupon(db, payload)
    await db.commit()
    log.info("admin_coupon_created", code=coupon.code, admin_id=admin.id)
    return await coupon_service.serialize_coupon(db, coupon)


@router.patch(
    "/coupons/{coupon_id}", response_model=CouponOut, summary="Update a coupon"
)
@limiter.limit(ADMIN_LIMIT)
async def update_coupon(
    request: Request,
    coupon_id: int,
    payload: CouponUpdate,
    admin: CurrentAdmin,
    db: DbSession,
) -> Any:
    coupon = await coupon_service.update_coupon(db, coupon_id, payload)
    await db.commit()
    return await coupon_service.serialize_coupon(db, coupon)


@router.delete(
    "/coupons/{coupon_id}",
    response_model=MessageResponse,
    summary="Soft-delete a coupon",
)
@limiter.limit(ADMIN_LIMIT)
async def delete_coupon(
    request: Request, coupon_id: int, admin: CurrentAdmin, db: DbSession
) -> Any:
    coupon = (
        await db.execute(
            select(Coupon).where(Coupon.id == coupon_id, Coupon.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if coupon is None:
        raise NotFoundError("Coupon not found")

    # Soft delete, not a row delete: `coupon_usage` rows reference it and past
    # orders still show the code that was applied.
    coupon.soft_delete()
    coupon.active = False
    await db.commit()
    log.info("admin_coupon_deleted", coupon_id=coupon_id, admin_id=admin.id)
    return MessageResponse(detail="Coupon deleted")


# ==========================================================================
# Reviews — moderation is agent-level, removal is admin-only.
# ==========================================================================
@router.get(
    "/reviews", response_model=Page[ReviewAdminOut], summary="List reviews"
)
@limiter.limit(ADMIN_LIMIT)
async def list_reviews(
    request: Request,
    agent: CurrentAgent,
    db: DbSession,
    limit: PageLimit,
    product_id: int | None = Query(default=None),
    flagged: bool | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    cursor: str | None = Query(default=None),
) -> Any:
    # `review_service.list_product_reviews` hides flagged rows because it feeds
    # the storefront; moderation needs to see exactly what it hides.
    stmt: Select = (
        select(Review, Product.name)
        .outerjoin(Product, Review.product_id == Product.id)
        .options(selectinload(Review.user))
    )
    if not include_deleted:
        stmt = stmt.where(Review.deleted_at.is_(None))
    if product_id is not None:
        stmt = stmt.where(Review.product_id == product_id)
    if flagged is not None:
        stmt = stmt.where(Review.flagged.is_(flagged))

    stmt = apply_cursor(stmt, Review.created_at, Review.id, cursor, descending=True)
    rows = (await db.execute(stmt.limit(limit + 1))).all()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = (
        encode_cursor(page_rows[-1][0].created_at, page_rows[-1][0].id)
        if has_more and page_rows
        else None
    )

    return {
        "items": [
            {
                "id": review.id,
                "user_id": review.user_id,
                "product_id": review.product_id,
                "order_id": review.order_id,
                "rating": review.rating,
                "comment": review.comment,
                "verified_purchase": review.verified_purchase,
                "author_name": review.user.display_name if review.user else None,
                "created_at": review.created_at,
                "flagged": review.flagged,
                "product_name": product_name,
                "deleted_at": review.deleted_at,
            }
            for review, product_name in page_rows
        ],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@router.post(
    "/reviews/{review_id}/flag",
    response_model=MessageResponse,
    summary="Flag a review (hides it from the storefront)",
)
@limiter.limit(ADMIN_LIMIT)
async def flag_review(
    request: Request, review_id: int, agent: CurrentAgent, db: DbSession
) -> Any:
    await review_service.set_flagged(db, review_id, True)
    await db.commit()
    return MessageResponse(detail="Review flagged")


@router.post(
    "/reviews/{review_id}/unflag",
    response_model=MessageResponse,
    summary="Unflag a review",
)
@limiter.limit(ADMIN_LIMIT)
async def unflag_review(
    request: Request, review_id: int, agent: CurrentAgent, db: DbSession
) -> Any:
    await review_service.set_flagged(db, review_id, False)
    await db.commit()
    return MessageResponse(detail="Review unflagged")


@router.delete(
    "/reviews/{review_id}",
    response_model=MessageResponse,
    summary="Delete a review",
)
@limiter.limit(ADMIN_LIMIT)
async def delete_review(
    request: Request, review_id: int, admin: CurrentAdmin, db: DbSession
) -> Any:
    """Admin-only: destroying customer content, unlike flagging, is not reversible
    from the moderation queue."""
    await review_service.admin_delete_review(db, review_id)
    await db.commit()
    log.info("admin_review_deleted", review_id=review_id, admin_id=admin.id)
    return MessageResponse(detail="Review deleted")


# ==========================================================================
# Shipping — rate cards and serviceability are configuration: admin writes.
# ==========================================================================
@router.get(
    "/shipping/configs",
    response_model=list[ShippingConfigOut],
    summary="List shipping rate cards",
)
@limiter.limit(ADMIN_LIMIT)
async def list_shipping_configs(
    request: Request, agent: CurrentAgent, db: DbSession
) -> Any:
    return await shipping_service.list_shipping_configs(db)


@router.post(
    "/shipping/configs",
    response_model=ShippingConfigOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a shipping rate card",
)
@limiter.limit(ADMIN_LIMIT)
async def create_shipping_config(
    request: Request,
    payload: ShippingConfigCreate,
    admin: CurrentAdmin,
    db: DbSession,
) -> Any:
    config = await shipping_service.create_shipping_config(db, payload)
    await db.commit()
    return config


@router.patch(
    "/shipping/configs/{config_id}",
    response_model=ShippingConfigOut,
    summary="Update a shipping rate card",
)
@limiter.limit(ADMIN_LIMIT)
async def update_shipping_config(
    request: Request,
    config_id: int,
    payload: ShippingConfigUpdate,
    admin: CurrentAdmin,
    db: DbSession,
) -> Any:
    config = await shipping_service.update_shipping_config(db, config_id, payload)
    await db.commit()
    return config


@router.post(
    "/shipping/pincodes",
    response_model=PincodeUploadResult,
    summary="Upload serviceable pincodes",
)
@limiter.limit(ADMIN_LIMIT)
async def upload_pincodes(
    request: Request,
    payload: PincodeUploadRequest,
    admin: CurrentAdmin,
    db: DbSession,
) -> Any:
    result = await shipping_service.upload_pincodes(
        db, payload.pincodes, payload.replace_existing
    )
    await db.commit()
    log.info("admin_pincodes_uploaded", admin_id=admin.id, **result)
    return result


# ==========================================================================
# Notifications — always scoped to the caller, so agent-level throughout.
# ==========================================================================
@router.get(
    "/notifications",
    response_model=Page[NotificationOut],
    summary="My notifications",
)
@limiter.limit(ADMIN_LIMIT)
async def list_notifications(
    request: Request,
    agent: CurrentAgent,
    db: DbSession,
    limit: PageLimit,
    unread_only: bool = Query(default=False),
    cursor: str | None = Query(default=None),
) -> Any:
    return await notification_service.list_notifications(
        db, agent.id, unread_only=unread_only, cursor=cursor, limit=limit
    )


@router.get("/notifications/unread-count", summary="My unread notification count")
@limiter.limit(ADMIN_LIMIT)
async def unread_notification_count(
    request: Request, agent: CurrentAgent, db: DbSession
) -> dict[str, int]:
    return {"unread": await notification_service.unread_count(db, agent.id)}


@router.post(
    "/notifications/read-all",
    response_model=MessageResponse,
    summary="Mark all my notifications read",
)
@limiter.limit(ADMIN_LIMIT)
async def mark_all_notifications_read(
    request: Request, agent: CurrentAgent, db: DbSession
) -> Any:
    count = await notification_service.mark_all_read(db, agent.id)
    await db.commit()
    return MessageResponse(detail=f"Marked {count} notifications read")


@router.post(
    "/notifications/{notification_id}/read",
    response_model=NotificationOut,
    summary="Mark one notification read",
)
@limiter.limit(ADMIN_LIMIT)
async def mark_notification_read(
    request: Request, notification_id: int, agent: CurrentAgent, db: DbSession
) -> Any:
    notification = await notification_service.mark_read(db, agent.id, notification_id)
    await db.commit()
    return notification_service.serialize_notification(notification)


# ==========================================================================
# Dashboard — business performance, so admin-only.
# ==========================================================================
@router.get(
    "/dashboard/stats", response_model=DashboardStats, summary="Dashboard statistics"
)
@limiter.limit(ADMIN_LIMIT)
async def dashboard_stats(request: Request, admin: CurrentAdmin, db: DbSession) -> Any:
    """Revenue, queue and catalogue health in seven grouped aggregates.

    Every figure is computed with SUM(CASE ...) inside one pass per table
    instead of a query per metric, so the whole panel is a fixed number of
    round trips no matter how much data there is.
    """
    today = _start_of_today()

    is_revenue = Order.status.in_(REVENUE_STATUSES)
    order_row = (
        await db.execute(
            select(
                func.count(Order.id),
                func.coalesce(
                    func.sum(case((Order.created_at >= today, 1), else_=0)), 0
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (Order.status == OrderStatus.PENDING_CONFIRMATION.value, 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(func.sum(case((is_revenue, Order.total), else_=0)), 0),
                func.coalesce(
                    func.sum(
                        case(
                            (and_(is_revenue, Order.created_at >= today), Order.total),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(func.sum(case((is_revenue, 1), else_=0)), 0),
            )
        )
    ).one()

    (
        total_orders,
        orders_today,
        pending_confirmation,
        total_revenue,
        revenue_today,
        paid_order_count,
    ) = order_row

    status_rows = (
        await db.execute(
            select(Order.status, func.count(Order.id)).group_by(Order.status)
        )
    ).all()

    user_row = (
        await db.execute(
            select(
                func.count(User.id),
                func.coalesce(
                    func.sum(case((User.created_at >= today, 1), else_=0)), 0
                ),
            ).where(User.deleted_at.is_(None))
        )
    ).one()

    cart_row = (
        await db.execute(
            select(
                func.count(Cart.id),
                func.coalesce(
                    func.sum(case((Cart.status == CartStatus.ACTIVE.value, 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case((Cart.status == CartStatus.CHECKED_OUT.value, 1), else_=0)
                    ),
                    0,
                ),
            )
        )
    ).one()
    total_carts, active_carts, converted_carts = cart_row

    conversation_row = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (Conversation.status.in_(OPEN_CONVERSATION_STATUSES), 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (Conversation.status == ConversationStatus.QUEUED.value, 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(case((Conversation.resolved_by == "ai", 1), else_=0)), 0
                ),
                func.coalesce(
                    func.sum(
                        case((Conversation.resolved_by.is_not(None), 1), else_=0)
                    ),
                    0,
                ),
            )
        )
    ).one()
    (
        conversations_open,
        conversations_queued,
        resolved_by_ai,
        resolved_total,
    ) = conversation_row

    ai_tokens_today = (
        await db.scalar(
            select(
                func.coalesce(
                    func.sum(AITokenUsage.input_tokens + AITokenUsage.output_tokens), 0
                )
            ).where(AITokenUsage.created_at >= today)
        )
    ) or 0

    popular_rows = (
        await db.execute(
            select(
                OrderItem.product_id,
                func.max(OrderItem.product_name),
                func.sum(OrderItem.quantity),
                func.sum(OrderItem.line_total),
            )
            .join(Order, OrderItem.order_id == Order.id)
            .where(is_revenue, OrderItem.product_id.is_not(None))
            .group_by(OrderItem.product_id)
            .order_by(func.sum(OrderItem.quantity).desc())
            .limit(5)
        )
    ).all()

    total_revenue = Decimal(str(total_revenue or 0))
    paid_order_count = int(paid_order_count or 0)
    average_order_value = (
        (total_revenue / paid_order_count).quantize(Decimal("0.01"))
        if paid_order_count
        else Decimal("0.00")
    )

    return DashboardStats(
        total_orders=int(total_orders or 0),
        orders_today=int(orders_today or 0),
        pending_confirmation=int(pending_confirmation or 0),
        total_revenue=total_revenue,
        revenue_today=Decimal(str(revenue_today or 0)),
        average_order_value=average_order_value,
        total_users=int(user_row[0] or 0),
        new_users_today=int(user_row[1] or 0),
        active_carts=int(active_carts or 0),
        conversion_rate=(
            round(int(converted_carts or 0) / int(total_carts) * 100, 2)
            if total_carts
            else 0.0
        ),
        ai_resolution_rate=(
            round(int(resolved_by_ai or 0) / int(resolved_total) * 100, 2)
            if resolved_total
            else 0.0
        ),
        conversations_open=int(conversations_open or 0),
        conversations_queued=int(conversations_queued or 0),
        ai_tokens_today=int(ai_tokens_today or 0),
        popular_products=[
            PopularProduct(
                product_id=product_id,
                name=name or "Unknown product",
                units_sold=int(units or 0),
                revenue=Decimal(str(revenue or 0)),
            )
            for product_id, name, units, revenue in popular_rows
        ],
        status_breakdown=[
            StatusBreakdown(status=order_status, count=int(count))
            for order_status, count in status_rows
        ],
    )


@router.get(
    "/dashboard/orders-chart",
    response_model=OrdersChart,
    summary="Orders and revenue over time",
)
@limiter.limit(ADMIN_LIMIT)
async def dashboard_orders_chart(
    request: Request,
    admin: CurrentAdmin,
    db: DbSession,
    granularity: str = Query(default="day", pattern="^(day|week|month)$"),
    days: int = Query(default=30, ge=1, le=365),
) -> Any:
    since = utcnow() - timedelta(days=days)
    # date_trunc is PostgreSQL-specific, which matches the production database.
    bucket = func.date_trunc(granularity, Order.created_at).label("period")

    rows = (
        await db.execute(
            select(
                bucket,
                func.count(Order.id),
                func.coalesce(func.sum(Order.total), 0),
            )
            .where(Order.created_at >= since, Order.status.in_(REVENUE_STATUSES))
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()

    return OrdersChart(
        granularity=granularity,
        points=[
            ChartPoint(
                period=period.date() if isinstance(period, datetime) else period,
                orders=int(count or 0),
                revenue=Decimal(str(revenue or 0)),
            )
            for period, count, revenue in rows
            if period is not None
        ],
    )


@router.get("/dashboard/low-stock", summary="Variants running low on stock")
@limiter.limit(ADMIN_LIMIT)
async def dashboard_low_stock(
    request: Request,
    admin: CurrentAdmin,
    db: DbSession,
    limit: PageLimit,
    threshold: int = Query(default=LOW_STOCK_THRESHOLD, ge=0, le=1000),
) -> list[dict[str, Any]]:
    """Separate from `/dashboard/stats` because `DashboardStats` has no field
    for it — see the note in the handover."""
    rows = (
        await db.execute(
            select(ProductVariant, Product.name)
            .join(Product, ProductVariant.product_id == Product.id)
            .where(
                ProductVariant.stock <= threshold,
                ProductVariant.active.is_(True),
                ProductVariant.deleted_at.is_(None),
                Product.deleted_at.is_(None),
            )
            .order_by(ProductVariant.stock.asc(), ProductVariant.id.asc())
            .limit(limit)
        )
    ).all()

    return [
        {
            "variant_id": variant.id,
            "product_id": variant.product_id,
            "product_name": product_name,
            "sku": variant.sku,
            "variant_name": variant.name,
            "stock": variant.stock,
        }
        for variant, product_name in rows
    ]
