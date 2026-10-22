"""Auth middleware: пускает только Owner/Admin из личного чата.

Всё остальное:
- игнорируется (никакого ответа boт-ом — не даём подсказки о существовании);
- фиксируется в audit c `result='rejected'`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionvault.config import Settings
from sessionvault.domain.admins import AdminService, IdentityInfo
from sessionvault.domain.audit import AuditService
from sessionvault.storage.db.models import AuditResult

log = logging.getLogger(__name__)


class AuthMiddleware(BaseMiddleware):
    """Пропускает наверх только Owner/Admin из личного чата.

    В хендлер прокидывает две вещи через `data`:
    - `identity: IdentityInfo` — роль и tg_user_id;
    - `db_session_factory: async_sessionmaker` — фабрика сессий (хендлер сам
      открывает scope, если нужен доступ к БД).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = _extract_user(event)
        chat_type = _extract_chat_type(event)
        chat_id, message_id = _extract_ids(event)

        if user is None or chat_type != "private":
            await self._reject(event, actor_tg_id=user.id if user else None, reason="not_private_or_no_user",
                                chat_id=chat_id, message_id=message_id)
            return None

        async with self._session_factory() as session:
            admins = AdminService(session, self._settings)
            identity = await admins.identify(user.id)
            if identity is None or not identity.is_active:
                audit = AuditService(session)
                await audit.record_now(
                    action="auth.reject",
                    request_id=f"reject-{user.id}-{uuid.uuid4()}",
                    result=AuditResult.REJECTED,
                    actor_tg_id=user.id,
                    chat_id=chat_id,
                    message_id=message_id,
                    error_code="unknown_user",
                )
                await session.commit()
                log.info("auth reject: tg_user_id=%s chat=%s", user.id, chat_id)
                return None
            await session.commit()

        data["identity"] = identity
        data["db_session_factory"] = self._session_factory
        return await handler(event, data)

    async def _reject(
        self,
        event: TelegramObject,
        *,
        actor_tg_id: int | None,
        reason: str,
        chat_id: int | None,
        message_id: int | None,
    ) -> None:
        async with self._session_factory() as session:
            audit = AuditService(session)
            await audit.record_now(
                action="auth.reject",
                request_id=f"reject-{actor_tg_id or 'anon'}-{uuid.uuid4()}",
                result=AuditResult.REJECTED,
                actor_tg_id=actor_tg_id,
                chat_id=chat_id,
                message_id=message_id,
                error_code=reason,
            )
            await session.commit()
        log.info("auth reject: reason=%s actor=%s chat=%s", reason, actor_tg_id, chat_id)


def _extract_user(event: TelegramObject) -> Any | None:
    if isinstance(event, Message):
        return event.from_user
    if isinstance(event, CallbackQuery):
        return event.from_user
    return None


def _extract_chat_type(event: TelegramObject) -> str | None:
    if isinstance(event, Message) and event.chat:
        return event.chat.type
    if isinstance(event, CallbackQuery) and event.message and event.message.chat:
        return event.message.chat.type
    return None


def _extract_ids(event: TelegramObject) -> tuple[int | None, int | None]:
    if isinstance(event, Message):
        return event.chat.id if event.chat else None, event.message_id
    if isinstance(event, CallbackQuery) and event.message:
        return event.message.chat.id if event.message.chat else None, event.message.message_id
    return None, None


__all__ = ["AuthMiddleware", "IdentityInfo"]
