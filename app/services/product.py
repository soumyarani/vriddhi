"""Catalog reads and writes, with Redis caching for the hot paths."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.cache import (
    TTL_CATEGORIES,
    TTL_PRODUCT_DETAIL,
    TTL_RATING,
    CacheKeys,
    cache_get,
    cache_set,
    invalidate_catalog,
)
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models.enums import SyncStatus
from app.models.product import Category, Product, ProductImage, ProductVariant
from app.models.review import Review
from app.pagination import apply_cursor, build_page
from app.schemas.product import (
    ProductCreate,
    ProductFilters,
    ProductUpdate,
    VariantCreate,
    VariantUpdate,
)
from logging_config import get_logger

log = get_logger(__name__)


def _slugify(name: str) -> str:
    cleaned = "".join(c.lower() if c.isalnum() else "-" for c in name)
    return "-".join(part for part in cleaned.split("-") if part)[:255]


def _active_product_filter() -> list[Any]:
    return [Product.deleted_at.is_(None), Product.active.is_(True)]


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------
async def list_categories(db: AsyncSession, use_cache: bool = True) -> list[dict[str, Any]]:
    if use_cache:
        cached = await cache_get(CacheKeys.CATEGORIES)
        if cached is not None:
            return cached

    counts = (
        select(Product.category_id, func.count(Product.id).label("cnt"))
        .where(*_active_product_filter())
        .group_by(Product.category_id)
        .subquery()
    )
    stmt = (
        select(Category, func.coalesce(counts.c.cnt, 0))
        .outerjoin(counts, counts.c.category_id == Category.id)
        .where(Category.deleted_at.is_(None), Category.active.is_(True))
        .order_by(Category.sort_order, Category.name)
    )
    rows = (await db.execute(stmt)).all()

    payload = [
        {
            "id": category.id,
            "name": category.name,
            "slug": category.slug,
            "description": category.description,
            "image_url": category.image_url,
            "sort_order": category.sort_order,
            "active": category.active,
            "product_count": int(count),
        }
        for category, count in rows
    ]
    if use_cache:
        await cache_set(CacheKeys.CATEGORIES, payload, TTL_CATEGORIES)
    return payload


async def create_category(db: AsyncSession, data: Any) -> Category:
    category = Category(
        name=data.name,
        slug=_slugify(data.name),
        description=data.description,
        image_url=data.image_url,
        sort_order=data.sort_order,
        active=data.active,
    )
    db.add(category)
    await db.flush()
    await invalidate_catalog()
    return category


async def get_category(db: AsyncSession, category_id: int) -> Category:
    stmt = select(Category).where(Category.id == category_id, Category.deleted_at.is_(None))
    category = (await db.execute(stmt)).scalar_one_or_none()
    if category is None:
        raise NotFoundError("Category not found")
    return category


# --------------------------------------------------------------------------
# Ratings
# --------------------------------------------------------------------------
async def get_rating_summary(db: AsyncSession, product_id: int) -> dict[str, Any]:
    """Ratings are always derived with AVG(), never a mutable column."""
    key = CacheKeys.PRODUCT_RATING.format(product_id=product_id)
    cached = await cache_get(key)
    if cached is not None:
        return cached

    stmt = (
        select(Review.rating, func.count(Review.id))
        .where(Review.product_id == product_id, Review.deleted_at.is_(None))
        .group_by(Review.rating)
    )
    rows = (await db.execute(stmt)).all()

    distribution = {int(rating): int(count) for rating, count in rows}
    total = sum(distribution.values())
    avg = (
        round(sum(r * c for r, c in distribution.items()) / total, 2) if total else None
    )
    summary = {
        "product_id": product_id,
        "avg_rating": avg,
        "review_count": total,
        "distribution": distribution,
    }
    await cache_set(key, summary, TTL_RATING)
    return summary


async def get_ratings_bulk(db: AsyncSession, product_ids: list[int]) -> dict[int, dict[str, Any]]:
    """One grouped query for a product list — avoids N+1 on the listing page."""
    if not product_ids:
        return {}
    stmt = (
        select(
            Review.product_id,
            func.avg(Review.rating),
            func.count(Review.id),
        )
        .where(Review.product_id.in_(product_ids), Review.deleted_at.is_(None))
        .group_by(Review.product_id)
    )
    rows = (await db.execute(stmt)).all()
    return {
        int(pid): {"avg_rating": round(float(avg), 2), "review_count": int(count)}
        for pid, avg, count in rows
    }


# --------------------------------------------------------------------------
# Product queries
# --------------------------------------------------------------------------
def _filters_fingerprint(filters: ProductFilters, cursor: str | None, limit: int) -> str:
    raw = json.dumps(
        {**filters.model_dump(mode="json"), "cursor": cursor, "limit": limit},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _apply_filters(stmt: Select, filters: ProductFilters) -> Select:
    if filters.category_id is not None:
        stmt = stmt.where(Product.category_id == filters.category_id)

    if filters.q:
        term = f"%{filters.q.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Product.name).like(term),
                func.lower(func.coalesce(Product.description, "")).like(term),
            )
        )

    if filters.min_price is not None:
        stmt = stmt.where(Product.base_price >= filters.min_price)
    if filters.max_price is not None:
        stmt = stmt.where(Product.base_price <= filters.max_price)

    if filters.in_stock_only:
        in_stock = (
            select(ProductVariant.product_id)
            .where(
                ProductVariant.stock > 0,
                ProductVariant.active.is_(True),
                ProductVariant.deleted_at.is_(None),
            )
            .distinct()
            .subquery()
        )
        stmt = stmt.join(in_stock, in_stock.c.product_id == Product.id)

    return stmt


async def list_products(
    db: AsyncSession,
    filters: ProductFilters,
    cursor: str | None = None,
    limit: int = 20,
    include_inactive: bool = False,
) -> dict[str, Any]:
    stmt = select(Product).options(selectinload(Product.variants), selectinload(Product.category))
    if not include_inactive:
        stmt = stmt.where(*_active_product_filter())

    stmt = _apply_filters(stmt, filters)
    stmt = apply_cursor(stmt, Product.created_at, Product.id, cursor, descending=True)

    # Over-fetch by one so `has_more` is exact without a COUNT query.
    rows = (await db.execute(stmt.limit(limit + 1))).scalars().unique().all()
    items, next_cursor, has_more = build_page(rows, limit)

    ratings = await get_ratings_bulk(db, [p.id for p in items])

    if filters.min_rating is not None:
        items = [
            p
            for p in items
            if (ratings.get(p.id, {}).get("avg_rating") or 0) >= filters.min_rating
        ]

    return {
        "items": [serialize_product_summary(p, ratings.get(p.id)) for p in items],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


def serialize_product_summary(
    product: Product, rating: dict[str, Any] | None = None
) -> dict[str, Any]:
    live_variants = [v for v in product.variants if v.active and v.deleted_at is None]
    prices = [
        v.price_override if v.price_override is not None else product.base_price
        for v in live_variants
    ]
    return {
        "id": product.id,
        "name": product.name,
        "description": product.description,
        "base_price": product.base_price,
        "image_urls": product.image_urls or [],
        "category_id": product.category_id,
        "category_name": product.category.name if product.category else None,
        "avg_rating": (rating or {}).get("avg_rating"),
        "review_count": (rating or {}).get("review_count", 0),
        "in_stock": any(v.stock > 0 for v in live_variants),
        "min_price": min(prices) if prices else product.base_price,
    }


async def get_product_detail(
    db: AsyncSession, product_id: int, use_cache: bool = True
) -> dict[str, Any]:
    key = CacheKeys.PRODUCT_DETAIL.format(product_id=product_id)
    if use_cache:
        cached = await cache_get(key)
        if cached is not None:
            return cached

    product = await get_product(db, product_id, with_images=True)
    rating = await get_rating_summary(db, product_id)

    detail = serialize_product_summary(product, rating)
    detail.update(
        {
            "hsn_code": product.hsn_code,
            "gst_rate": product.gst_rate,
            "weight_grams": product.weight_grams,
            "active": product.active,
            "meta_retailer_id": product.meta_retailer_id,
            "sync_status": product.sync_status,
            "created_at": product.created_at,
            "variants": [
                {
                    "id": v.id,
                    "sku": v.sku,
                    "name": v.name,
                    "attributes": v.attributes or {},
                    "price": v.price_override if v.price_override is not None else product.base_price,
                    "price_override": v.price_override,
                    "stock": v.stock,
                    "in_stock": v.stock > 0,
                    "active": v.active,
                    "meta_retailer_id": v.meta_retailer_id,
                }
                for v in product.variants
                if v.deleted_at is None
            ],
            "images": [
                {
                    "id": i.id,
                    "url": i.url,
                    "alt_text": i.alt_text,
                    "sort_order": i.sort_order,
                    "variant_id": i.variant_id,
                }
                for i in sorted(product.images, key=lambda x: x.sort_order)
            ],
        }
    )

    if use_cache:
        await cache_set(key, detail, TTL_PRODUCT_DETAIL)
    return detail


async def get_product(
    db: AsyncSession,
    product_id: int,
    include_deleted: bool = False,
    with_images: bool = False,
) -> Product:
    options = [selectinload(Product.variants), selectinload(Product.category)]
    if with_images:
        options.append(selectinload(Product.images))

    stmt = select(Product).options(*options).where(Product.id == product_id)
    if not include_deleted:
        stmt = stmt.where(Product.deleted_at.is_(None))

    product = (await db.execute(stmt)).scalar_one_or_none()
    if product is None:
        raise NotFoundError("Product not found")
    return product


async def get_variant(db: AsyncSession, variant_id: int) -> ProductVariant:
    stmt = (
        select(ProductVariant)
        .options(selectinload(ProductVariant.product))
        .where(ProductVariant.id == variant_id, ProductVariant.deleted_at.is_(None))
    )
    variant = (await db.execute(stmt)).scalar_one_or_none()
    if variant is None:
        raise NotFoundError("Product variant not found")
    return variant


# --------------------------------------------------------------------------
# Product writes
# --------------------------------------------------------------------------
async def _assert_sku_free(db: AsyncSession, skus: list[str], exclude_id: int | None = None) -> None:
    if not skus:
        return
    stmt = select(ProductVariant.sku).where(ProductVariant.sku.in_(skus))
    if exclude_id is not None:
        stmt = stmt.where(ProductVariant.id != exclude_id)
    taken = (await db.execute(stmt)).scalars().all()
    if taken:
        raise ConflictError(f"SKU already exists: {', '.join(sorted(set(taken)))}")


async def create_product(db: AsyncSession, data: ProductCreate) -> Product:
    if data.category_id is not None:
        await get_category(db, data.category_id)

    await _assert_sku_free(db, [v.sku for v in data.variants])

    product = Product(
        name=data.name,
        description=data.description,
        base_price=data.base_price,
        category_id=data.category_id,
        image_urls=data.image_urls,
        hsn_code=data.hsn_code,
        gst_rate=data.gst_rate,
        weight_grams=data.weight_grams,
        active=data.active,
        sync_status=SyncStatus.PENDING,
    )
    db.add(product)
    await db.flush()

    variants = data.variants or [
        VariantCreate(sku=f"SKU-{product.id}-DEFAULT", name="Default", stock=0)
    ]
    for variant in variants:
        db.add(_build_variant(product.id, variant))

    for index, url in enumerate(data.image_urls):
        db.add(ProductImage(product_id=product.id, url=url, sort_order=index))

    await db.flush()
    await invalidate_catalog(product.id)
    log.info("product_created", product_id=product.id, variant_count=len(variants))
    return product


def _build_variant(product_id: int, data: VariantCreate) -> ProductVariant:
    return ProductVariant(
        product_id=product_id,
        sku=data.sku,
        name=data.name,
        attributes=data.attributes,
        price_override=data.price_override,
        stock=data.stock,
        weight_grams=data.weight_grams,
        active=data.active,
    )


async def update_product(db: AsyncSession, product_id: int, data: ProductUpdate) -> Product:
    product = await get_product(db, product_id)

    changes = data.model_dump(exclude_unset=True)
    if "category_id" in changes and changes["category_id"] is not None:
        await get_category(db, changes["category_id"])

    for field, value in changes.items():
        setattr(product, field, value)

    # Any content change invalidates the Meta catalog copy.
    product.sync_status = SyncStatus.PENDING
    await db.flush()
    await invalidate_catalog(product_id)
    log.info("product_updated", product_id=product_id, fields=sorted(changes))
    return product


async def soft_delete_product(db: AsyncSession, product_id: int) -> Product:
    product = await get_product(db, product_id)
    product.soft_delete()
    product.active = False
    await db.flush()
    await invalidate_catalog(product_id)
    log.info("product_soft_deleted", product_id=product_id)
    return product


async def add_variant(db: AsyncSession, product_id: int, data: VariantCreate) -> ProductVariant:
    await get_product(db, product_id)
    await _assert_sku_free(db, [data.sku])

    variant = _build_variant(product_id, data)
    db.add(variant)
    await db.flush()
    await invalidate_catalog(product_id)
    return variant


async def update_variant(
    db: AsyncSession, product_id: int, variant_id: int, data: VariantUpdate
) -> ProductVariant:
    variant = await get_variant(db, variant_id)
    if variant.product_id != product_id:
        raise ValidationError("Variant does not belong to this product")

    changes = data.model_dump(exclude_unset=True)
    if "sku" in changes and changes["sku"] != variant.sku:
        await _assert_sku_free(db, [changes["sku"]], exclude_id=variant_id)

    for field, value in changes.items():
        setattr(variant, field, value)

    await db.flush()
    await invalidate_catalog(product_id)
    return variant


async def bulk_import(db: AsyncSession, items: list[Any], update_existing: bool) -> dict[str, Any]:
    """Import products by name, resolving or creating categories as needed."""
    result = {"created": 0, "updated": 0, "skipped": 0, "errors": []}

    for entry in items:
        try:
            category_id = entry.category_id
            if category_id is None and getattr(entry, "category_name", None):
                category_id = await _resolve_category_by_name(db, entry.category_name)

            existing = (
                await db.execute(
                    select(Product).where(
                        func.lower(Product.name) == entry.name.lower().strip(),
                        Product.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()

            if existing is not None:
                if not update_existing:
                    result["skipped"] += 1
                    continue
                existing.base_price = entry.base_price
                existing.description = entry.description
                existing.category_id = category_id
                existing.gst_rate = entry.gst_rate
                existing.hsn_code = entry.hsn_code
                existing.sync_status = SyncStatus.PENDING
                result["updated"] += 1
            else:
                payload = ProductCreate(
                    **{**entry.model_dump(exclude={"category_name"}), "category_id": category_id}
                )
                await create_product(db, payload)
                result["created"] += 1

            await db.flush()
        except Exception as exc:
            # One bad row must not abort the whole import.
            result["errors"].append(f"{entry.name}: {exc}")
            result["skipped"] += 1

    await invalidate_catalog()
    log.info("product_bulk_import", **{k: v for k, v in result.items() if k != "errors"})
    return result


async def _resolve_category_by_name(db: AsyncSession, name: str) -> int:
    stmt = select(Category).where(
        func.lower(Category.name) == name.lower().strip(), Category.deleted_at.is_(None)
    )
    category = (await db.execute(stmt)).scalar_one_or_none()
    if category is None:
        category = Category(name=name.strip(), slug=_slugify(name))
        db.add(category)
        await db.flush()
    return category.id


# --------------------------------------------------------------------------
# AI context
# --------------------------------------------------------------------------
async def build_ai_catalog_context(db: AsyncSession, max_products: int = 60) -> list[dict[str, Any]]:
    """Compact catalog for the AI system prompt — ids and prices only, no prose.

    Full descriptions would blow the token budget, so the model gets just
    enough to name products and emit valid product/variant IDs.
    """
    cached = await cache_get(CacheKeys.AI_CATALOG)
    if cached is not None:
        return cached

    stmt = (
        select(Product)
        .options(selectinload(Product.variants), selectinload(Product.category))
        .where(*_active_product_filter())
        .order_by(Product.created_at.desc())
        .limit(max_products)
    )
    products = (await db.execute(stmt)).scalars().unique().all()

    context = []
    for product in products:
        variants = [
            {
                "variant_id": v.id,
                "name": v.name,
                "price": float(v.price_override if v.price_override is not None else product.base_price),
                "stock": v.stock,
            }
            for v in product.variants
            if v.active and v.deleted_at is None
        ]
        context.append(
            {
                "id": product.id,
                "name": product.name,
                "price": float(product.base_price),
                "category": product.category.name if product.category else None,
                "variants": variants,
            }
        )

    await cache_set(CacheKeys.AI_CATALOG, context, TTL_PRODUCT_DETAIL)
    return context


def effective_price(product: Product, variant: ProductVariant | None) -> Decimal:
    if variant is not None and variant.price_override is not None:
        return variant.price_override
    return product.base_price
