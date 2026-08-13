from __future__ import annotations


def parse_cursor(cursor: str | None) -> int | None:
    if not cursor:
        return None
    try:
        value = int(cursor)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def next_cursor(last_id: int | None, has_more: bool) -> str | None:
    if not has_more or not last_id:
        return None
    return str(last_id)
