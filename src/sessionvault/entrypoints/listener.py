"""CLI-точка входа для listener'а service-уведов: `sessionvault-listener`.

Отдельный процесс: держит kurigram-клиенты всех active-аккаунтов, форвардит
любое сообщение из chat_id=777000 владельцу через бота (Bot API, без polling).
"""

from __future__ import annotations

import asyncio

from aiogram import Bot

from sessionvault.config import get_settings
from sessionvault.logging import setup_logging
from sessionvault.mtproto.service_listener import run as run_listener
from sessionvault.security.key_provider import build_default as build_key_provider
from sessionvault.storage.db.session import get_session_factory


async def _main() -> None:
    settings = get_settings()
    key_provider = build_key_provider(settings)
    session_factory = get_session_factory()

    bot = Bot(token=settings.bot_token.get_secret_value())
    try:
        await run_listener(
            settings=settings,
            key_provider=key_provider,
            session_factory=session_factory,
            bot=bot,
        )
    finally:
        await bot.session.close()


def main() -> None:
    setup_logging()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
