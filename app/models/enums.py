from __future__ import annotations

from enum import StrEnum


class AuthProvider(StrEnum):
    GOOGLE = "google"
    WHATSAPP = "whatsapp"


class AgentRole(StrEnum):
    AGENT = "agent"
    ADMIN = "admin"


class CartStatus(StrEnum):
    ACTIVE = "active"
    CHECKED_OUT = "checked_out"
    EXPIRED = "expired"
    # Superseded by another cart during an account merge.
    MERGED = "merged"


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    PENDING_CONFIRMATION = "pending_confirmation"
    CONFIRMED = "confirmed"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RETURN_REQUESTED = "return_requested"
    RETURN_APPROVED = "return_approved"
    RETURNED = "returned"
    REFUNDED = "refunded"


# Allowed forward transitions. Anything not listed is rejected by the order service.
ORDER_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING_PAYMENT: {OrderStatus.PAID, OrderStatus.CANCELLED},
    OrderStatus.PAID: {OrderStatus.PENDING_CONFIRMATION, OrderStatus.CANCELLED, OrderStatus.REFUNDED},
    OrderStatus.PENDING_CONFIRMATION: {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
    OrderStatus.CONFIRMED: {OrderStatus.PROCESSING, OrderStatus.CANCELLED},
    OrderStatus.PROCESSING: {OrderStatus.SHIPPED, OrderStatus.CANCELLED},
    OrderStatus.SHIPPED: {OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED},
    OrderStatus.OUT_FOR_DELIVERY: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: {OrderStatus.RETURN_REQUESTED},
    OrderStatus.RETURN_REQUESTED: {OrderStatus.RETURN_APPROVED, OrderStatus.DELIVERED},
    OrderStatus.RETURN_APPROVED: {OrderStatus.RETURNED},
    OrderStatus.RETURNED: {OrderStatus.REFUNDED},
    OrderStatus.CANCELLED: {OrderStatus.REFUNDED},
    OrderStatus.REFUNDED: set(),
}

# Statuses an agent may set directly via the admin update-status endpoint.
AGENT_SETTABLE_STATUSES = {
    OrderStatus.PROCESSING,
    OrderStatus.SHIPPED,
    OrderStatus.OUT_FOR_DELIVERY,
    OrderStatus.DELIVERED,
    OrderStatus.RETURN_APPROVED,
    OrderStatus.RETURNED,
}

# Once an order reaches any of these, the customer can no longer cancel it.
NON_CANCELLABLE_STATUSES = {
    OrderStatus.SHIPPED,
    OrderStatus.OUT_FOR_DELIVERY,
    OrderStatus.DELIVERED,
    OrderStatus.CANCELLED,
    OrderStatus.RETURN_REQUESTED,
    OrderStatus.RETURN_APPROVED,
    OrderStatus.RETURNED,
    OrderStatus.REFUNDED,
}


class PaymentStatus(StrEnum):
    CREATED = "created"
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    EXPIRED = "expired"
    FLAGGED = "flagged"  # amount mismatch — held for manual review


class RefundStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class ConversationStatus(StrEnum):
    AI = "ai"
    QUEUED = "queued"
    HUMAN = "human"
    RESOLVED = "resolved"


class MessageDirection(StrEnum):
    IN = "in"
    OUT = "out"


class MessageType(StrEnum):
    TEXT = "text"
    PRODUCT = "product"
    PRODUCT_LIST = "product_list"
    LIST = "list"
    BUTTONS = "buttons"
    IMAGE = "image"


class DiscountType(StrEnum):
    PERCENT = "percent"
    FLAT = "flat"


class WebhookSource(StrEnum):
    META = "meta"
    CASHFREE = "cashfree"


class WebhookStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class SyncStatus(StrEnum):
    PENDING = "pending"
    SYNCED = "synced"
    FAILED = "failed"


class NotificationType(StrEnum):
    NEW_ORDER = "new_order"
    ESCALATION = "escalation"
    RETURN_REQUEST = "return_request"
    PAYMENT_FLAGGED = "payment_flagged"
    ORDER_CANCELLED = "order_cancelled"
    LOW_STOCK = "low_stock"


class EmailType(StrEnum):
    ORDER_CONFIRMED = "order_confirmed"
    ORDER_SHIPPED = "order_shipped"
    ORDER_DELIVERED = "order_delivered"
    REFUND_PROCESSED = "refund_processed"
    ACCOUNT_LINKED = "account_linked"


class Intent(StrEnum):
    BROWSE = "browse"
    SEARCH = "search"
    ADD_TO_CART = "add_to_cart"
    REMOVE_FROM_CART = "remove_from_cart"
    VIEW_CART = "view_cart"
    CHECKOUT = "checkout"
    TRACK_ORDER = "track_order"
    ORDER_HISTORY = "order_history"
    CANCEL_ORDER = "cancel_order"
    RETURN_ORDER = "return_order"
    HELP = "help"
    ESCALATE = "escalate"


# Intents the scripted fallback can serve from the DB when OpenAI is unavailable.
FALLBACK_SERVICEABLE_INTENTS = {
    Intent.BROWSE,
    Intent.VIEW_CART,
    Intent.TRACK_ORDER,
    Intent.ORDER_HISTORY,
    Intent.HELP,
}
