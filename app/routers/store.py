"""Customer-facing storefront API.

Thin HTTP layer over ``app/services``: parse, delegate, commit, return. All
ownership checks live in the services (an order or address that belongs to
someone else raises ``NotFoundError``, not 403, so IDs cannot be probed) — no
endpoint here reaches past them into the ORM.

``get_db`` does not commit, so every mutating endpoint commits explicitly.
"""

# NOTE: `from __future__ import annotations` is deliberately absent. The slowapi
# limiter decorator wraps endpoints with functools.wraps, which keeps slowapi's
# module globals on the wrapper, so FastAPI cannot resolve string annotations
# back to these schema classes.
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Body, Query, Request, Response
from fastapi.responses import Response as RawResponse

from app.config import settings
from app.dependencies import CurrentUser, DbSession, OptionalUser, PageLimit
from app.rate_limit import limiter
from app.schemas.cart import (
    ApplyCouponRequest,
    CartItemAdd,
    CartItemUpdate,
    CartOut,
    CheckoutRequest,
    CheckoutResponse,
    ShippingEstimate,
    ShippingEstimateRequest,
)
from app.schemas.common import CUSTOMER_RESPONSES, MessageResponse, Page
from app.schemas.order import (
    CancelOrderRequest,
    OrderDetail,
    OrderSummary,
    ReturnOrderRequest,
)
from app.schemas.product import CategoryOut, ProductDetail, ProductFilters, ProductSummary
from app.schemas.review import ReviewOut, WishlistItemOut
from app.schemas.user import AddressCreate, AddressOut, AddressUpdate, ProfileUpdate, WhatsAppOptIn
from app.schemas.auth import UserProfile
from app.services import cart as cart_service
from app.services import order as order_service
from app.services import product as product_service
from app.services import review as review_service
from app.services import shipping as shipping_service
from app.services import user as user_service
from app.services.invoice import generate_invoice, invoice_filename

# Catalogue reads are public and so cannot 401, but documenting the customer set
# router-wide is the honest trade: the overwhelming majority of this surface is
# authenticated, and an extra documented 401 on a public GET is far less
# misleading than an undocumented 409 on checkout.
router = APIRouter(prefix="/api", tags=["store"], responses=CUSTOMER_RESPONSES)

STORE_LIMIT = settings.rate_limit_store
# Deliberately tighter than the storefront limit: a coupon code is a secret and
# an unthrottled apply endpoint is a code-enumeration oracle.
COUPON_LIMIT = settings.rate_limit_coupon


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------
@router.get("/categories", response_model=list[CategoryOut], summary="List categories")
@limiter.limit(STORE_LIMIT)
async def list_categories(request: Request, response: Response, db: DbSession) -> Any:
    return await product_service.list_categories(db)


@router.get("/products", response_model=Page[ProductSummary], summary="Browse products")
@limiter.limit(STORE_LIMIT)
async def list_products(
    request: Request,
    response: Response,
    db: DbSession,
    limit: PageLimit,
    cursor: str | None = Query(default=None, max_length=512),
    category_id: int | None = Query(default=None, ge=1),
    q: str | None = Query(default=None, max_length=200),
    min_price: Decimal | None = Query(default=None, ge=0),
    max_price: Decimal | None = Query(default=None, ge=0),
    min_rating: float | None = Query(default=None, ge=0, le=5),
    in_stock_only: bool = Query(default=False),
) -> Any:
    filters = ProductFilters(
        category_id=category_id,
        q=q,
        min_price=min_price,
        max_price=max_price,
        min_rating=min_rating,
        in_stock_only=in_stock_only,
    )
    return await product_service.list_products(db, filters, cursor=cursor, limit=limit)


@router.get(
    "/products/search", response_model=Page[ProductSummary], summary="Search products"
)
@limiter.limit(STORE_LIMIT)
async def search_products(
    request: Request,
    response: Response,
    db: DbSession,
    limit: PageLimit,
    q: str = Query(min_length=1, max_length=200),
    cursor: str | None = Query(default=None, max_length=512),
    category_id: int | None = Query(default=None, ge=1),
    in_stock_only: bool = Query(default=False),
) -> Any:
    filters = ProductFilters(q=q, category_id=category_id, in_stock_only=in_stock_only)
    return await product_service.list_products(db, filters, cursor=cursor, limit=limit)


@router.get(
    "/products/{product_id}", response_model=ProductDetail, summary="Product detail"
)
@limiter.limit(STORE_LIMIT)
async def get_product(
    request: Request, response: Response, product_id: int, db: DbSession
) -> Any:
    return await product_service.get_product_detail(db, product_id)


