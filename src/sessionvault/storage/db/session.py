"""Async engine + sessionmaker.

Один engine на процесс, sessionmaker выдаёт сессии. `session_scope()` —
async context manager для доменных сервисов.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sessionvault.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession]:
    """Использование:

        async with session_scope() as session:
            ...
            await session.commit()
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        await session.close()


async def dispose_engine() -> None:
    """Закрыть engine при shutdown-е приложения."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
