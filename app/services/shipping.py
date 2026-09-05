"""Serviceable-area checks and zone/weight based shipping rates."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError, ValidationError
from app.models.shipping import ServiceablePincode, ShippingConfig
from logging_config import get_logger

log = get_logger(__name__)

DEFAULT_ZONE = "default"
GRAMS_PER_KG = Decimal("1000")


async def lookup_pincode(db: AsyncSession, pincode: str) -> ServiceablePincode | None:
    stmt = select(ServiceablePincode).where(
        ServiceablePincode.pincode == pincode.strip(),
        ServiceablePincode.active.is_(True),
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def is_serviceable(db: AsyncSession, pincode: str) -> bool:
    """An empty pincode table means the shop has not restricted delivery yet."""
    if await _pincode_table_is_empty(db):
        return True
    return await lookup_pincode(db, pincode) is not None


async def _pincode_table_is_empty(db: AsyncSession) -> bool:
    count = await db.scalar(
        select(func.count(ServiceablePincode.id)).where(ServiceablePincode.active.is_(True))
    )
    return not count


async def resolve_zone(db: AsyncSession, pincode: str) -> str:
    entry = await lookup_pincode(db, pincode)
    return entry.zone if entry else DEFAULT_ZONE


async def _find_rate(db: AsyncSession, zone: str, weight_grams: int) -> ShippingConfig | None:
    stmt = (
        select(ShippingConfig)
        .where(
            ShippingConfig.zone == zone,
            ShippingConfig.active.is_(True),
            ShippingConfig.min_weight <= weight_grams,
            ShippingConfig.max_weight >= weight_grams,
        )
        .order_by(ShippingConfig.base_cost)
        .limit(1)
    )
    rate = (await db.execute(stmt)).scalar_one_or_none()
    if rate is not None or zone == DEFAULT_ZONE:
        return rate
    # Fall back to the catch-all zone so an unmapped pincode is still quotable.
    return await _find_rate(db, DEFAULT_ZONE, weight_grams)


async def calculate_shipping(
    db: AsyncSession,
    pincode: str,
    weight_grams: int,
    order_value: Decimal,
) -> dict[str, Any]:
    zone = await resolve_zone(db, pincode)
    serviceable = await is_serviceable(db, pincode)

    if not serviceable:
        return {
            "pincode": pincode,
            "serviceable": False,
            "zone": None,
            "shipping_cost": Decimal("0.00"),
            "free_shipping_applied": False,
            "eta_days_min": None,
            "eta_days_max": None,
            "message": "We do not deliver to this pincode yet.",
        }

    rate = await _find_rate(db, zone, weight_grams)
    if rate is None:
        log.warning("no_shipping_rate_configured", zone=zone, weight_grams=weight_grams)
        return {
            "pincode": pincode,
            "serviceable": True,
            "zone": zone,
            "shipping_cost": Decimal("0.00"),
            "free_shipping_applied": True,
            "eta_days_min": 3,
            "eta_days_max": 7,
            "message": "Shipping is on us for this order.",
        }

    cost = rate.base_cost
    if rate.per_kg_cost:
        cost += rate.per_kg_cost * (Decimal(weight_grams) / GRAMS_PER_KG)

    free_applied = rate.free_above_amount is not None and order_value >= rate.free_above_amount
    if free_applied:
        cost = Decimal("0.00")

    return {
        "pincode": pincode,
        "serviceable": True,
        "zone": zone,
        "shipping_cost": cost.quantize(Decimal("0.01")),
        "free_shipping_applied": free_applied,
        "eta_days_min": rate.eta_days_min,
        "eta_days_max": rate.eta_days_max,
        "message": (
            "Free shipping applied."
            if free_applied
            else f"Delivered in {rate.eta_days_min}-{rate.eta_days_max} days."
        ),
    }


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------
async def list_shipping_configs(db: AsyncSession) -> list[ShippingConfig]:
    stmt = select(ShippingConfig).order_by(ShippingConfig.zone, ShippingConfig.min_weight)
    return list((await db.execute(stmt)).scalars().all())


async def create_shipping_config(db: AsyncSession, data: Any) -> ShippingConfig:
    config = ShippingConfig(**data.model_dump())
    db.add(config)
    await db.flush()
    log.info("shipping_config_created", zone=config.zone, config_id=config.id)
    return config


async def update_shipping_config(db: AsyncSession, config_id: int, data: Any) -> ShippingConfig:
    config = (
        await db.execute(select(ShippingConfig).where(ShippingConfig.id == config_id))
    ).scalar_one_or_none()
    if config is None:
        raise NotFoundError("Shipping config not found")

    changes = data.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(config, field, value)

    if config.min_weight >= config.max_weight:
        raise ValidationError("min_weight must be less than max_weight")

    await db.flush()
    return config


async def upload_pincodes(
    db: AsyncSession, entries: list[Any], replace_existing: bool
) -> dict[str, int]:
    result = {"created": 0, "updated": 0, "deactivated": 0}

    if replace_existing:
        existing_all = (await db.execute(select(ServiceablePincode))).scalars().all()
        for row in existing_all:
            row.active = False
            result["deactivated"] += 1

    by_pincode = {e.pincode.strip(): e for e in entries}
    found = (
        (
            await db.execute(
                select(ServiceablePincode).where(
                    ServiceablePincode.pincode.in_(list(by_pincode))
                )
            )
        )
        .scalars()
        .all()
    )
    existing = {row.pincode: row for row in found}

    for pincode, entry in by_pincode.items():
        row = existing.get(pincode)
        if row is None:
            db.add(
                ServiceablePincode(
                    pincode=pincode,
                    zone=entry.zone,
                    city=entry.city,
                    state=entry.state,
                    cod_available=entry.cod_available,
                    active=True,
                )
            )
            result["created"] += 1
        else:
            row.zone = entry.zone
            row.city = entry.city
            row.state = entry.state
            row.cod_available = entry.cod_available
            row.active = True
            result["updated"] += 1
            if replace_existing:
                result["deactivated"] -= 1

    await db.flush()
    log.info("pincodes_uploaded", **result)
    return result
