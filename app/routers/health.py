from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.database import SessionLocal
from app.redis import get_redis_client

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    db_ok = False
    redis_ok = False

    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    try:
        redis_client = get_redis_client()
        if hasattr(redis_client, "ping"):
            redis_client.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    status = "ok" if db_ok and redis_ok else "degraded"
    return {"status": status, "db": "ok" if db_ok else "down", "redis": "ok" if redis_ok else "down", "whatsapp_api": "unknown", "cashfree_api": "unknown"}


@router.get("/health/ready")
def readiness() -> dict:
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
        return {"ready": True}
    except Exception:
        return {"ready": False}


@router.get("/metrics")
def metrics() -> dict:
    return {
        "request_count": 0,
        "request_latency_ms": 0,
        "error_rate": 0,
        "ai_token_usage": 0,
        "queue_depth": 0,
    }
