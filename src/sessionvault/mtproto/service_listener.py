"""Long-lived listener: форвардит service-уведы Telegram (chat 777000) владельцу
через бота.

При старте: разовый снапшот active-аккаунтов из БД. Для каждого поднимается
persistent kurigram Client с `no_updates=False`, вешается handler на
`chat_id=SERVICE_CHAT_ID`. При приходе сообщения — если удалось выделить код,
шлём краткое `🔢 12345 <phone>`; иначе форвардим сырое тело (Owner просил
максимум лениво: все служебные уведы).

# ponytail: MVP — новые аккаунты подхватятся только после рестарта listener.
# upgrade: periodic reload (asyncio.timer) когда реально начнут добавлять.
# ponytail: два клиента (listener + существующий "Получить код") на один
# auth_key — Telegram это допускает, владелец получит дубль. upgrade: убрать
# кнопку или дедуп через redis SET.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import signal
import tempfile
import uuid
from html import escape
from pathlib import Path
from typing import Any

from aiogram import Bot
from pyrogram import Client, filters  # type: ignore[attr-defined]
from pyrogram.handlers import MessageHandler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from sessionvault.config import Settings
from sessionvault.domain.accounts.service import AccountsService
from sessionvault.mtproto.login_code import SERVICE_CHAT_ID, extract_login_code
from sessionvault.mtproto.session_file import build_pyrogram_session_file
from sessionvault.security.key_provider import KeyProvider
from sessionvault.storage.db.models import AccountStatus, TelegramAccount

log = logging.getLogger(__name__)

_TEXT_LIMIT = 1500  # обрезаем длинные service-уведы для сообщения в чат


async def _load_active_accounts(
    session_factory: async_sessionmaker[Any],
) -> list[uuid.UUID]:
    """Все active-аккаунты — только id, secrets будем брать поштучно."""
    async with session_factory() as session:
        stmt = select(TelegramAccount.id).where(
            TelegramAccount.status == AccountStatus.ACTIVE
        )
        rows = (await session.execute(stmt)).scalars().all()
    return [row for row in rows]


def _make_handler(
    *, bot: Bot, owner_id: int, phone_e164: str, account_id: uuid.UUID
) -> Any:
    async def handle(_client: Client, message: Any) -> None:
        text = str(
            getattr(message, "text", None) or getattr(message, "caption", None) or ""
        )
        code = extract_login_code(text) if text else None
        try:
            if code:
                await bot.send_message(
                    owner_id,
                    f"🔢 <code>{escape(code)}</code>\n"
                    f"📱 <code>{escape(phone_e164)}</code>",
                    parse_mode="HTML",
                )
            elif text:
                snippet = text[:_TEXT_LIMIT]
                if len(text) > _TEXT_LIMIT:
                    snippet += "\n…"
                await bot.send_message(
                    owner_id,
                    f"📩 <b>{escape(phone_e164)}</b>\n{escape(snippet)}",
                    parse_mode="HTML",
                )
        except Exception:
            # доставка в бот не критична — не роняем listener
            log.exception(
                "failed to forward service message account_id=%s", account_id
            )

    return handle


async def _run_one_client(
    *,
    account_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
    settings: Settings,
    key_provider: KeyProvider,
    bot: Bot,
    session_root: Path,
    stop_event: asyncio.Event,
) -> None:
    async with session_factory() as session:
        secrets = await AccountsService(session, key_provider).get_secrets(account_id)
    if secrets is None:
        log.warning("secrets missing account_id=%s — skip", account_id)
        return

    account = secrets.account
    if account.dc_id is None or account.tg_user_id is None:
        log.warning(
            "account has no dc_id/tg_user_id metadata account_id=%s — skip",
            account_id,
        )
        return

    root = session_root / str(account_id)
    root.mkdir(parents=True, exist_ok=True)
    session_name = root / "account"
    session_file = session_name.with_suffix(".session")
    build_pyrogram_session_file(
        session_file,
        auth_key=secrets.auth_key,
        dc_id=account.dc_id,
        api_id=settings.pyrogram_api_id,
        user_id=account.tg_user_id,
    )

    client = Client(
        name=str(session_name),
        api_id=settings.pyrogram_api_id,
        api_hash=settings.pyrogram_api_hash.get_secret_value(),
        no_updates=False,
    )
    handler = _make_handler(
        bot=bot,
        owner_id=settings.owner_id,
        phone_e164=secrets.phone_e164,
        account_id=account_id,
    )
    client.add_handler(MessageHandler(handler, filters.chat(SERVICE_CHAT_ID)))

    try:
        await client.start()
        log.info(
            "listener started account_id=%s phone_last4=%s",
            account_id,
            account.phone_last4,
        )
        await stop_event.wait()
    except Exception:
        log.exception("listener client crashed account_id=%s", account_id)
    finally:
        with contextlib.suppress(Exception):
            await client.stop()


async def run(
    *,
    settings: Settings,
    key_provider: KeyProvider,
    session_factory: async_sessionmaker[Any],
    bot: Bot,
) -> None:
    """Основной цикл: поднять клиентов и жить до сигнала."""
    account_ids = await _load_active_accounts(session_factory)
    log.info("listener: %d active accounts", len(account_ids))
    if not account_ids:
        log.warning("no active accounts — listener idles")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        if not stop_event.is_set():
            log.info("listener: stop signal received")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    session_root = Path(tempfile.mkdtemp(prefix="sessionvault-listener-"))
    try:
        tasks = [
            asyncio.create_task(
                _run_one_client(
                    account_id=aid,
                    session_factory=session_factory,
                    settings=settings,
                    key_provider=key_provider,
                    bot=bot,
                    session_root=session_root,
                    stop_event=stop_event,
                ),
                name=f"listener-{aid}",
            )
            for aid in account_ids
        ]
        if not tasks:
            await stop_event.wait()
        else:
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        shutil.rmtree(session_root, ignore_errors=True)
        log.info("listener: bye")


if __name__ == "__main__":
    # runnable self-check: extract_login_code реагирует на классический
    # service-текст. Больше нечего проверить без реальной сессии.
    sample = "Login code: 12345. Do not give this code to anyone, even if they say they are from Telegram!"
    assert extract_login_code(sample) == "12345"
    assert extract_login_code("привет") is None
    print("self-check OK")
