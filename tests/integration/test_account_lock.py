"""AccountLock: захват-возврат, отклонение при уже взятом, TTL."""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncGenerator
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import RedisError

from sessionvault.security.locks import AccountLock


@pytest_asyncio.fixture()
async def redis() -> AsyncGenerator[Redis[bytes]]:
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    try:
        with socket.create_connection((host, port), timeout=0.2):
            pass
    except OSError as exc:
        pytest.skip(f"Redis is not available at {host}:{port}: {exc}")

    client = Redis.from_url(
        url,
        decode_responses=False,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        await client.ping()
    except RedisError as exc:
        await client.close()
        pytest.skip(f"Redis is not available at {url}: {exc}")
    try:
        yield client
    finally:
        await client.close()


async def test_acquire_release(redis: Redis[bytes]) -> None:
    async with AccountLock(redis, account_id="acc-1", holder="worker-a") as lock:
        assert lock.acquired is True
    # После выхода лок свободен — можем взять повторно.
    async with AccountLock(redis, account_id="acc-1", holder="worker-b") as lock:
        assert lock.acquired is True


async def test_second_acquire_rejected_while_held(redis: Redis[bytes]) -> None:
    async with AccountLock(redis, account_id="acc-2", holder="a") as first:
        assert first.acquired is True
        async with AccountLock(redis, account_id="acc-2", holder="b") as second:
            assert second.acquired is False


async def test_extend_only_by_holder(redis: Redis[bytes]) -> None:
    async with AccountLock(redis, account_id="acc-3", holder="a", ttl_ms=500) as first:
        assert first.acquired is True
        assert await first.extend(2000) is True
    # После освобождения продление не работает.
    lock = AccountLock(redis, account_id="acc-3", holder="a", ttl_ms=500)
    assert await lock.extend(2000) is False


async def test_expired_lock_can_be_reacquired(redis: Redis[bytes]) -> None:
    lock = AccountLock(redis, account_id="acc-4", holder="a", ttl_ms=200)
    await lock.__aenter__()
    assert lock.acquired
    await asyncio.sleep(0.4)
    # Не выходим из первого лока — просто ждём TTL и берём заново.
    async with AccountLock(redis, account_id="acc-4", holder="b") as second:
        assert second.acquired is True
    await lock.__aexit__(None, None, None)  # cleanup — no-op, чужой уже владеет
