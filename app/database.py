from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings


def _engine_kwargs() -> dict:
    # SQLite (used by the test suite) does not support pool sizing args.
    if settings.database_url.startswith("sqlite"):
        return {"poolclass": NullPool}
    return {
        "pool_size": settings.db_pool_min,
        "max_overflow": max(settings.db_pool_max - settings.db_pool_min, 0),
        "pool_pre_ping": True,
        "pool_recycle": settings.db_pool_recycle_seconds,
    }


engine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    future=True,
    **_engine_kwargs(),
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a session that rolls back on error."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Standalone session for workers and scripts (commits on clean exit)."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_database() -> bool:
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def dispose_engine() -> None:
    await engine.dispose()
