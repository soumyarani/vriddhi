from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.cache import cache_delete, cache_get_json, cache_set_json
from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import (
    Address,
    Cart,
    CartItem,
    Category,
    Coupon,
    CouponUsage,
    InventoryReservation,
    Order,
    OrderItem,
    Payment,
    Product,
    ProductVariant,
    RefreshToken,
    Review,
    ShippingConfig,
    User,
    Wishlist,
)
from app.pagination import next_cursor, parse_cursor
from app.rate_limit import RequestRateLimiter
from app.schemas import (
    AddressIn,
    CartItemCreate,
    CartItemUpdate,
    CheckoutRequest,
    CheckoutRetryRequest,
    CouponApplyRequest,
    OrderStatusRequest,
    PaginationResponse,
    ProfileUpdateRequest,
    ProductDetailOut,
    ProductOut,
    ReviewCreateRequest,
    ShippingEstimateResponse,
    UserProfile,
    WhatsAppOptInRequest,
)

router = APIRouter(prefix="/api/store", tags=["store"])
coupon_rate_limiter = RequestRateLimiter(max_requests=settings.coupon_rate_limit_per_minute, window_seconds=60)


def _as_decimal(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _get_or_create_cart(db: Session, user_id: int) -> Cart:
    cart = db.scalar(select(Cart).where(Cart.user_id == user_id, Cart.status == "active"))
    if cart:
        return cart
    cart = Cart(user_id=user_id, status="active", expires_at=datetime.utcnow() + timedelta(hours=24))
    db.add(cart)
    db.flush()
    return cart


def _line_price(db: Session, item: CartItem) -> Decimal:
    product = db.get(Product, item.product_id)
    if not product:
        return Decimal("0")
    price = _as_decimal(product.base_price)
    if item.variant_id:
        variant = db.get(ProductVariant, item.variant_id)
        if variant and variant.price_override is not None:
            price = _as_decimal(variant.price_override)
    return price * Decimal(item.quantity)


def _cart_summary(db: Session, cart: Cart) -> dict:
    items = db.scalars(select(CartItem).where(CartItem.cart_id == cart.id)).all()
    subtotal = sum((_line_price(db, item) for item in items), Decimal("0"))
    discount = Decimal("0")
    if cart.coupon_id:
        coupon = db.get(Coupon, cart.coupon_id)
        if coupon and coupon.active:
            if coupon.discount_type == "percent":
                discount = subtotal * (_as_decimal(coupon.discount_value) / Decimal("100"))
            else:
                discount = _as_decimal(coupon.discount_value)
            if coupon.max_discount_amount:
                discount = min(discount, _as_decimal(coupon.max_discount_amount))
    total = max(subtotal - discount, Decimal("0"))
    return {
        "id": cart.id,
        "status": cart.status,
        "coupon_id": cart.coupon_id,
        "subtotal": str(subtotal.quantize(Decimal("0.01"))),
        "discount": str(discount.quantize(Decimal("0.01"))),
        "total": str(total.quantize(Decimal("0.01"))),
        "items": [
            {"id": i.id, "product_id": i.product_id, "variant_id": i.variant_id, "quantity": i.quantity, "line_total": str(_line_price(db, i).quantize(Decimal('0.01')))}
            for i in items
        ],
    }


@router.get("/categories")
def list_categories(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    cached = cache_get_json("store:categories")
    if cached and offset == 0 and limit == 50:
        return cached
    categories = db.scalars(
        select(Category)
        .where(Category.active.is_(True), Category.deleted_at.is_(None))
        .order_by(Category.sort_order.asc(), Category.id.asc())
        .offset(offset)
        .limit(limit)
    ).all()
    data = [{"id": c.id, "name": c.name, "description": c.description, "image_url": c.image_url, "sort_order": c.sort_order} for c in categories]
    if offset == 0 and limit == 50:
        cache_set_json("store:categories", data, 300)
    return data


@router.get("/products", response_model=PaginationResponse)
def list_products(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    category_id: int | None = None,
    q: str | None = None,
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
):
    cursor_id = parse_cursor(cursor)
    query = select(Product).where(Product.active.is_(True), Product.deleted_at.is_(None))
    if category_id is not None:
        query = query.where(Product.category_id == category_id)
    if q:
        query = query.where(or_(Product.name.ilike(f"%{q}%"), Product.description.ilike(f"%{q}%")))
    if min_price is not None:
        query = query.where(Product.base_price >= min_price)
    if max_price is not None:
        query = query.where(Product.base_price <= max_price)
    if cursor_id is not None:
        query = query.where(Product.id > cursor_id)
    rows = db.scalars(query.order_by(Product.id.asc()).limit(limit + 1)).all()
    items = rows[:limit]
    has_more = len(rows) > limit
    return PaginationResponse(
        items=[{"id": p.id, "name": p.name, "description": p.description, "category_id": p.category_id, "base_price": str(p.base_price)} for p in items],
        next_cursor=next_cursor(items[-1].id if items else None, has_more),
    )


@router.get("/products/{id}", response_model=ProductDetailOut)
def get_product(id: int, _: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cache_key = f"store:product:{id}"
    cached = cache_get_json(cache_key)
    if cached:
        return cached

    product = db.get(Product, id)
    if not product or not product.active or product.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Product not found")
    variants = db.scalars(select(ProductVariant).where(ProductVariant.product_id == id)).all()
    reviews = db.scalars(select(Review).where(Review.product_id == id, Review.deleted_at.is_(None)).order_by(Review.id.desc()).limit(20)).all()
    payload = {
        "id": product.id,
        "name": product.name,
        "description": product.description,
        "category_id": product.category_id,
        "base_price": str(product.base_price),
        "variants": [
            {
                "id": v.id,
                "sku": v.sku,
                "name": v.name,
                "attributes": v.attributes,
                "price_override": str(v.price_override) if v.price_override is not None else None,
                "stock": v.stock,
            }
            for v in variants
        ],
        "reviews": [{"id": r.id, "user_id": r.user_id, "rating": r.rating, "comment": r.comment} for r in reviews],
    }
    cache_set_json(cache_key, payload, 120)
    return payload


@router.get("/cart")
def get_cart(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cart = _get_or_create_cart(db, current_user.id)
    db.commit()
    return _cart_summary(db, cart)


@router.post("/cart/items")
def add_cart_item(payload: CartItemCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cart = _get_or_create_cart(db, current_user.id)
    if payload.variant_id:
        variant = db.get(ProductVariant, payload.variant_id)
        if not variant or variant.product_id != payload.product_id:
            raise HTTPException(status_code=400, detail="Invalid variant")
        if variant.stock < payload.quantity:
            raise HTTPException(status_code=400, detail="Insufficient stock")

    existing = db.scalar(
        select(CartItem).where(
            CartItem.cart_id == cart.id,
            CartItem.product_id == payload.product_id,
            CartItem.variant_id.is_(payload.variant_id) if payload.variant_id is None else CartItem.variant_id == payload.variant_id,
        )
    )
    if existing:
        existing.quantity += payload.quantity
    else:
        db.add(CartItem(cart_id=cart.id, product_id=payload.product_id, variant_id=payload.variant_id, quantity=payload.quantity))
    db.commit()
    return _cart_summary(db, cart)


@router.put("/cart/items/{id}")
def update_cart_item(id: int, payload: CartItemUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cart = _get_or_create_cart(db, current_user.id)
    item = db.get(CartItem, id)
    if not item or item.cart_id != cart.id:
        raise HTTPException(status_code=404, detail="Cart item not found")
    if item.variant_id:
        variant = db.get(ProductVariant, item.variant_id)
        if variant and variant.stock < payload.quantity:
            raise HTTPException(status_code=400, detail="Insufficient stock")
    item.quantity = payload.quantity
    db.commit()
    return _cart_summary(db, cart)


@router.delete("/cart/items/{id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_cart_item(id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cart = _get_or_create_cart(db, current_user.id)
    item = db.get(CartItem, id)
    if item and item.cart_id == cart.id:
        db.delete(item)
        db.commit()


@router.post("/cart/apply-coupon")
def apply_coupon(payload: CouponApplyRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not coupon_rate_limiter.allow(f"coupon:{current_user.id}"):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    cart = _get_or_create_cart(db, current_user.id)
    coupon = db.scalar(select(Coupon).where(func.lower(Coupon.code) == payload.code.lower(), Coupon.deleted_at.is_(None)))
    if not coupon or not coupon.active:
        raise HTTPException(status_code=400, detail="Invalid coupon")
    if coupon.expires_at and coupon.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Coupon expired")

    usage_count = db.scalar(select(func.count(CouponUsage.id)).where(CouponUsage.coupon_id == coupon.id)) or 0
    if coupon.max_uses is not None and usage_count >= coupon.max_uses:
        raise HTTPException(status_code=400, detail="Coupon usage exceeded")
    cart.coupon_id = coupon.id
    db.commit()
    return _cart_summary(db, cart)


@router.delete("/cart/coupon")
def remove_coupon(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cart = _get_or_create_cart(db, current_user.id)
    cart.coupon_id = None
    db.commit()
    return _cart_summary(db, cart)


@router.get("/shipping/estimate", response_model=ShippingEstimateResponse)
def estimate_shipping(pincode: str, amount: Decimal = Decimal("0"), db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    cfg = db.scalar(select(ShippingConfig).where(ShippingConfig.active.is_(True)).order_by(ShippingConfig.id.asc()))
    if not cfg:
        return ShippingEstimateResponse(pincode=pincode, serviceable=False, shipping_cost=Decimal("0"))
    shipping_cost = _as_decimal(cfg.base_cost)
    if cfg.free_above_amount and amount >= _as_decimal(cfg.free_above_amount):
        shipping_cost = Decimal("0")
    return ShippingEstimateResponse(pincode=pincode, serviceable=True, shipping_cost=shipping_cost)


@router.post("/checkout")
def checkout(payload: CheckoutRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cart = _get_or_create_cart(db, current_user.id)
    items = db.scalars(select(CartItem).where(CartItem.cart_id == cart.id)).all()
    if not items:
        raise HTTPException(status_code=400, detail="Cart is empty")

    address = db.get(Address, payload.address_id)
    if not address or address.user_id != current_user.id or not address.is_serviceable:
        raise HTTPException(status_code=400, detail="Invalid address")

    subtotal = sum((_line_price(db, item) for item in items), Decimal("0"))
    shipping_cfg = db.scalar(select(ShippingConfig).where(ShippingConfig.active.is_(True)).order_by(ShippingConfig.id.asc()))
    shipping_cost = _as_decimal(shipping_cfg.base_cost) if shipping_cfg else Decimal("0")
    tax_amount = (subtotal * Decimal("0.18")).quantize(Decimal("0.01"))
    discount = Decimal("0")
    applied_coupon = None
    if payload.coupon_code:
        applied_coupon = db.scalar(select(Coupon).where(func.lower(Coupon.code) == payload.coupon_code.lower(), Coupon.active.is_(True), Coupon.deleted_at.is_(None)))
        if applied_coupon:
            discount = _as_decimal(applied_coupon.discount_value)
            if applied_coupon.discount_type == "percent":
                discount = subtotal * (discount / Decimal("100"))
    total = max(subtotal + shipping_cost + tax_amount - discount, Decimal("0"))

    order_number = f"ORD-{datetime.utcnow().year}-{int(datetime.utcnow().timestamp() * 1000)}"
    order = Order(
        order_number=order_number,
        user_id=current_user.id,
        address_snapshot={
            "line1": address.line1,
            "line2": address.line2,
            "city": address.city,
            "state": address.state,
            "pincode": address.pincode,
            "country": address.country,
        },
        subtotal=subtotal,
        shipping_cost=shipping_cost,
        tax_amount=tax_amount,
        tax_breakup={"gst": str(tax_amount)},
        discount_amount=discount,
        total=total,
        status="pending_payment",
    )
    db.add(order)
    db.flush()

    for item in items:
        product = db.get(Product, item.product_id)
        variant = db.get(ProductVariant, item.variant_id) if item.variant_id else None
        unit = _line_price(db, CartItem(cart_id=0, product_id=item.product_id, variant_id=item.variant_id, quantity=1))
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=item.product_id,
                variant_id=item.variant_id,
                product_name=product.name if product else "Unknown",
                variant_name=variant.name if variant else None,
                quantity=item.quantity,
                unit_price=unit,
                tax_rate=Decimal("18"),
                tax_amount=(unit * Decimal(item.quantity) * Decimal("0.18")).quantize(Decimal("0.01")),
            )
        )
        if variant:
            if variant.stock < item.quantity:
                raise HTTPException(status_code=400, detail="Insufficient stock")
            variant.stock -= item.quantity
            db.add(
                InventoryReservation(
                    cart_id=cart.id,
                    variant_id=variant.id,
                    quantity=item.quantity,
                    expires_at=datetime.utcnow() + timedelta(minutes=settings.payment_link_ttl_minutes),
                )
            )

    if applied_coupon:
        cart.coupon_id = applied_coupon.id

    payment = Payment(
        order_id=order.id,
        cashfree_order_id=f"cf_{order.order_number}",
        amount=total,
        status="pending",
        payment_link=f"https://payments.example/{order.order_number}",
        expires_at=datetime.utcnow() + timedelta(minutes=settings.payment_link_ttl_minutes),
        idempotency_key=f"checkout-{order.id}-{int(datetime.utcnow().timestamp())}",
    )
    db.add(payment)

    for item in items:
        db.delete(item)

    db.commit()
    return {
        "order_id": order.id,
        "order_number": order.order_number,
        "status": order.status,
        "subtotal": str(subtotal),
        "shipping_cost": str(shipping_cost),
        "tax_amount": str(tax_amount),
        "discount_amount": str(discount),
        "total": str(total),
        "payment_link": payment.payment_link,
        "payment_expires_at": payment.expires_at,
    }


@router.post("/checkout/retry")
def retry_checkout(payload: CheckoutRetryRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = db.get(Order, payload.order_id)
    if not order or order.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Order not found")

    payment = db.scalar(select(Payment).where(Payment.order_id == order.id).order_by(Payment.id.desc()))
    if payment and payment.status == "paid":
        raise HTTPException(status_code=400, detail="Order already paid")

    if payment:
        payment.status = "expired"
    new_payment = Payment(
        order_id=order.id,
        cashfree_order_id=f"cf_{order.order_number}-retry-{int(datetime.utcnow().timestamp())}",
        amount=order.total,
        status="pending",
        payment_link=f"https://payments.example/{order.order_number}?retry=1",
        expires_at=datetime.utcnow() + timedelta(minutes=settings.payment_link_ttl_minutes),
        idempotency_key=f"retry-{order.id}-{int(datetime.utcnow().timestamp())}",
    )
    db.add(new_payment)
    db.commit()
    return {"order_id": order.id, "payment_link": new_payment.payment_link, "expires_at": new_payment.expires_at}


@router.get("/orders", response_model=PaginationResponse)
def list_orders(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status"),
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
):
    cursor_id = parse_cursor(cursor)
    query = select(Order).where(Order.user_id == current_user.id)
    if status_filter:
        query = query.where(Order.status == status_filter)
    if cursor_id is not None:
        query = query.where(Order.id > cursor_id)
    rows = db.scalars(query.order_by(Order.id.asc()).limit(limit + 1)).all()
    items = rows[:limit]
    has_more = len(rows) > limit
    return PaginationResponse(
        items=[
            {
                "id": o.id,
                "order_number": o.order_number,
                "status": o.status,
                "total": str(o.total),
            }
            for o in items
        ],
        next_cursor=next_cursor(items[-1].id if items else None, has_more),
    )


@router.get("/orders/{id}")
def order_detail(id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = db.get(Order, id)
    if not order or order.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Order not found")
    items = db.scalars(select(OrderItem).where(OrderItem.order_id == order.id)).all()
    return {
        "id": order.id,
        "order_number": order.order_number,
        "status": order.status,
        "tracking_number": order.tracking_number,
        "tracking_url": order.tracking_url,
        "items": [
            {
                "id": i.id,
                "product_id": i.product_id,
                "product_name": i.product_name,
                "quantity": i.quantity,
                "unit_price": str(i.unit_price),
            }
            for i in items
        ],
    }


@router.post("/orders/{id}/cancel")
def cancel_order(id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = db.get(Order, id)
    if not order or order.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status in {"shipped", "out_for_delivery", "delivered"}:
        raise HTTPException(status_code=400, detail="Order cannot be cancelled")
    order.status = "cancelled"
    order.cancelled_at = datetime.utcnow()
    db.commit()
    return {"status": "cancelled"}


@router.post("/orders/{id}/return")
def request_return(id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = db.get(Order, id)
    if not order or order.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != "delivered":
        raise HTTPException(status_code=400, detail="Return allowed only after delivery")
    order.status = "return_requested"
    db.commit()
    return {"status": "return_requested"}


@router.get("/orders/{id}/invoice")
def invoice(id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = db.get(Order, id)
    if not order or order.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Order not found")
    content = f"Invoice for {order.order_number}\nTotal: {order.total}\nGST: {order.tax_amount}\n"
    return Response(content=content.encode("utf-8"), media_type="application/pdf")


@router.post("/orders/{id}/review")
def submit_review(id: int, payload: ReviewCreateRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = db.get(Order, id)
    if not order or order.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != "delivered":
        raise HTTPException(status_code=400, detail="Reviews allowed only for delivered orders")

    item_exists = db.scalar(select(OrderItem.id).where(OrderItem.order_id == id, OrderItem.product_id == payload.product_id))
    if not item_exists:
        raise HTTPException(status_code=400, detail="Product not part of this order")

    existing = db.scalar(select(Review).where(Review.order_id == id, Review.product_id == payload.product_id, Review.user_id == current_user.id, Review.deleted_at.is_(None)))
    if existing:
        raise HTTPException(status_code=400, detail="Review already submitted")

    review = Review(
        user_id=current_user.id,
        product_id=payload.product_id,
        order_id=id,
        rating=payload.rating,
        comment=payload.comment,
        verified_purchase=True,
    )
    db.add(review)
    db.commit()
    cache_delete(f"store:product:{payload.product_id}")
    return {"id": review.id, "rating": review.rating, "comment": review.comment}


@router.get("/products/{id}/reviews", response_model=PaginationResponse)
def list_product_reviews(id: int, _: User = Depends(get_current_user), db: Session = Depends(get_db), cursor: str | None = None, limit: int = Query(default=20, ge=1, le=100)):
    cursor_id = parse_cursor(cursor)
    query = select(Review).where(Review.product_id == id, Review.deleted_at.is_(None))
    if cursor_id is not None:
        query = query.where(Review.id > cursor_id)
    rows = db.scalars(query.order_by(Review.id.asc()).limit(limit + 1)).all()
    items = rows[:limit]
    has_more = len(rows) > limit
    return PaginationResponse(items=[{"id": r.id, "user_id": r.user_id, "rating": r.rating, "comment": r.comment} for r in items], next_cursor=next_cursor(items[-1].id if items else None, has_more))


@router.get("/profile", response_model=UserProfile)
def get_profile(current_user: User = Depends(get_current_user)):
    return UserProfile.model_validate(current_user)


@router.put("/profile", response_model=UserProfile)
def update_profile(payload: ProfileUpdateRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if payload.name is not None:
        current_user.name = payload.name
    if payload.gstin is not None:
        current_user.gstin = payload.gstin
    db.commit()
    db.refresh(current_user)
    return UserProfile.model_validate(current_user)


@router.get("/profile/addresses")
def list_addresses(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    addresses = db.scalars(select(Address).where(Address.user_id == current_user.id)).all()
    return [
        {
            "id": a.id,
            "label": a.label,
            "line1": a.line1,
            "line2": a.line2,
            "city": a.city,
            "state": a.state,
            "pincode": a.pincode,
            "country": a.country,
            "is_default": a.is_default,
            "is_serviceable": a.is_serviceable,
        }
        for a in addresses
    ]


@router.post("/profile/addresses")
def add_address(payload: AddressIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    is_serviceable = len(payload.pincode) >= 6
    address = Address(
        user_id=current_user.id,
        label=payload.label,
        line1=payload.line1,
        line2=payload.line2,
        city=payload.city,
        state=payload.state,
        pincode=payload.pincode,
        country=payload.country,
        is_default=payload.is_default,
        is_serviceable=is_serviceable,
    )
    db.add(address)
    db.commit()
    return {"id": address.id, "is_serviceable": address.is_serviceable}


@router.put("/profile/addresses/{id}")
def update_address(id: int, payload: AddressIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    address = db.get(Address, id)
    if not address or address.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Address not found")
    address.label = payload.label
    address.line1 = payload.line1
    address.line2 = payload.line2
    address.city = payload.city
    address.state = payload.state
    address.pincode = payload.pincode
    address.country = payload.country
    address.is_default = payload.is_default
    address.is_serviceable = len(payload.pincode) >= 6
    db.commit()
    return {"id": address.id, "is_serviceable": address.is_serviceable}


@router.delete("/profile/addresses/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_address(id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    address = db.get(Address, id)
    if address and address.user_id == current_user.id:
        db.delete(address)
        db.commit()


@router.put("/profile/whatsapp-opt-in")
def whatsapp_opt_in(payload: WhatsAppOptInRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    current_user.whatsapp_opt_in = payload.whatsapp_opt_in
    db.commit()
    return {"whatsapp_opt_in": current_user.whatsapp_opt_in}


@router.get("/wishlist", response_model=PaginationResponse)
def list_wishlist(current_user: User = Depends(get_current_user), db: Session = Depends(get_db), cursor: str | None = None, limit: int = Query(default=20, ge=1, le=100)):
    cursor_id = parse_cursor(cursor)
    query = select(Wishlist).where(Wishlist.user_id == current_user.id)
    if cursor_id is not None:
        query = query.where(Wishlist.id > cursor_id)
    rows = db.scalars(query.order_by(Wishlist.id.asc()).limit(limit + 1)).all()
    items = rows[:limit]
    has_more = len(rows) > limit
    return PaginationResponse(
        items=[{"id": w.id, "product_id": w.product_id, "created_at": w.created_at.isoformat()} for w in items],
        next_cursor=next_cursor(items[-1].id if items else None, has_more),
    )


@router.post("/wishlist/{product_id}")
def add_wishlist(product_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    exists = db.scalar(select(Wishlist).where(Wishlist.user_id == current_user.id, Wishlist.product_id == product_id))
    if not exists:
        db.add(Wishlist(user_id=current_user.id, product_id=product_id))
        db.commit()
    return {"status": "ok"}


@router.delete("/wishlist/{product_id}")
def remove_wishlist(product_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.scalar(select(Wishlist).where(Wishlist.user_id == current_user.id, Wishlist.product_id == product_id))
    if row:
        db.delete(row)
        db.commit()
    return {"status": "ok"}
