"""User profile, addresses and wishlist.

Address writes go through `_clear_other_defaults` so the "exactly one default"
invariant holds regardless of which endpoint touched the row.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ConflictError, NotFoundError, ValidationError
from app.models.enums import AuthProvider
from app.models.product import Product
from app.models.user import Address, User
from app.models.wishlist import Wishlist
from app.services.shipping import is_serviceable
from app.services.tax import state_code_for
from logging_config import get_logger

log = get_logger(__name__)


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
async def get_user(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None or user.is_deleted:
        raise NotFoundError("User not found")
    return user


async def get_by_phone(db: AsyncSession, phone: str) -> User | None:
    return (
        await db.execute(
            select(User).where(User.phone == normalize_phone(phone), User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    return (
        await db.execute(
            select(User).where(
                func.lower(User.email) == email.strip().lower(), User.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()


async def get_by_google_id(db: AsyncSession, google_id: str) -> User | None:
    return (
        await db.execute(
            select(User).where(User.google_id == google_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()


def normalize_phone(phone: str) -> str:
    """Store phones as bare digits with the country code, matching Meta's `wa_id`."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if not digits:
        raise ValidationError("Invalid phone number")
    return digits


async def get_or_create_whatsapp_user(
    db: AsyncSession, phone: str, name: str | None = None
) -> tuple[User, bool]:
    """Return `(user, created)` for an inbound WhatsApp sender."""
    phone = normalize_phone(phone)
    user = await get_by_phone(db, phone)
    if user is not None:
        # Meta only sends the profile name; never overwrite a name the user set.
        if name and not user.name:
            user.name = name
            await db.flush()
        return user, False

    user = User(
        phone=phone,
        name=name,
        auth_provider=AuthProvider.WHATSAPP,
        whatsapp_opt_in=True,
    )
    db.add(user)
    await db.flush()
    log.info("user_created", user_id=user.id, provider="whatsapp")
    return user, True


async def update_profile(db: AsyncSession, user_id: int, data: Any) -> User:
    user = await get_user(db, user_id)
    fields = data.model_dump(exclude_unset=True) if hasattr(data, "model_dump") else dict(data)

    if "phone" in fields and fields["phone"]:
        phone = normalize_phone(fields["phone"])
        existing = await get_by_phone(db, phone)
        if existing is not None and existing.id != user.id:
            raise ConflictError("That phone number belongs to another account")
        fields["phone"] = phone

    for field, value in fields.items():
        setattr(user, field, value)

    await db.flush()
    log.info("user_profile_updated", user_id=user.id, fields=sorted(fields))
    return user


def serialize_user(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "phone": user.phone,
        "picture_url": user.picture_url,
        "auth_provider": user.auth_provider,
        "whatsapp_opt_in": user.whatsapp_opt_in,
        "gstin": user.gstin,
        "created_at": user.created_at,
    }


# --------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------
async def list_addresses(db: AsyncSession, user_id: int) -> list[Address]:
    return list(
        (
            await db.execute(
                select(Address)
                .where(Address.user_id == user_id, Address.deleted_at.is_(None))
                .order_by(Address.is_default.desc(), Address.id.desc())
            )
        )
        .scalars()
        .all()
    )


async def get_address(db: AsyncSession, user_id: int, address_id: int) -> Address:
    address = (
        await db.execute(
            select(Address).where(
                Address.id == address_id,
                Address.user_id == user_id,
                Address.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if address is None:
        raise NotFoundError("Address not found")
    return address


async def _clear_other_defaults(db: AsyncSession, user_id: int, keep_id: int | None) -> None:
    stmt = update(Address).where(Address.user_id == user_id, Address.is_default.is_(True))
    if keep_id is not None:
        stmt = stmt.where(Address.id != keep_id)
    await db.execute(stmt.values(is_default=False))


async def create_address(db: AsyncSession, user_id: int, data: Any) -> Address:
    fields = data.model_dump() if hasattr(data, "model_dump") else dict(data)
    address = Address(user_id=user_id, **fields)
    address.is_serviceable = await is_serviceable(db, address.pincode)

    existing = await list_addresses(db, user_id)
    # First address is always the default; nothing to fall back to otherwise.
    if not existing:
        address.is_default = True

    db.add(address)
    await db.flush()

    if address.is_default:
        await _clear_other_defaults(db, user_id, keep_id=address.id)

    log.info("address_created", user_id=user_id, address_id=address.id)
    return address


async def update_address(db: AsyncSession, user_id: int, address_id: int, data: Any) -> Address:
    address = await get_address(db, user_id, address_id)
    fields = data.model_dump(exclude_unset=True) if hasattr(data, "model_dump") else dict(data)

    for field, value in fields.items():
        setattr(address, field, value)

    if "pincode" in fields:
        address.is_serviceable = await is_serviceable(db, address.pincode)

    await db.flush()
    if address.is_default:
        await _clear_other_defaults(db, user_id, keep_id=address.id)
    return address


async def set_default_address(db: AsyncSession, user_id: int, address_id: int) -> Address:
    address = await get_address(db, user_id, address_id)
    await _clear_other_defaults(db, user_id, keep_id=address.id)
    address.is_default = True
    await db.flush()
    return address


async def delete_address(db: AsyncSession, user_id: int, address_id: int) -> None:
    address = await get_address(db, user_id, address_id)
    was_default = address.is_default
    address.soft_delete()
    address.is_default = False
    await db.flush()

    if was_default:
        # Promote the newest surviving address so checkout always has a default.
        remaining = await list_addresses(db, user_id)
        if remaining:
            remaining[0].is_default = True
            await db.flush()

    log.info("address_deleted", user_id=user_id, address_id=address_id)


async def get_default_address(db: AsyncSession, user_id: int) -> Address | None:
    addresses = await list_addresses(db, user_id)
    return addresses[0] if addresses else None


def serialize_address(address: Address) -> dict[str, Any]:
    return {
        "id": address.id,
        "label": address.label,
        "recipient_name": address.recipient_name,
        "recipient_phone": address.recipient_phone,
        "line1": address.line1,
        "line2": address.line2,
        "city": address.city,
        "state": address.state,
        "state_code": state_code_for(address.state),
        "pincode": address.pincode,
        "country": address.country,
        "is_default": address.is_default,
        "is_serviceable": address.is_serviceable,
    }


# --------------------------------------------------------------------------
# Wishlist
# --------------------------------------------------------------------------
async def list_wishlist(db: AsyncSession, user_id: int) -> list[Wishlist]:
    return list(
        (
            await db.execute(
                select(Wishlist)
                .where(Wishlist.user_id == user_id)
                .order_by(Wishlist.id.desc())
            )
        )
        .scalars()
        .all()
    )


async def add_to_wishlist(db: AsyncSession, user_id: int, product_id: int) -> Wishlist:
    product = (
        await db.execute(
            select(Product).where(Product.id == product_id, Product.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if product is None:
        raise NotFoundError("Product not found")

    existing = (
        await db.execute(
            select(Wishlist).where(
                Wishlist.user_id == user_id, Wishlist.product_id == product_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    entry = Wishlist(user_id=user_id, product_id=product_id)
    db.add(entry)
    await db.flush()
    return entry


async def remove_from_wishlist(db: AsyncSession, user_id: int, product_id: int) -> None:
    entry = (
        await db.execute(
            select(Wishlist).where(
                Wishlist.user_id == user_id, Wishlist.product_id == product_id
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        raise NotFoundError("Product is not in your wishlist")
    await db.delete(entry)
    await db.flush()
