"""Cursor (keyset) pagination.

Offset pagination degrades on large tables and can skip or repeat rows when
data shifts between pages. Every list endpoint here pages on a strictly
ordered `(sort_column, id)` tuple instead.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Generic, Sequence, TypeVar

from pydantic import BaseModel, Field
from sqlalchemy import Select, and_, or_

from app.config import settings

T = TypeVar("T")


class CursorPage(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = Field(default=settings.default_page_size)


def encode_cursor(sort_value: Any, row_id: int) -> str:
    payload = json.dumps({"s": _serialize(sort_value), "i": row_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[Any, int] | None:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        return payload["s"], int(payload["i"])
    except Exception:
        # A malformed cursor is treated as "start from the beginning" rather
        # than a 500 — cursors are opaque and clients may mangle them.
        return None


def _serialize(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return settings.default_page_size
    return max(1, min(limit, settings.max_page_size))


def apply_cursor(
    stmt: Select,
    sort_column: Any,
    id_column: Any,
    cursor: str | None,
    descending: bool = True,
) -> Select:
    """Order by `(sort_column, id)` and seek past `cursor` if provided."""
    if cursor:
        decoded = decode_cursor(cursor)
        if decoded is not None:
            sort_value, row_id = decoded
            if descending:
                stmt = stmt.where(
                    or_(
                        sort_column < sort_value,
                        and_(sort_column == sort_value, id_column < row_id),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        sort_column > sort_value,
                        and_(sort_column == sort_value, id_column > row_id),
                    )
                )

    order = (
        (sort_column.desc(), id_column.desc())
        if descending
        else (sort_column.asc(), id_column.asc())
    )
    return stmt.order_by(*order)


def build_page(
    rows: Sequence[Any],
    limit: int,
    sort_attr: str = "created_at",
) -> tuple[list[Any], str | None, bool]:
    """Trim the over-fetched sentinel row and derive the next cursor.

    Callers should query `limit + 1` rows so `has_more` is exact without a
    second COUNT query.
    """
    has_more = len(rows) > limit
    items = list(rows[:limit])
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(getattr(last, sort_attr), last.id)
    return items, next_cursor, has_more
