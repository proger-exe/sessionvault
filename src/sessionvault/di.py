"""DI-контейнер: собирает зависимости и живёт весь runtime процесса.

Стиль — простой ленивый Container без magic. В хендлерах aiogram зависимости
пробрасываются через `workflow_data` / middleware, а сам Container живёт
как атрибут `Dispatcher["container"]`.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from redis.asyncio import Redis

from sessionvault.config import Settings, get_settings
from sessionvault.security.key_provider import KeyProvider
from sessionvault.security.key_provider import build_default as build_key_provider
from sessionvault.storage.db.session import get_engine, get_session_factory


@dataclass
class Container:
    settings: Settings
    bot: Bot
    dispatcher: Dispatcher
    redis: Redis[bytes]
    key_provider: KeyProvider

    async def close(self) -> None:
        await self.bot.session.close()
        await self.redis.close()
        await get_engine().dispose()


def build_container() -> Container:
    settings = get_settings()
    bot = Bot(token=settings.bot_token.get_secret_value())
    dispatcher = Dispatcher()
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    key_provider = build_key_provider(settings)

    # Инициализация «холодных» зависимостей — engine создаётся лениво.
    get_engine()
    get_session_factory()

    container = Container(
        settings=settings,
        bot=bot,
        dispatcher=dispatcher,
        redis=redis,
        key_provider=key_provider,
    )
    # Пробрасываем в workflow_data — хендлеры получают через инъекцию имен.
    dispatcher["container"] = container
    dispatcher["settings"] = settings
    dispatcher["key_provider"] = key_provider
    dispatcher["redis"] = redis
    return container
