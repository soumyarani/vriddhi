"""Account merging.

A shopper who used WhatsApp first and then signed in with Google is one person
with two rows. Merging has to move their history without ever hard-deleting
anything, and has to be safe to retry.
"""

from __future__ import annotations

import pytest

from app.errors import ConflictError
from app.models.enums import AuthProvider
from app.models.user import User
from app.services import account_merge


@pytest.fixture
async def whatsapp_user(db):
    record = User(phone="919000000001", name="WhatsApp Shopper",
                  auth_provider=AuthProvider.WHATSAPP)
    db.add(record)
    await db.flush()
    return record


async def test_merge_moves_orders_to_the_google_account(db, user, whatsapp_user):
    from app.models.order import Order

    order = Order(user_id=whatsapp_user.id, order_number="ORD-WA-1", subtotal=0,
                  total=0, address_snapshot={})
    db.add(order)
    await db.flush()

    result = await account_merge.merge_accounts(db, whatsapp_user.id, user.id)

    assert result["merged"] is True
    await db.refresh(order)
    assert order.user_id == user.id


async def test_source_is_soft_deleted_not_removed(db, user, whatsapp_user):
    """Nothing is hard-deleted — the row survives as an audit trail."""
    source_id = whatsapp_user.id
    await account_merge.merge_accounts(db, source_id, user.id)

    survivor = await db.get(User, source_id)
    assert survivor is not None
    assert survivor.deleted_at is not None
    assert survivor.merged_into_user_id == user.id


async def test_merge_frees_the_unique_identifiers(db, user, whatsapp_user):
    """The dead row must stop resolving, or the next login re-finds it."""
    source_id = whatsapp_user.id
    await account_merge.merge_accounts(db, source_id, user.id)

    survivor = await db.get(User, source_id)
    assert survivor.phone is None
    assert survivor.email is None
    assert survivor.google_id is None


async def test_target_absorbs_a_missing_phone(db, whatsapp_user):
    """The Google account survives, but inherits the phone it lacked."""
    google_only = User(email="g@example.com", name="G", google_id="g-only",
                       auth_provider=AuthProvider.GOOGLE)
    db.add(google_only)
    await db.flush()

    await account_merge.merge_accounts(db, whatsapp_user.id, google_only.id)
    assert google_only.phone == "919000000001"


async def test_target_keeps_its_own_details(db, user, whatsapp_user):
    """Absorbing must never overwrite something the survivor already had."""
    await account_merge.merge_accounts(db, whatsapp_user.id, user.id)
    assert user.name == "Asha Rao"
    assert user.email == "shopper@example.com"


async def test_merge_revokes_the_absorbed_sessions(db, user, whatsapp_user):
    from sqlalchemy import select

    from app.auth import issue_refresh_token
    from app.models.user import RefreshToken

    await issue_refresh_token(db, user_id=whatsapp_user.id)
    await account_merge.merge_accounts(db, whatsapp_user.id, user.id)

    tokens = (
        await db.execute(select(RefreshToken).where(RefreshToken.user_id == whatsapp_user.id))
    ).scalars().all()
    assert tokens and all(t.revoked_at is not None for t in tokens)


async def test_merge_is_idempotent(db, user, whatsapp_user):
    """A retried merge must report a no-op, not move data twice."""
    first = await account_merge.merge_accounts(db, whatsapp_user.id, user.id)
    second = await account_merge.merge_accounts(db, whatsapp_user.id, user.id)

    assert first["merged"] is True
    assert second["merged"] is False
    assert second["reason"] == "already_merged"


async def test_merging_an_account_into_itself_is_a_no_op(db, user):
    result = await account_merge.merge_accounts(db, user.id, user.id)
    assert result["merged"] is False


async def test_double_merging_into_a_third_account_is_refused(db, user, whatsapp_user):
    third = User(email="third@example.com", name="Third", google_id="g-third",
                 auth_provider=AuthProvider.GOOGLE)
    db.add(third)
    await db.flush()

    await account_merge.merge_accounts(db, whatsapp_user.id, user.id)
    with pytest.raises(ConflictError):
        await account_merge.merge_accounts(db, whatsapp_user.id, third.id)


async def test_wishlists_deduplicate_on_merge(db, user, whatsapp_user, product):
    from sqlalchemy import select

    from app.models.wishlist import Wishlist

    db.add(Wishlist(user_id=user.id, product_id=product.id))
    db.add(Wishlist(user_id=whatsapp_user.id, product_id=product.id))
    await db.flush()

    await account_merge.merge_accounts(db, whatsapp_user.id, user.id)

    rows = (
        await db.execute(select(Wishlist).where(Wishlist.user_id == user.id))
    ).scalars().all()
    assert len(rows) == 1


async def test_only_the_newest_cart_survives(db, user, whatsapp_user, product, variant):
    """Carts are not merged line-by-line.

    Combining two carts would silently change what the shopper is about to buy,
    so the most recently touched one wins and the other is retired.
    """
    from sqlalchemy import select

    from app.models.cart import Cart
    from app.models.enums import CartStatus
    from app.services import cart as cart_service

    await cart_service.add_item(db, user.id, product.id, variant.id, 1)
    await cart_service.add_item(db, whatsapp_user.id, product.id, variant.id, 2)

    await account_merge.merge_accounts(db, whatsapp_user.id, user.id)

    active = (
        await db.execute(
            select(Cart).where(Cart.user_id == user.id, Cart.status == CartStatus.ACTIVE)
        )
    ).scalars().all()
    assert len(active) == 1