@router.get("/products/{product_id}/reviews", summary="Reviews for a product")
async def list_product_reviews(
    product_id: int, db: DbSession, limit: PageLimit,
    cursor: str | None = Query(default=None, max_length=512),
) -> dict[str, Any]:
    # No response_model: `review.serialize_review` returns a public projection
    # (`author`, no user_id/order_id) that does not match `ReviewOut`.
    return await review_service.list_product_reviews(db, product_id, cursor=cursor, limit=limit)


# --------------------------------------------------------------------------
# Cart
# --------------------------------------------------------------------------
@router.get("/cart", response_model=CartOut, summary="Current cart")
async def get_cart(user: CurrentUser, db: DbSession) -> Any:
    cart = await cart_service.get_or_create_cart(db, user.id)
    payload = await cart_service.serialize_cart(db, cart)
    await db.commit()
    return payload


@router.post("/cart/items", response_model=CartOut, summary="Add an item to the cart")
async def add_cart_item(body: CartItemAdd, user: CurrentUser, db: DbSession) -> Any:
    cart = await cart_service.add_item(
        db, user.id, body.product_id, body.variant_id, body.quantity
    )
    payload = await cart_service.serialize_cart(db, cart)
    await db.commit()
    return payload


@router.patch(
    "/cart/items/{item_id}", response_model=CartOut, summary="Change item quantity"
)
async def update_cart_item(
    item_id: int, body: CartItemUpdate, user: CurrentUser, db: DbSession
) -> Any:
    cart = await cart_service.update_item(db, user.id, item_id, body.quantity)
    payload = await cart_service.serialize_cart(db, cart)
    await db.commit()
    return payload


@router.delete(
    "/cart/items/{item_id}", response_model=CartOut, summary="Remove an item"
)
async def remove_cart_item(item_id: int, user: CurrentUser, db: DbSession) -> Any:
    cart = await cart_service.remove_item(db, user.id, item_id)
    payload = await cart_service.serialize_cart(db, cart)
    await db.commit()
    return payload


@router.delete("/cart", response_model=CartOut, summary="Empty the cart")
async def clear_cart(user: CurrentUser, db: DbSession) -> Any:
    cart = await cart_service.get_or_create_cart(db, user.id)
    await cart_service.clear_cart(db, cart)
    payload = await cart_service.serialize_cart(db, cart)
    await db.commit()
    return payload


@router.post("/cart/coupon", response_model=CartOut, summary="Apply a coupon")
@limiter.limit(COUPON_LIMIT)
async def apply_coupon(
    request: Request,
    response: Response,
    body: ApplyCouponRequest,
    user: CurrentUser,
    db: DbSession,
) -> Any:
    cart = await cart_service.apply_coupon(db, user.id, body.code)
    payload = await cart_service.serialize_cart(db, cart)
    await db.commit()
    return payload


@router.delete("/cart/coupon", response_model=CartOut, summary="Remove the coupon")
async def remove_coupon(user: CurrentUser, db: DbSession) -> Any:
    cart = await cart_service.remove_coupon(db, user.id)
    payload = await cart_service.serialize_cart(db, cart)
    await db.commit()
    return payload


# --------------------------------------------------------------------------
# Checkout
# --------------------------------------------------------------------------
def _checkout_payload(order: Any, payment: Any) -> dict[str, Any]:
    return {
        "order_id": order.id,
        "order_number": order.order_number,
        "total": order.total,
        "currency": order.currency,
        "payment_link": getattr(payment, "payment_link", None),
        "payment_session_id": getattr(payment, "payment_session_id", None),
        "payment_expires_at": getattr(payment, "expires_at", None),
        "status": order.status,
    }


@router.post(
    "/checkout",
    response_model=CheckoutResponse,
    status_code=201,
    summary="Place an order from the cart",
)
async def checkout(body: CheckoutRequest, user: CurrentUser, db: DbSession) -> Any:
    order, payment = await order_service.checkout(
        db,
        user,
        address_id=body.address_id,
        new_address=body.new_address,
        channel="web",
    )
    payload = _checkout_payload(order, payment)
    await db.commit()
    return payload


@router.post(
    "/orders/{order_id}/retry-payment",
    response_model=CheckoutResponse,
    summary="Issue a fresh payment link",
)
async def retry_payment(order_id: int, user: CurrentUser, db: DbSession) -> Any:
    order, payment = await order_service.retry_payment(db, user, order_id)
    payload = _checkout_payload(order, payment)
    await db.commit()
    return payload


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------
@router.get("/orders", response_model=Page[OrderSummary], summary="My orders")
async def list_orders(
    user: CurrentUser,
    db: DbSession,
    limit: PageLimit,
    cursor: str | None = Query(default=None, max_length=512),
    status: str | None = Query(default=None, max_length=40),
) -> Any:
    return await order_service.list_orders(
        db, user_id=user.id, status=status, cursor=cursor, limit=limit
    )


