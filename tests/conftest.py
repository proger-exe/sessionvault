"""Shared pytest fixtures. Интеграционные тесты используют живой Postgres из
docker-compose (см. `DATABASE_URL` в `.env`).
"""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@pytest_asyncio.fixture()
async def db_session() -> AsyncGenerator[AsyncSession]:
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://tg:tg@localhost:5432/sessionvault",
    )
    parsed = make_url(url)
    host = parsed.host or "localhost"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=0.2):
            pass
    except OSError as exc:
        pytest.skip(f"Postgres is not available at {host}:{port}: {exc}")

    engine = create_async_engine(url, future=True, connect_args={"timeout": 1})
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"Postgres is not available at {url}: {exc}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()
    await engine.dispose()
