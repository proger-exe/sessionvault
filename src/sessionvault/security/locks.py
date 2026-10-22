"""Redis account-lock: гарантия «один write-worker на аккаунт единовременно».

Использование:

    async with AccountLock(redis, account_id="…", holder="export-worker") as lock:
        if not lock.acquired:
            raise AlreadyLockedError()
        ...  # безопасная work-секция

`acquired` = False если лок уже взят кем-то другим — вызывающий сам решает,
что делать (ждать / отклонять / ставить в очередь).

Unlock идёт через Lua-скрипт: не удаляем чужой лок, если наш TTL истёк
и лок захватил кто-то ещё под тем же ключом.
"""

from __future__ import annotations

import secrets
from types import TracebackType

from redis.asyncio import Redis

_UNLOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

_DEFAULT_TTL_MS = 60_000


class AccountLock:
    """Async context manager для эксклюзивной работы над аккаунтом."""

    def __init__(
        self,
        redis: Redis[bytes],
        *,
        account_id: str,
        holder: str,
        ttl_ms: int = _DEFAULT_TTL_MS,
    ) -> None:
        self._redis = redis
        self._key = f"acc:lock:{account_id}"
        self._holder = holder
        self._token = secrets.token_hex(16)
        self._ttl_ms = ttl_ms
        self.acquired = False

    async def __aenter__(self) -> AccountLock:
        # SET key token NX PX ttl_ms — атомарная попытка захвата.
        result = await self._redis.set(
            self._key, self._token, nx=True, px=self._ttl_ms
        )
        self.acquired = bool(result)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self.acquired:
            return
        # Атомарный CAS-unlock через Lua.
        try:
            await self._redis.eval(_UNLOCK_SCRIPT, 1, self._key, self._token)  # type: ignore[no-untyped-call]
        finally:
            self.acquired = False

    async def extend(self, ttl_ms: int) -> bool:
        """Продлить TTL, если мы всё ещё держатель лока."""
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("pexpire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        rv = await self._redis.eval(script, 1, self._key, self._token, ttl_ms)  # type: ignore[no-untyped-call]
        return bool(rv)
