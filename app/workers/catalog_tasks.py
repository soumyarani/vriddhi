"""Meta Commerce catalogue synchronisation tasks.

`sync_product_task` is retried by arq up to `MAX_TRIES`. Catalogue sync is
eventually-consistent by design: a failed sync leaves the product marked
`failed` and the nightly sweep picks it up again, so a Meta outage delays the
catalogue rather than blocking a product edit.
"""

from __future__ import annotations

from typing import Any

from app.database import session_scope
from logging_config import get_logger

log = get_logger(__name__)

MAX_TRIES = 3


async def sync_product_task(ctx: dict, product_id: int) -> dict[str, Any]:
    from app.services.catalog_sync import sync_product

    async with session_scope() as db:
        ok = await sync_product(db, product_id)

    if not ok and ctx.get("job_try", 1) < MAX_TRIES:
        # sync_product never raises, so raising here is what asks arq to retry.
        raise RuntimeError(f"catalog sync failed for product {product_id}")

    return {"product_id": product_id, "synced": ok}


async def delete_product_task(ctx: dict, product_id: int) -> dict[str, Any]:
    from app.services.catalog_sync import delete_from_catalog

    async with session_scope() as db:
        ok = await delete_from_catalog(db, product_id)
    return {"product_id": product_id, "deleted": ok}


async def sync_pending_task(ctx: dict) -> dict[str, Any]:
    """Sweep everything not yet in sync. Runs nightly."""
    from app.services.catalog_sync import sync_pending

    async with session_scope() as db:
        return await sync_pending(db, limit=100)
