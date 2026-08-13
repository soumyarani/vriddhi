from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_agent, require_admin
from app.models import (
    Agent,
    Conversation,
    Coupon,
    CouponUsage,
    Message,
    Notification,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    Refund,
    Review,
    ShippingConfig,
    User,
)
from app.pagination import next_cursor, parse_cursor
from app.schemas import (
    ConversationAssignRequest,
    ConversationReplyRequest,
    CouponCreateRequest,
    CouponUpdateRequest,
    OrderConfirmRequest,
    OrderStatusRequest,
    PaginationResponse,
    ProductCreateRequest,
    ProductUpdateRequest,
    RefundRequest,
    ShippingConfigRequest,
    VariantRequest,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/conversations", response_model=PaginationResponse)
def list_conversations(
    _: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status"),
    assigned_agent: int | None = None,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
):
    cursor_id = parse_cursor(cursor)
    query = select(Conversation)
    if status_filter:
        query = query.where(Conversation.status == status_filter)
    if assigned_agent is not None:
        query = query.where(Conversation.assigned_agent_id == assigned_agent)
    if cursor_id is not None:
        query = query.where(Conversation.id > cursor_id)
    rows = db.scalars(query.order_by(Conversation.id.asc()).limit(limit + 1)).all()
    items = rows[:limit]
    has_more = len(rows) > limit
    return PaginationResponse(
        items=[{"id": c.id, "user_id": c.user_id, "status": c.status, "assigned_agent_id": c.assigned_agent_id} for c in items],
        next_cursor=next_cursor(items[-1].id if items else None, has_more),
    )


@router.get("/conversations/{id}/messages")
def conversation_messages(id: int, _: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    conversation = db.get(Conversation, id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = db.scalars(select(Message).where(Message.conversation_id == id).order_by(Message.id.asc())).all()
    return [{"id": m.id, "direction": m.direction, "body": m.body, "message_type": m.message_type, "created_at": m.created_at} for m in messages]


@router.post("/conversations/{id}/reply")
def conversation_reply(id: int, payload: ConversationReplyRequest, agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    conversation = db.get(Conversation, id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    message = Message(conversation_id=id, direction="outbound", body=payload.message, message_type="text")
    conversation.status = "human"
    conversation.assigned_agent_id = conversation.assigned_agent_id or agent.id
    db.add(message)
    db.commit()
    return {"id": message.id, "status": "sent"}


@router.post("/conversations/{id}/assign")
def conversation_assign(id: int, payload: ConversationAssignRequest, _: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    conversation = db.get(Conversation, id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    assigned = db.get(Agent, payload.agent_id)
    if not assigned:
        raise HTTPException(status_code=404, detail="Agent not found")
    conversation.assigned_agent_id = payload.agent_id
    db.commit()
    return {"status": "assigned", "agent_id": payload.agent_id}


@router.get("/users", response_model=PaginationResponse)
def list_users(
    _: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
    q: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
):
    cursor_id = parse_cursor(cursor)
    query = select(User).where(User.deleted_at.is_(None))
    if q:
        query = query.where(
            (User.name.ilike(f"%{q}%"))
            | (User.phone.ilike(f"%{q}%"))
            | (User.email.ilike(f"%{q}%"))
        )
    if cursor_id is not None:
        query = query.where(User.id > cursor_id)
    rows = db.scalars(query.order_by(User.id.asc()).limit(limit + 1)).all()
    items = rows[:limit]
    has_more = len(rows) > limit
    return PaginationResponse(
        items=[{"id": u.id, "name": u.name, "phone": u.phone, "email": u.email} for u in items],
        next_cursor=next_cursor(items[-1].id if items else None, has_more),
    )


@router.get("/users/{id}")
def user_detail(id: int, _: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    user = db.get(User, id)
    if not user or user.deleted_at is not None:
        raise HTTPException(status_code=404, detail="User not found")
    orders = db.scalars(select(Order).where(Order.user_id == id).order_by(Order.id.desc()).limit(50)).all()
    conversations = db.scalars(select(Conversation).where(Conversation.user_id == id).order_by(Conversation.id.desc()).limit(50)).all()
    return {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "email": user.email,
        "orders": [{"id": o.id, "order_number": o.order_number, "status": o.status, "total": str(o.total)} for o in orders],
        "conversations": [{"id": c.id, "status": c.status, "assigned_agent_id": c.assigned_agent_id} for c in conversations],
    }


@router.get("/orders", response_model=PaginationResponse)
def admin_orders(
    _: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status"),
    user_id: int | None = None,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
):
    cursor_id = parse_cursor(cursor)
    query = select(Order)
    if status_filter:
        query = query.where(Order.status == status_filter)
    if user_id is not None:
        query = query.where(Order.user_id == user_id)
    if cursor_id is not None:
        query = query.where(Order.id > cursor_id)
    rows = db.scalars(query.order_by(Order.id.asc()).limit(limit + 1)).all()
    items = rows[:limit]
    has_more = len(rows) > limit
    return PaginationResponse(
        items=[{"id": o.id, "order_number": o.order_number, "status": o.status, "total": str(o.total), "user_id": o.user_id} for o in items],
        next_cursor=next_cursor(items[-1].id if items else None, has_more),
    )


@router.get("/orders/{id}")
def admin_order_detail(id: int, _: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    order = db.get(Order, id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    items = db.scalars(select(OrderItem).where(OrderItem.order_id == id)).all()
    return {
        "id": order.id,
        "order_number": order.order_number,
        "status": order.status,
        "payment": None,
        "refunds": [],
        "items": [{"product_id": i.product_id, "quantity": i.quantity, "unit_price": str(i.unit_price)} for i in items],
    }


@router.post("/orders/{id}/confirm")
def confirm_order(id: int, payload: OrderConfirmRequest, _: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    order = db.get(Order, id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    order.status = "processing"
    order.delivery_eta = payload.delivery_eta
    order.tracking_number = payload.tracking_number
    order.tracking_url = payload.tracking_url
    db.commit()
    return {"status": order.status}


@router.post("/orders/{id}/update-status")
def update_order_status(id: int, payload: OrderStatusRequest, _: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    order = db.get(Order, id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    allowed = {"processing", "shipped", "out_for_delivery", "delivered"}
    if payload.status not in allowed:
        raise HTTPException(status_code=400, detail="Invalid status")
    order.status = payload.status
    if payload.tracking_number:
        order.tracking_number = payload.tracking_number
    db.commit()
    return {"status": order.status}


@router.post("/orders/{id}/refund")
def refund_order(id: int, payload: RefundRequest, _: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    order = db.get(Order, id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    refund = Refund(order_id=order.id, payment_id=1, amount=payload.amount, status="pending", reason=payload.reason)
    db.add(refund)
    db.commit()
    return {"id": refund.id, "status": refund.status}


@router.get("/products", response_model=PaginationResponse)
def admin_products(_: Agent = Depends(get_current_agent), db: Session = Depends(get_db), cursor: str | None = None, limit: int = Query(default=20, ge=1, le=100)):
    cursor_id = parse_cursor(cursor)
    query = select(Product)
    if cursor_id is not None:
        query = query.where(Product.id > cursor_id)
    rows = db.scalars(query.order_by(Product.id.asc()).limit(limit + 1)).all()
    items = rows[:limit]
    has_more = len(rows) > limit
    return PaginationResponse(items=[{"id": p.id, "name": p.name, "active": p.active, "deleted_at": p.deleted_at.isoformat() if p.deleted_at else None} for p in items], next_cursor=next_cursor(items[-1].id if items else None, has_more))


@router.post("/products")
def create_product(payload: ProductCreateRequest, _: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    product = Product(name=payload.name, description=payload.description, base_price=payload.base_price, category_id=payload.category_id, active=True)
    db.add(product)
    db.commit()
    return {"id": product.id}


@router.put("/products/{id}")
def update_product(id: int, payload: ProductUpdateRequest, _: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    product = db.get(Product, id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    updates = payload.model_dump(exclude_none=True)
    for key, value in updates.items():
        setattr(product, key, value)
    db.commit()
    return {"id": product.id}


@router.delete("/products/{id}")
def delete_product(id: int, _: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    product = db.get(Product, id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    product.deleted_at = datetime.utcnow()
    product.active = False
    db.commit()
    return {"status": "deleted"}


@router.post("/products/import")
def import_products(_: Agent = Depends(require_admin)):
    return {"status": "accepted"}


@router.post("/products/{id}/variants")
def add_variant(id: int, payload: VariantRequest, _: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    product = db.get(Product, id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    variant = ProductVariant(product_id=id, sku=payload.sku, name=payload.name, attributes=payload.attributes, price_override=payload.price_override, stock=payload.stock)
    db.add(variant)
    db.commit()
    return {"id": variant.id}


@router.put("/products/{id}/variants/{vid}")
def update_variant(id: int, vid: int, payload: VariantRequest, _: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    variant = db.get(ProductVariant, vid)
    if not variant or variant.product_id != id:
        raise HTTPException(status_code=404, detail="Variant not found")
    variant.sku = payload.sku
    variant.name = payload.name
    variant.attributes = payload.attributes
    variant.price_override = payload.price_override
    variant.stock = payload.stock
    db.commit()
    return {"id": variant.id}


@router.get("/coupons", response_model=PaginationResponse)
def list_coupons(_: Agent = Depends(require_admin), db: Session = Depends(get_db), cursor: str | None = None, limit: int = Query(default=20, ge=1, le=100)):
    cursor_id = parse_cursor(cursor)
    query = select(Coupon)
    if cursor_id is not None:
        query = query.where(Coupon.id > cursor_id)
    rows = db.scalars(query.order_by(Coupon.id.asc()).limit(limit + 1)).all()
    items = rows[:limit]
    has_more = len(rows) > limit
    return PaginationResponse(items=[{"id": c.id, "code": c.code, "active": c.active} for c in items], next_cursor=next_cursor(items[-1].id if items else None, has_more))


@router.post("/coupons")
def create_coupon(payload: CouponCreateRequest, _: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    coupon = Coupon(
        code=payload.code,
        discount_type=payload.discount_type,
        discount_value=payload.discount_value,
        min_order=payload.min_order,
        max_uses=payload.max_uses,
        per_user_limit=payload.per_user_limit,
        active=True,
    )
    db.add(coupon)
    db.commit()
    return {"id": coupon.id}


@router.put("/coupons/{id}")
def update_coupon(id: int, payload: CouponUpdateRequest, _: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    coupon = db.get(Coupon, id)
    if not coupon:
        raise HTTPException(status_code=404, detail="Coupon not found")
    if payload.active is not None:
        coupon.active = payload.active
    if payload.discount_value is not None:
        coupon.discount_value = payload.discount_value
    db.commit()
    return {"id": coupon.id}


@router.get("/coupons/{id}/usage")
def coupon_usage(id: int, _: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(CouponUsage).where(CouponUsage.coupon_id == id).order_by(CouponUsage.used_at.desc())).all()
    return [{"id": r.id, "user_id": r.user_id, "order_id": r.order_id, "used_at": r.used_at} for r in rows]


@router.get("/reviews", response_model=PaginationResponse)
def admin_reviews(
    _: Agent = Depends(require_admin),
    db: Session = Depends(get_db),
    rating: int | None = None,
    product_id: int | None = None,
    flagged: bool | None = None,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
):
    cursor_id = parse_cursor(cursor)
    query = select(Review).where(Review.deleted_at.is_(None))
    if rating is not None:
        query = query.where(Review.rating == rating)
    if product_id is not None:
        query = query.where(Review.product_id == product_id)
    if flagged is not None:
        query = query.where(Review.rating <= 2 if flagged else Review.rating >= 3)
    if cursor_id is not None:
        query = query.where(Review.id > cursor_id)
    rows = db.scalars(query.order_by(Review.id.asc()).limit(limit + 1)).all()
    items = rows[:limit]
    has_more = len(rows) > limit
    return PaginationResponse(items=[{"id": r.id, "product_id": r.product_id, "rating": r.rating, "comment": r.comment} for r in items], next_cursor=next_cursor(items[-1].id if items else None, has_more))


@router.delete("/reviews/{id}")
def delete_review(id: int, _: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    review = db.get(Review, id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    review.deleted_at = datetime.utcnow()
    db.commit()
    return {"status": "deleted"}


@router.get("/shipping/config")
def shipping_config_list(_: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(ShippingConfig).order_by(ShippingConfig.id.asc())).all()
    return [{"id": r.id, "zone": r.zone, "base_cost": str(r.base_cost), "active": r.active} for r in rows]


@router.post("/shipping/config")
def shipping_config_create(payload: ShippingConfigRequest, _: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    row = ShippingConfig(
        zone=payload.zone,
        min_weight=payload.min_weight,
        max_weight=payload.max_weight,
        base_cost=payload.base_cost,
        free_above_amount=payload.free_above_amount,
        active=True,
    )
    db.add(row)
    db.commit()
    return {"id": row.id}


@router.put("/shipping/config/{id}")
def shipping_config_update(id: int, payload: ShippingConfigRequest, _: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    row = db.get(ShippingConfig, id)
    if not row:
        raise HTTPException(status_code=404, detail="Shipping config not found")
    row.zone = payload.zone
    row.min_weight = payload.min_weight
    row.max_weight = payload.max_weight
    row.base_cost = payload.base_cost
    row.free_above_amount = payload.free_above_amount
    db.commit()
    return {"id": row.id}


@router.post("/shipping/serviceable-pincodes")
def upload_serviceable_pincodes(_: Agent = Depends(require_admin)):
    return {"status": "accepted"}


@router.get("/notifications")
def notifications(agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    rows = db.scalars(select(Notification).where(Notification.agent_id == agent.id).order_by(Notification.read.asc(), Notification.id.desc()).limit(100)).all()
    return [{"id": n.id, "type": n.type, "title": n.title, "message": n.message, "read": n.read} for n in rows]


@router.put("/notifications/{id}/read")
def mark_notification_read(id: int, agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    notification = db.get(Notification, id)
    if not notification or notification.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.read = True
    db.commit()
    return {"status": "read"}


@router.put("/notifications/read-all")
def mark_all_read(agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    rows = db.scalars(select(Notification).where(Notification.agent_id == agent.id, Notification.read.is_(False))).all()
    for row in rows:
        row.read = True
    db.commit()
    return {"updated": len(rows)}


@router.get("/dashboard/stats")
def dashboard_stats(_: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    total_orders = db.scalar(select(func.count(Order.id))) or 0
    revenue = db.scalar(select(func.coalesce(func.sum(Order.total), 0))) or Decimal("0")
    total_users = db.scalar(select(func.count(User.id))) or 0
    delivered = db.scalar(select(func.count(Order.id)).where(Order.status == "delivered")) or 0
    conversion_rate = (float(delivered) / float(total_orders) * 100.0) if total_orders else 0.0
    return {
        "orders": total_orders,
        "revenue": str(revenue),
        "users": total_users,
        "conversion_rate": round(conversion_rate, 2),
        "popular_products": [],
        "ai_vs_human_resolution_rate": {"ai": 0, "human": 0},
    }


@router.get("/dashboard/orders-chart")
def orders_chart(_: Agent = Depends(get_current_agent), db: Session = Depends(get_db), granularity: str = "daily"):
    rows = db.scalars(select(Order).order_by(Order.id.asc()).limit(2000)).all()
    buckets: dict[str, int] = {}
    for row in rows:
        key = row.order_number[:8] if granularity == "monthly" else str(row.id)
        buckets[key] = buckets.get(key, 0) + 1
    return {"granularity": granularity, "data": [{"bucket": k, "count": v} for k, v in buckets.items()]}