@router.get("/orders/{order_id}", response_model=OrderDetail, summary="Order detail")
async def get_order(order_id: int, user: CurrentUser, db: DbSession) -> Any:
    order = await order_service.get_user_order(db, user.id, order_id)
    return await order_service.serialize_order_detail(db, order)


@router.post(
    "/orders/{order_id}/cancel", response_model=OrderDetail, summary="Cancel an order"
)
async def cancel_order(
    order_id: int, body: CancelOrderRequest, user: CurrentUser, db: DbSession
) -> Any:
    order = await order_service.get_user_order(db, user.id, order_id)
    await order_service.cancel_order(db, order, body.reason)
    await db.commit()

    fresh = await order_service.get_user_order(db, user.id, order_id)
    return await order_service.serialize_order_detail(db, fresh)


@router.post(
    "/orders/{order_id}/return",
    response_model=OrderDetail,
    summary="Request a return",
)
async def request_return(
    order_id: int, body: ReturnOrderRequest, user: CurrentUser, db: DbSession
) -> Any:
    order = await order_service.get_user_order(db, user.id, order_id)
    await order_service.request_return(db, order, body.reason)
    await db.commit()

    fresh = await order_service.get_user_order(db, user.id, order_id)
    return await order_service.serialize_order_detail(db, fresh)


