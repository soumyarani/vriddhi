"""Merging a WhatsApp-only account into a Google account.

A shopper often starts on WhatsApp (identified by phone) and later signs in on
the web with Google (identified by email). Those are two rows until one of them
proves it owns the other's identifier.

Rules, per spec:

* The Google account is always the survivor — it carries the email, which is
  the stronger identity and the one email notifications go to.
* Orders, addresses, conversations, reviews and wishlists move wholesale.
* Carts do not merge line-by-line: the most recently updated cart wins.
  Merging two carts silently changes what someone thought they were buying.
* The absorbed row is soft-deleted, never dropped, and keeps a
  `merged_into_user_id` pointer so old references still resolve.
* Re-running the merge is a no-op, because the second call finds the source
  already pointing at the target.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ConflictError, ValidationError
from app.models.cart import Cart
from app.models.conversation import Conversation
from app.models.enums import AuthProvider, CartStatus
from app.models.order import Order
from app.models.review import Review
from app.models.user import Address, RefreshToken, User
from app.models.wishlist import Wishlist
from logging_config import get_logger

log = get_logger(__name__)

# Tables that simply change owner. (model, user_id column)
_REASSIGNED = (Order, Address, Conversation, Review)


async def merge_accounts(db: AsyncSession, source_id: int, target_id: int) -> dict[str, Any]:
    """Fold `source` into `target`. Safe to call more than once."""
    if source_id == target_id:
        return {"merged": False, "reason": "same_account"}

    source = await db.get(User, source_id)
    target = await db.get(User, target_id)
    if source is None or target is None:
        raise ValidationError("Both accounts must exist to merge")

    if source.merged_into_user_id == target_id:
        log.info("account_merge_noop", source_id=source_id, target_id=target_id)
        return {"merged": False, "reason": "already_merged"}
    if source.merged_into_user_id is not None or target.merged_into_user_id is not None:
        raise ConflictError("One of these accounts has already been merged elsewhere")

    moved: dict[str, int] = {}

    for model in _REASSIGNED:
        result = await db.execute(
            update(model).where(model.user_id == source_id).values(user_id=target_id)
        )
        moved[model.__tablename__] = result.rowcount or 0

    moved["wishlists"] = await _merge_wishlists(db, source_id, target_id)
    moved["carts"] = await _merge_carts(db, source_id, target_id)

    # Sessions belonging to the absorbed account must not keep working.
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == source_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )

    _absorb_identity(source, target)

    source.merged_into_user_id = target_id
    source.soft_delete()
    # Free the unique indexes so the phone/email can never resolve to the dead row.
    source.phone = None
    source.email = None
    source.google_id = None

    await db.flush()

    log.info(
        "accounts_merged",
        source_id=source_id,
        target_id=target_id,
        moved=moved,
    )
    return {"merged": True, "source_id": source_id, "target_id": target_id, "moved": moved}


def _absorb_identity(source: User, target: User) -> None:
    """Copy over identifiers the survivor is missing, never overwriting its own."""
    if not target.phone and source.phone:
        target.phone = source.phone
    if not target.name and source.name:
        target.name = source.name
    if not target.gstin and source.gstin:
        target.gstin = source.gstin
    if source.whatsapp_opt_in:
        target.whatsapp_opt_in = True
    if target.google_id:
        target.auth_provider = AuthProvider.GOOGLE


async def _merge_wishlists(db: AsyncSession, source_id: int, target_id: int) -> int:
    """Move wishlist rows, dropping products the target already saved."""
    existing = set(
        (
            await db.execute(select(Wishlist.product_id).where(Wishlist.user_id == target_id))
        )
        .scalars()
        .all()
    )
    rows = (
        (await db.execute(select(Wishlist).where(Wishlist.user_id == source_id)))
        .scalars()
        .all()
    )

    moved = 0
    for row in rows:
        if row.product_id in existing:
            await db.delete(row)
        else:
            row.user_id = target_id
            moved += 1
    return moved


async def _merge_carts(db: AsyncSession, source_id: int, target_id: int) -> int:
    """Keep the most recently updated active cart; abandon the other.

    Line-level merging is deliberately avoided — combining two carts changes
    the order someone is about to place without them asking for it.
    """
    carts = list(
        (
            await db.execute(
                select(Cart)
                .where(
                    Cart.user_id.in_([source_id, target_id]),
                    Cart.status == CartStatus.ACTIVE,
                )
                .order_by(Cart.updated_at.desc(), Cart.id.desc())
            )
        )
        .scalars()
        .all()
    )
    if not carts:
        return 0

    winner, *losers = carts
    winner.user_id = target_id
    for cart in losers:
        cart.status = CartStatus.MERGED
        cart.user_id = target_id

    return 1


async def link_or_merge_on_google_login(
    db: AsyncSession, google_user: User, phone: str | None
) -> dict[str, Any]:
    """Called after a Google sign-in that supplies a verified phone number.

    If a separate WhatsApp-only account owns that phone, absorb it.
    """
    if not phone:
        return {"merged": False, "reason": "no_phone"}

    from app.services.user import get_by_phone, normalize_phone

    phone = normalize_phone(phone)
    existing = await get_by_phone(db, phone)

    if existing is None:
        google_user.phone = phone
        await db.flush()
        return {"merged": False, "reason": "phone_claimed"}

    if existing.id == google_user.id:
        return {"merged": False, "reason": "same_account"}

    return await merge_accounts(db, source_id=existing.id, target_id=google_user.id)


async def link_or_merge_on_whatsapp_identify(
    db: AsyncSession, whatsapp_user: User, email: str
) -> dict[str, Any]:
    """Called when a WhatsApp shopper proves ownership of an email address.

    The Google account survives even though WhatsApp initiated the link.
    """
    from app.services.user import get_by_email

    google_user = await get_by_email(db, email)
    if google_user is None:
        whatsapp_user.email = email.strip().lower()
        await db.flush()
        return {"merged": False, "reason": "email_claimed"}

    if google_user.id == whatsapp_user.id:
        return {"merged": False, "reason": "same_account"}

    return await merge_accounts(db, source_id=whatsapp_user.id, target_id=google_user.id)


def _now():
    from app.models.base import utcnow

    return utcnow()
