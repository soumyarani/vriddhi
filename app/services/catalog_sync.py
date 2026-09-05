"""Product -> Meta Commerce Manager catalog synchronisation.

Uses the Catalog Batch API so a product and all of its variants move in one
call. Nothing here raises: sync runs as a background task and a catalog outage
must never fail the product write that triggered it.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.base import utcnow
from app.models.enums import SyncStatus
from app.models.product import Product, ProductVariant
from logging_config import get_logger

log = get_logger(__name__)

CURRENCY = settings.currency
ITEM_TYPE = "PRODUCT_ITEM"
CONDITION = "new"
IN_STOCK = "in stock"
OUT_OF_STOCK = "out of stock"
MAX_SYNC_ERROR_CHARS = 1000

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=15.0, pool=5.0)


def build_retailer_id(product_id: int, variant_id: int | None = None) -> str:
    return f"prod_{product_id}" if variant_id is None else f"prod_{product_id}_v{variant_id}"


def _to_paise(price: Decimal) -> int:
    return int((Decimal(price) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _product_image(product: Product) -> str | None:
    for url in product.image_urls or []:
        if isinstance(url, str) and url.strip():
            return url
    images = sorted(product.images or [], key=lambda i: i.sort_order)
    for image in images:
        if image.variant_id is None and image.url:
            return image.url
    return images[0].url if images else None


def _variant_image(product: Product, variant: ProductVariant) -> str | None:
    images = sorted(
        (i for i in product.images or [] if i.variant_id == variant.id),
        key=lambda i: i.sort_order,
    )
    return images[0].url if images else _product_image(product)


def _item_data(
    product: Product,
    retailer_id: str,
    name: str,
    price: Decimal,
    availability: str,
    image_url: str,
) -> dict[str, Any]:
    return {
        "id": retailer_id,
        "name": name[:200],
        "description": (product.description or product.name)[:9999],
        "price": _to_paise(price),
        "currency": CURRENCY,
        "availability": availability,
        "condition": CONDITION,
        "image_url": image_url,
        "url": f"{settings.storefront_url}/products/{product.id}",
        "brand": settings.seller_legal_name,
    }


def _build_requests(product: Product) -> tuple[list[dict[str, Any]], dict[int | None, str]]:
    """Returns batch requests plus a {variant_id or None: retailer_id} map."""
    requests: list[dict[str, Any]] = []
    retailer_ids: dict[int | None, str] = {}

    variants = [v for v in product.variants if not v.is_deleted and v.active]

    if not variants:
        image_url = _product_image(product)
        if not image_url:
            log.warning("catalog_item_skipped", product_id=product.id, reason="missing_image")
            return [], {}
        retailer_id = build_retailer_id(product.id)
        retailer_ids[None] = retailer_id
        requests.append(
            {
                "method": "UPDATE",
                "data": _item_data(
                    product,
                    retailer_id,
                    product.name,
                    product.base_price,
                    IN_STOCK if product.active else OUT_OF_STOCK,
                    image_url,
                ),
            }
        )
        return requests, retailer_ids

    for variant in variants:
        image_url = _variant_image(product, variant)
        if not image_url:
            log.warning(
                "catalog_item_skipped",
                product_id=product.id,
                variant_id=variant.id,
                reason="missing_image",
            )
            continue
        retailer_id = build_retailer_id(product.id, variant.id)
        retailer_ids[variant.id] = retailer_id
        price = variant.price_override if variant.price_override is not None else product.base_price
        requests.append(
            {
                "method": "UPDATE",
                "data": _item_data(
                    product,
                    retailer_id,
                    f"{product.name} - {variant.name}",
                    price,
                    IN_STOCK if variant.stock > 0 else OUT_OF_STOCK,
                    image_url,
                ),
            }
        )

    return requests, retailer_ids


async def _post_batch(requests: list[dict[str, Any]]) -> dict[str, Any]:
    url = f"{settings.graph_base}/{settings.whatsapp_catalog_id}/items_batch"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json",
    }
    payload = {"item_type": ITEM_TYPE, "requests": requests}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(url, json=payload, headers=headers)

    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:500]}
    if not isinstance(body, dict):
        body = {"raw": body}

    if response.status_code >= 400:
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        message = error.get("error_user_msg") or error.get("message") or response.text[:300]
        raise RuntimeError(f"catalog batch failed ({response.status_code}): {message}")

    # A 200 can still carry per-item validation failures.
    for handle in body.get("validation_status") or []:
        errors = handle.get("errors") or []
        if errors:
            raise RuntimeError(
                f"catalog item {handle.get('retailer_id')} rejected: {errors[0].get('message')}"
            )

    return body


async def _load_product(db: AsyncSession, product_id: int) -> Product | None:
    result = await db.execute(
        select(Product)
        .options(selectinload(Product.variants), selectinload(Product.images))
        .where(Product.id == product_id)
    )
    return result.scalar_one_or_none()


async def _mark_failed(db: AsyncSession, product: Product, reason: str) -> None:
    product.sync_status = SyncStatus.FAILED
    product.sync_error = reason[:MAX_SYNC_ERROR_CHARS]
    await db.commit()


async def sync_product(db: AsyncSession, product_id: int) -> bool:
    if not settings.whatsapp_catalog_id:
        log.warning("catalog_sync_disabled", product_id=product_id, reason="missing_catalog_id")
        return False

    product = await _load_product(db, product_id)
    if product is None:
        log.warning("catalog_sync_product_missing", product_id=product_id)
        return False

    try:
        requests, retailer_ids = _build_requests(product)
        if not requests:
            await _mark_failed(db, product, "No syncable items: product image_url is required by Meta")
            log.warning("catalog_sync_failed", product_id=product_id, reason="no_syncable_items")
            return False

        await _post_batch(requests)

        for variant in product.variants:
            if variant.id in retailer_ids:
                variant.meta_retailer_id = retailer_ids[variant.id]
        product.meta_retailer_id = retailer_ids.get(None) or build_retailer_id(product.id)
        product.sync_status = SyncStatus.SYNCED
        product.sync_error = None
        product.synced_at = utcnow()
        await db.commit()

        log.info("catalog_sync_ok", product_id=product_id, items=len(requests))
        return True
    except Exception as exc:
        await db.rollback()
        product = await _load_product(db, product_id)
        if product is not None:
            try:
                await _mark_failed(db, product, str(exc))
            except Exception as persist_exc:
                await db.rollback()
                log.warning(
                    "catalog_sync_status_persist_failed",
                    product_id=product_id,
                    error=str(persist_exc),
                )
        log.warning("catalog_sync_failed", product_id=product_id, error=str(exc))
        return False


async def delete_from_catalog(db: AsyncSession, product_id: int) -> bool:
    if not settings.whatsapp_catalog_id:
        log.warning("catalog_sync_disabled", product_id=product_id, reason="missing_catalog_id")
        return False

    product = await _load_product(db, product_id)
    if product is None:
        log.warning("catalog_delete_product_missing", product_id=product_id)
        return False

    retailer_ids = [product.meta_retailer_id or build_retailer_id(product.id)]
    retailer_ids += [
        variant.meta_retailer_id or build_retailer_id(product.id, variant.id)
        for variant in product.variants
    ]

    try:
        await _post_batch(
            [
                {"method": "DELETE", "data": {"id": rid}}
                for rid in dict.fromkeys(retailer_ids)
            ]
        )

        product.meta_retailer_id = None
        product.sync_status = SyncStatus.PENDING
        product.sync_error = None
        product.synced_at = None
        for variant in product.variants:
            variant.meta_retailer_id = None
        await db.commit()

        log.info("catalog_delete_ok", product_id=product_id, items=len(retailer_ids))
        return True
    except Exception as exc:
        await db.rollback()
        log.warning("catalog_delete_failed", product_id=product_id, error=str(exc))
        return False


async def sync_pending(db: AsyncSession, limit: int = 50) -> dict[str, int]:
    result = await db.execute(
        select(Product.id)
        .where(Product.sync_status != SyncStatus.SYNCED, Product.deleted_at.is_(None))
        .order_by(Product.updated_at)
        .limit(limit)
    )
    product_ids = list(result.scalars().all())

    synced = 0
    failed = 0
    for product_id in product_ids:
        if await sync_product(db, product_id):
            synced += 1
        else:
            failed += 1

    log.info("catalog_sync_pending_done", synced=synced, failed=failed, considered=len(product_ids))
    return {"synced": synced, "failed": failed}