@router.get(
    "/orders/{order_id}/invoice",
    response_class=RawResponse,
    responses={200: {"content": {"application/pdf": {}}, "description": "Invoice PDF"}},
    summary="Download the GST invoice",
)
async def download_invoice(
    order_id: int, user: CurrentUser, db: DbSession
) -> RawResponse:
    # Ownership is resolved first so the PDF generator is never reached with
    # someone else's order id.
    order = await order_service.get_user_order(db, user.id, order_id)
    pdf = await generate_invoice(db, order.id)
    filename = invoice_filename(order.order_number)
    return RawResponse(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------
# Reviews
# --------------------------------------------------------------------------
@router.get("/reviews/pending", summary="Items I can still review")
async def pending_reviews(user: CurrentUser, db: DbSession) -> list[dict[str, Any]]:
    return await review_service.reviewable_items(db, user.id)


@router.get("/reviews/mine", summary="My reviews")
async def my_reviews(
    user: CurrentUser,
    db: DbSession,
    limit: PageLimit,
    cursor: str | None = Query(default=None, max_length=512),
) -> dict[str, Any]:
    return await review_service.list_user_reviews(db, user.id, cursor=cursor, limit=limit)


@router.post(
    "/orders/{order_id}/reviews",
    response_model=ReviewOut,
    status_code=201,
    summary="Review a delivered item",
)
async def create_review(
    order_id: int,
    user: CurrentUser,
    db: DbSession,
    product_id: int = Body(ge=1),
    rating: int = Body(ge=1, le=5),
    comment: str | None = Body(default=None, max_length=2000),
) -> Any:
    review = await review_service.create_review(
        db,
        user_id=user.id,
        product_id=product_id,
        order_id=order_id,
        rating=rating,
        comment=comment,
    )
    await db.commit()
    return review


@router.patch(
    "/reviews/{review_id}", response_model=ReviewOut, summary="Edit my review"
)
async def update_review(
    review_id: int,
    user: CurrentUser,
    db: DbSession,
    rating: int | None = Body(default=None, ge=1, le=5),
    comment: str | None = Body(default=None, max_length=2000),
) -> Any:
    review = await review_service.update_review(
        db, user.id, review_id, rating=rating, comment=comment
    )
    await db.commit()
    return review


@router.delete(
    "/reviews/{review_id}", response_model=MessageResponse, summary="Delete my review"
)
async def delete_review(review_id: int, user: CurrentUser, db: DbSession) -> Any:
    await review_service.delete_review(db, user.id, review_id)
    await db.commit()
    return MessageResponse(detail="Review deleted")


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------
@router.get("/profile", response_model=UserProfile, summary="My profile")
async def get_profile(user: CurrentUser, db: DbSession) -> Any:
    return user_service.serialize_user(user)


@router.patch("/profile", response_model=UserProfile, summary="Update my profile")
async def update_profile(body: ProfileUpdate, user: CurrentUser, db: DbSession) -> Any:
    updated = await user_service.update_profile(db, user.id, body)
    await db.commit()
    return user_service.serialize_user(updated)


@router.patch(
    "/profile/whatsapp-opt-in",
    response_model=UserProfile,
    summary="Toggle WhatsApp messaging",
)
async def set_whatsapp_opt_in(
    body: WhatsAppOptIn, user: CurrentUser, db: DbSession
) -> Any:
    updated = await user_service.update_profile(
        db, user.id, {"whatsapp_opt_in": body.opt_in}
    )
    await db.commit()
    return user_service.serialize_user(updated)


# --------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------
@router.get("/addresses", response_model=list[AddressOut], summary="My addresses")
async def list_addresses(user: CurrentUser, db: DbSession) -> Any:
    return await user_service.list_addresses(db, user.id)


@router.post(
    "/addresses",
    response_model=AddressOut,
    status_code=201,
    summary="Add an address",
)
async def create_address(body: AddressCreate, user: CurrentUser, db: DbSession) -> Any:
    address = await user_service.create_address(db, user.id, body)
    await db.commit()
    return address


@router.patch(
    "/addresses/{address_id}", response_model=AddressOut, summary="Edit an address"
)
async def update_address(
    address_id: int, body: AddressUpdate, user: CurrentUser, db: DbSession
) -> Any:
    address = await user_service.update_address(db, user.id, address_id, body)
    await db.commit()
    return address


@router.delete(
    "/addresses/{address_id}",
    response_model=MessageResponse,
    summary="Delete an address",
)
async def delete_address(address_id: int, user: CurrentUser, db: DbSession) -> Any:
    await user_service.delete_address(db, user.id, address_id)
    await db.commit()
    return MessageResponse(detail="Address deleted")


@router.post(
    "/addresses/{address_id}/default",
    response_model=AddressOut,
    summary="Set the default address",
)
async def set_default_address(
    address_id: int, user: CurrentUser, db: DbSession
) -> Any:
    address = await user_service.set_default_address(db, user.id, address_id)
    await db.commit()
    return address


# --------------------------------------------------------------------------
# Wishlist
# --------------------------------------------------------------------------
def _wishlist_item(entry: Any) -> dict[str, Any]:
    product = entry.product
    images = product.image_urls or []
    first = images[0] if images else None
    if isinstance(first, dict):
        first = first.get("url")
    live = [v for v in product.variants if v.active and v.deleted_at is None]
    return {
        "id": entry.id,
        "product_id": entry.product_id,
        "product_name": product.name,
        "base_price": float(product.base_price),
        "image_url": first,
        "in_stock": any(v.stock > 0 for v in live),
        "added_at": entry.created_at,
    }


@router.get("/wishlist", response_model=list[WishlistItemOut], summary="My wishlist")
async def list_wishlist(user: CurrentUser, db: DbSession) -> Any:
    entries = await user_service.list_wishlist(db, user.id)
    return [_wishlist_item(entry) for entry in entries]


@router.post(
    "/wishlist/{product_id}",
    response_model=WishlistItemOut,
    status_code=201,
    summary="Save a product",
)
async def add_to_wishlist(product_id: int, user: CurrentUser, db: DbSession) -> Any:
    entry = await user_service.add_to_wishlist(db, user.id, product_id)
    await db.commit()
    return _wishlist_item(entry)


@router.delete(
    "/wishlist/{product_id}",
    response_model=MessageResponse,
    summary="Remove from wishlist",
)
async def remove_from_wishlist(
    product_id: int, user: CurrentUser, db: DbSession
) -> Any:
    await user_service.remove_from_wishlist(db, user.id, product_id)
    await db.commit()
    return MessageResponse(detail="Removed from wishlist")


# --------------------------------------------------------------------------
# Shipping
# --------------------------------------------------------------------------
@router.post(
    "/shipping/check",
    response_model=ShippingEstimate,
    summary="Pincode serviceability and shipping cost",
)
@limiter.limit(STORE_LIMIT)
async def check_pincode(
    request: Request,
    response: Response,
    body: ShippingEstimateRequest,
    db: DbSession,
    user: OptionalUser,
) -> Any:
    # Anonymous callers get a plain serviceability answer; a signed-in shopper
    # gets the real quote for what is currently in their cart.
    weight_grams = 0
    order_value = Decimal("0.00")

    if user is not None:
        cart = await cart_service.get_active_cart(db, user.id)
        if cart is not None and cart.items:
            weight_grams = cart_service.cart_weight(cart)
            priced = await cart_service.price_cart(db, cart, pincode=body.pincode)
            order_value = priced["totals"]["subtotal"]

    estimate = await shipping_service.calculate_shipping(
        db, body.pincode, weight_grams, order_value
    )
    await db.commit()
    return estimate
