#!/usr/bin/env python
"""Seed a realistic Indian demo catalogue so the WhatsApp flow can be demoed end to end.

Creates categories, products with size/colour variants, product images, coupons
(including one deliberately expired so the validation path is demoable),
shipping zones, serviceable pincodes and one admin agent.

The script is idempotent: every row is looked up by a natural key (slug, Meta
retailer id, SKU, coupon code, pincode, email) before insert, so running it
repeatedly neither duplicates nor crashes.

Prices are GST-inclusive, matching Indian retail practice and app/services/tax.py,
which extracts tax out of the gross rather than adding it on top.

Prerequisites: the schema must already exist. Run `alembic upgrade head` first.

Usage:
    .venv/bin/python seed_data.py
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import dispose_engine, session_scope
from app.models import (
    Agent,
    Category,
    Coupon,
    Product,
    ProductImage,
    ProductVariant,
    ServiceablePincode,
    ShippingConfig,
)
from app.models.base import utcnow
from app.models.enums import AgentRole, DiscountType

# Placeholder CDN. Swap for real asset URLs before a customer-facing demo.
IMAGE_BASE = "https://cdn.example.com/whatsapp-commerce"

stats: Counter[str] = Counter()


def D(value: str) -> Decimal:
    return Decimal(value)


# ---------------------------------------------------------------------------
# Catalogue data
#
# gst_rate / hsn_code follow real Indian GST schedules rather than one flat
# rate, so invoices exercise more than a single tax bracket:
#   - apparel under Rs.1000        -> 5%   (HSN 62xx)
#   - apparel Rs.1000 and above    -> 12%  (HSN 5407 / 62xx)
#   - kitchen and metal houseware  -> 12%  (HSN 73xx / 74xx / 76xx)
#   - electronics and cosmetics    -> 18%  (HSN 85xx / 33xx / 34xx)
# ---------------------------------------------------------------------------

CATEGORIES: list[dict[str, Any]] = [
    {
        "slug": "ethnic-wear",
        "name": "Ethnic Wear",
        "description": "Handpicked sarees, kurtas and kurtis from Indian weavers.",
        "sort_order": 1,
    },
    {
        "slug": "electronics",
        "name": "Electronics & Accessories",
        "description": "Everyday audio, charging and connectivity essentials.",
        "sort_order": 2,
    },
    {
        "slug": "home-kitchen",
        "name": "Home & Kitchen",
        "description": "Cookware and serveware built for the Indian kitchen.",
        "sort_order": 3,
    },
    {
        "slug": "beauty-personal-care",
        "name": "Beauty & Personal Care",
        "description": "Ayurvedic and cold-pressed skin and hair care.",
        "sort_order": 4,
    },
]

PRODUCTS: list[dict[str, Any]] = [
    # ---------------- Ethnic Wear ----------------
    {
        "retailer_id": "WC-ETH-SAREE-BANARASI",
        "category": "ethnic-wear",
        "name": "Banarasi Silk Saree with Zari Border",
        "description": (
            "Handwoven Banarasi silk saree with traditional gold zari border and "
            "an unstitched blouse piece. Dry clean only."
        ),
        "base_price": "2499.00",
        "gst_rate": "12.00",
        "hsn_code": "5407",
        "weight_grams": 700,
        "variants": [
            {"sku": "ETH-SAR-BAN-MRN", "name": "Maroon", "attributes": {"colour": "Maroon"}, "stock": 18},
            {"sku": "ETH-SAR-BAN-RBL", "name": "Royal Blue", "attributes": {"colour": "Royal Blue"}, "stock": 12},
            {"sku": "ETH-SAR-BAN-EMG", "name": "Emerald Green", "attributes": {"colour": "Emerald Green"}, "stock": 7},
        ],
    },
    {
        "retailer_id": "WC-ETH-ANARKALI-SET",
        "category": "ethnic-wear",
        "name": "Cotton Anarkali Kurta Set with Dupatta",
        "description": "Breathable cotton Anarkali kurta with palazzo and printed dupatta.",
        "base_price": "1299.00",
        "gst_rate": "12.00",
        "hsn_code": "6211",
        "weight_grams": 550,
        "variants": [
            {"sku": "ETH-ANK-S", "name": "Size S", "attributes": {"size": "S"}, "stock": 14, "weight_grams": 520},
            {"sku": "ETH-ANK-M", "name": "Size M", "attributes": {"size": "M"}, "stock": 22, "weight_grams": 550},
            {"sku": "ETH-ANK-L", "name": "Size L", "attributes": {"size": "L"}, "stock": 9, "weight_grams": 580},
        ],
    },
    {
        "retailer_id": "WC-ETH-CHIKANKARI-KURTI",
        "category": "ethnic-wear",
        "name": "Lucknowi Chikankari Straight Kurti",
        "description": "Hand-embroidered Chikankari kurti in soft rayon. Machine washable.",
        # Under Rs.1000, so it falls in the 5% apparel slab.
        "base_price": "899.00",
        "gst_rate": "5.00",
        "hsn_code": "6206",
        "weight_grams": 300,
        "variants": [
            {"sku": "ETH-CHK-M", "name": "Size M", "attributes": {"size": "M"}, "stock": 25},
            {"sku": "ETH-CHK-L", "name": "Size L", "attributes": {"size": "L"}, "stock": 17},
            {"sku": "ETH-CHK-XL", "name": "Size XL", "attributes": {"size": "XL"}, "stock": 0},
        ],
    },
    # ---------------- Electronics ----------------
    {
        "retailer_id": "WC-ELE-EARBUDS-ENC",
        "category": "electronics",
        "name": "Wireless Earbuds with ENC, 40H Playtime",
        "description": "TWS earbuds with environmental noise cancellation and USB-C fast charge.",
        "base_price": "1799.00",
        "gst_rate": "18.00",
        "hsn_code": "8518",
        "weight_grams": 120,
        "variants": [
            {"sku": "ELE-EAR-BLK", "name": "Midnight Black", "attributes": {"colour": "Black"}, "stock": 40},
            {"sku": "ELE-EAR-WHT", "name": "Pearl White", "attributes": {"colour": "White"}, "stock": 26},
        ],
    },
    {
        "retailer_id": "WC-ELE-POWERBANK",
        "category": "electronics",
        "name": "Fast Charging Power Bank 22.5W",
        "description": "Dual-output power bank with USB-C PD and 18W QC. BIS certified.",
        "base_price": "2199.00",
        "gst_rate": "18.00",
        "hsn_code": "8507",
        "weight_grams": 420,
        "variants": [
            {"sku": "ELE-PWB-20K", "name": "20000mAh", "attributes": {"capacity": "20000mAh"}, "stock": 30},
            # Cheaper capacity uses price_override to exercise variant pricing.
            {
                "sku": "ELE-PWB-10K",
                "name": "10000mAh",
                "attributes": {"capacity": "10000mAh"},
                "stock": 45,
                "price_override": "1399.00",
                "weight_grams": 240,
            },
        ],
    },
    {
        "retailer_id": "WC-ELE-USBC-CABLE",
        "category": "electronics",
        "name": "Braided USB-C to USB-C Cable 60W",
        "description": "Nylon-braided 60W charging and data cable with reinforced connectors.",
        "base_price": "399.00",
        "gst_rate": "18.00",
        "hsn_code": "8544",
        "weight_grams": 90,
        "variants": [
            {"sku": "ELE-CBL-1M", "name": "1 metre", "attributes": {"length": "1m"}, "stock": 80},
            {
                "sku": "ELE-CBL-2M",
                "name": "2 metre",
                "attributes": {"length": "2m"},
                "stock": 55,
                "price_override": "549.00",
                "weight_grams": 140,
            },
        ],
    },
    # ---------------- Home & Kitchen ----------------
    {
        "retailer_id": "WC-HOM-PRESSURE-COOKER",
        "category": "home-kitchen",
        "name": "Triply Stainless Steel Pressure Cooker",
        "description": "Induction-friendly triply base cooker with ISI-marked safety valve.",
        "base_price": "2899.00",
        "gst_rate": "12.00",
        "hsn_code": "7323",
        "weight_grams": 2200,
        "variants": [
            {"sku": "HOM-PRC-3L", "name": "3 Litre", "attributes": {"capacity": "3L"}, "stock": 16, "weight_grams": 1800},
            {
                "sku": "HOM-PRC-5L",
                "name": "5 Litre",
                "attributes": {"capacity": "5L"},
                "stock": 11,
                "price_override": "3499.00",
                "weight_grams": 2400,
            },
        ],
    },
    {
        "retailer_id": "WC-HOM-DOSA-TAWA",
        "category": "home-kitchen",
        "name": "Non-Stick Dosa Tawa with Cool-Touch Handle",
        "description": "PFOA-free non-stick coating, gas and induction compatible.",
        "base_price": "749.00",
        "gst_rate": "12.00",
        "hsn_code": "7615",
        "weight_grams": 1100,
        "variants": [
            {"sku": "HOM-TWA-28", "name": "28 cm", "attributes": {"diameter": "28cm"}, "stock": 24},
            {
                "sku": "HOM-TWA-30",
                "name": "30 cm",
                "attributes": {"diameter": "30cm"},
                "stock": 19,
                "price_override": "899.00",
                "weight_grams": 1300,
            },
        ],
    },
    {
        "retailer_id": "WC-HOM-COPPER-BOTTLE",
        "category": "home-kitchen",
        "name": "Pure Copper Water Bottle, Hammered Finish",
        "description": "Seamless hammered copper bottle for Ayurvedic storage. Hand wash only.",
        "base_price": "649.00",
        "gst_rate": "12.00",
        "hsn_code": "7418",
        "weight_grams": 400,
        "variants": [
            {"sku": "HOM-CPB-750", "name": "750 ml", "attributes": {"capacity": "750ml"}, "stock": 33},
            {
                "sku": "HOM-CPB-1L",
                "name": "1 Litre",
                "attributes": {"capacity": "1L"},
                "stock": 21,
                "price_override": "799.00",
                "weight_grams": 480,
            },
        ],
    },
    # ---------------- Beauty & Personal Care ----------------
    {
        "retailer_id": "WC-BEA-KUMKUMADI-SERUM",
        "category": "beauty-personal-care",
        "name": "Kumkumadi Brightening Face Serum",
        "description": "Saffron and Ayurvedic oil blend for overnight glow. Paraben free.",
        "base_price": "899.00",
        "gst_rate": "18.00",
        "hsn_code": "3304",
        "weight_grams": 120,
        "variants": [
            {"sku": "BEA-KKS-30", "name": "30 ml", "attributes": {"volume": "30ml"}, "stock": 38},
            {
                "sku": "BEA-KKS-50",
                "name": "50 ml",
                "attributes": {"volume": "50ml"},
                "stock": 20,
                "price_override": "1349.00",
                "weight_grams": 165,
            },
        ],
    },
    {
        "retailer_id": "WC-BEA-COCONUT-HAIR-OIL",
        "category": "beauty-personal-care",
        "name": "Cold-Pressed Virgin Coconut Hair Oil",
        "description": "Single-origin Kerala coconut oil, wood-pressed and unrefined.",
        "base_price": "449.00",
        "gst_rate": "18.00",
        "hsn_code": "3305",
        "weight_grams": 350,
        "variants": [
            {"sku": "BEA-CHO-200", "name": "200 ml", "attributes": {"volume": "200ml"}, "stock": 60},
            {
                "sku": "BEA-CHO-500",
                "name": "500 ml",
                "attributes": {"volume": "500ml"},
                "stock": 34,
                "price_override": "899.00",
                "weight_grams": 700,
            },
        ],
    },
    {
        "retailer_id": "WC-BEA-NEEM-SOAP",
        "category": "beauty-personal-care",
        "name": "Ayurvedic Neem & Tulsi Handmade Soap",
        "description": "Cold-process soap with neem, tulsi and coconut oil. No SLS.",
        "base_price": "320.00",
        "gst_rate": "18.00",
        "hsn_code": "3401",
        "weight_grams": 400,
        "variants": [
            {"sku": "BEA-NTS-P4", "name": "Pack of 4", "attributes": {"pack": "4"}, "stock": 70},
            {
                "sku": "BEA-NTS-P6",
                "name": "Pack of 6",
                "attributes": {"pack": "6"},
                "stock": 42,
                "price_override": "449.00",
                "weight_grams": 600,
            },
        ],
    },
]

COUPONS: list[dict[str, Any]] = [
    {
        "code": "WELCOME10",
        "description": "10% off your first order, capped at Rs.200.",
        "discount_type": DiscountType.PERCENT,
        "discount_value": "10.00",
        "max_discount_amount": "200.00",
        "min_order": "999.00",
        "max_uses": 1000,
        "per_user_limit": 1,
        "expires_in_days": 90,
    },
    {
        "code": "FLAT250",
        "description": "Flat Rs.250 off on orders above Rs.1999.",
        "discount_type": DiscountType.FLAT,
        "discount_value": "250.00",
        "max_discount_amount": None,
        "min_order": "1999.00",
        "max_uses": 500,
        "per_user_limit": 2,
        "expires_in_days": 45,
    },
    {
        # Deliberately expired so the "coupon expired" branch is demoable.
        "code": "DIWALI25",
        "description": "Expired Diwali offer - 25% off, capped at Rs.500 (demo of expiry path).",
        "discount_type": DiscountType.PERCENT,
        "discount_value": "25.00",
        "max_discount_amount": "500.00",
        "min_order": "1499.00",
        "max_uses": 2000,
        "per_user_limit": 1,
        "expires_in_days": -30,
    },
]

# zone names are free-form; "default" is the catch-all app/services/shipping.py
# falls back to when a pincode maps to no configured zone.
SHIPPING_CONFIGS: list[dict[str, Any]] = [
    {
        "zone": "local", "label": "Bengaluru local", "min_weight": 0, "max_weight": 500,
        "base_cost": "40.00", "per_kg_cost": "0.00", "free_above_amount": "999.00",
        "eta_days_min": 1, "eta_days_max": 3,
    },
    {
        "zone": "local", "label": "Bengaluru local (heavy)", "min_weight": 501, "max_weight": 5000,
        "base_cost": "60.00", "per_kg_cost": "20.00", "free_above_amount": "999.00",
        "eta_days_min": 1, "eta_days_max": 3,
    },
    {
        "zone": "metro", "label": "Metro cities", "min_weight": 0, "max_weight": 500,
        "base_cost": "60.00", "per_kg_cost": "0.00", "free_above_amount": "1499.00",
        "eta_days_min": 2, "eta_days_max": 5,
    },
    {
        "zone": "metro", "label": "Metro cities (heavy)", "min_weight": 501, "max_weight": 5000,
        "base_cost": "90.00", "per_kg_cost": "30.00", "free_above_amount": "1499.00",
        "eta_days_min": 2, "eta_days_max": 5,
    },
    {
        "zone": "rest_of_india", "label": "Rest of India", "min_weight": 0, "max_weight": 5000,
        "base_cost": "110.00", "per_kg_cost": "40.00", "free_above_amount": "2499.00",
        "eta_days_min": 4, "eta_days_max": 8,
    },
    {
        "zone": "default", "label": "Catch-all fallback", "min_weight": 0, "max_weight": 1000000,
        "base_cost": "140.00", "per_kg_cost": "50.00", "free_above_amount": None,
        "eta_days_min": 5, "eta_days_max": 10,
    },
]

PINCODES: list[dict[str, Any]] = [
    {"pincode": "560001", "zone": "local", "city": "Bengaluru", "state": "Karnataka", "cod_available": True},
    {"pincode": "560034", "zone": "local", "city": "Bengaluru", "state": "Karnataka", "cod_available": True},
    {"pincode": "560103", "zone": "local", "city": "Bengaluru", "state": "Karnataka", "cod_available": True},
    {"pincode": "400001", "zone": "metro", "city": "Mumbai", "state": "Maharashtra", "cod_available": True},
    {"pincode": "110001", "zone": "metro", "city": "New Delhi", "state": "Delhi", "cod_available": True},
    {"pincode": "600001", "zone": "metro", "city": "Chennai", "state": "Tamil Nadu", "cod_available": False},
    {"pincode": "700001", "zone": "metro", "city": "Kolkata", "state": "West Bengal", "cod_available": False},
    {"pincode": "500001", "zone": "metro", "city": "Hyderabad", "state": "Telangana", "cod_available": True},
    {"pincode": "302001", "zone": "rest_of_india", "city": "Jaipur", "state": "Rajasthan", "cod_available": False},
    {"pincode": "781001", "zone": "rest_of_india", "city": "Guwahati", "state": "Assam", "cod_available": False},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def get_or_create(
    db: AsyncSession, model: type, defaults: dict[str, Any], **lookup: Any
) -> tuple[Any, bool]:
    """Fetch a row by its natural key, or insert it. Returns (row, created)."""
    stmt = select(model)
    for column, value in lookup.items():
        stmt = stmt.where(getattr(model, column) == value)
    existing = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    if existing is not None:
        stats[f"{model.__name__}:skipped"] += 1
        return existing, False

    row = model(**{**lookup, **defaults})
    db.add(row)
    # Flush so dependent rows can reference the generated primary key.
    await db.flush()
    stats[f"{model.__name__}:created"] += 1
    return row, True


def admin_email() -> str:
    """Admin address on the first domain the auth layer will accept."""
    domains = settings.agent_domains
    if domains:
        return f"admin@{domains[0]}"
    # AGENT_ALLOWED_DOMAINS is empty by default; fall back to a placeholder and
    # warn, because sign-in will be refused until the domain is configured.
    return "admin@example.com"


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def seed_categories(db: AsyncSession) -> dict[str, Category]:
    by_slug: dict[str, Category] = {}
    for spec in CATEGORIES:
        category, _ = await get_or_create(
            db,
            Category,
            defaults={
                "name": spec["name"],
                "description": spec["description"],
                "sort_order": spec["sort_order"],
                "image_url": f"{IMAGE_BASE}/categories/{spec['slug']}.jpg",
                "active": True,
            },
            slug=spec["slug"],
        )
        by_slug[spec["slug"]] = category
    return by_slug


async def seed_products(db: AsyncSession, categories: dict[str, Category]) -> None:
    for spec in PRODUCTS:
        category = categories[spec["category"]]
        product, _ = await get_or_create(
            db,
            Product,
            defaults={
                "name": spec["name"],
                "description": spec["description"],
                "base_price": D(spec["base_price"]),
                "category_id": category.id,
                "gst_rate": D(spec["gst_rate"]),
                "hsn_code": spec["hsn_code"],
                "weight_grams": spec["weight_grams"],
                "image_urls": [f"{IMAGE_BASE}/products/{spec['retailer_id'].lower()}-1.jpg"],
                "active": True,
            },
            meta_retailer_id=spec["retailer_id"],
        )

        for position, variant_spec in enumerate(spec["variants"]):
            price_override = variant_spec.get("price_override")
            await get_or_create(
                db,
                ProductVariant,
                defaults={
                    "product_id": product.id,
                    "name": variant_spec["name"],
                    "attributes": variant_spec["attributes"],
                    "stock": variant_spec["stock"],
                    "price_override": D(price_override) if price_override else None,
                    "weight_grams": variant_spec.get("weight_grams"),
                    "meta_retailer_id": f"{spec['retailer_id']}-{position + 1}",
                    "active": True,
                },
                sku=variant_spec["sku"],
            )

        # One catalogue image per product, keyed on URL so re-runs are no-ops.
        image_url = f"{IMAGE_BASE}/products/{spec['retailer_id'].lower()}-1.jpg"
        await get_or_create(
            db,
            ProductImage,
            defaults={"alt_text": spec["name"], "sort_order": 0},
            product_id=product.id,
            url=image_url,
        )


async def seed_coupons(db: AsyncSession) -> None:
    now = utcnow()
    for spec in COUPONS:
        max_discount = spec["max_discount_amount"]
        await get_or_create(
            db,
            Coupon,
            defaults={
                "description": spec["description"],
                "discount_type": spec["discount_type"],
                "discount_value": D(spec["discount_value"]),
                "max_discount_amount": D(max_discount) if max_discount else None,
                "min_order": D(spec["min_order"]),
                "max_uses": spec["max_uses"],
                "per_user_limit": spec["per_user_limit"],
                "starts_at": now - timedelta(days=1),
                "expires_at": now + timedelta(days=spec["expires_in_days"]),
                "active": True,
            },
            code=spec["code"],
        )


async def seed_shipping(db: AsyncSession) -> None:
    for spec in SHIPPING_CONFIGS:
        free_above = spec["free_above_amount"]
        await get_or_create(
            db,
            ShippingConfig,
            defaults={
                "label": spec["label"],
                "base_cost": D(spec["base_cost"]),
                "per_kg_cost": D(spec["per_kg_cost"]),
                "free_above_amount": D(free_above) if free_above else None,
                "eta_days_min": spec["eta_days_min"],
                "eta_days_max": spec["eta_days_max"],
                "active": True,
            },
            # No unique constraint exists, so the zone/weight bracket is the
            # natural key.
            zone=spec["zone"],
            min_weight=spec["min_weight"],
            max_weight=spec["max_weight"],
        )

    for spec in PINCODES:
        await get_or_create(
            db,
            ServiceablePincode,
            defaults={
                "zone": spec["zone"],
                "city": spec["city"],
                "state": spec["state"],
                "cod_available": spec["cod_available"],
                "active": True,
            },
            pincode=spec["pincode"],
        )


async def seed_agent(db: AsyncSession) -> str:
    email = admin_email()
    await get_or_create(
        db,
        Agent,
        defaults={
            "name": "Store Admin",
            "role": AgentRole.ADMIN,
            "active": True,
        },
        email=email,
    )
    return email


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def print_summary(email: str) -> None:
    entities = [
        ("Categories", "Category"),
        ("Products", "Product"),
        ("Variants", "ProductVariant"),
        ("Product images", "ProductImage"),
        ("Coupons", "Coupon"),
        ("Shipping rates", "ShippingConfig"),
        ("Serviceable pincodes", "ServiceablePincode"),
        ("Admin agents", "Agent"),
    ]
    print("\n  Seed summary")
    print("  " + "-" * 46)
    print(f"  {'':<22}{'created':>10}{'skipped':>10}")
    total_created = total_skipped = 0
    for label, key in entities:
        created = stats[f"{key}:created"]
        skipped = stats[f"{key}:skipped"]
        total_created += created
        total_skipped += skipped
        print(f"  {label:<22}{created:>10}{skipped:>10}")
    print("  " + "-" * 46)
    print(f"  {'TOTAL':<22}{total_created:>10}{total_skipped:>10}")
    print(f"\n  Admin agent: {email}")
    if not settings.agent_domains:
        print(
            "  WARNING: AGENT_ALLOWED_DOMAINS is empty, so this agent cannot sign in.\n"
            "           Set AGENT_ALLOWED_DOMAINS in .env and re-run to seed a\n"
            "           matching admin address."
        )
    print("  'skipped' rows already existed - the seed is idempotent.\n")


async def main() -> int:
    print(f"Seeding demo catalogue into: {settings.database_url}")
    try:
        async with session_scope() as db:
            categories = await seed_categories(db)
            await seed_products(db, categories)
            await seed_coupons(db)
            await seed_shipping(db)
            email = await seed_agent(db)
    except SQLAlchemyError as exc:
        print(f"\nSeeding failed: {exc}", file=sys.stderr)
        print(
            "Is the schema present? Run `alembic upgrade head` first.",
            file=sys.stderr,
        )
        return 1
    finally:
        await dispose_engine()

    print_summary(email)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
