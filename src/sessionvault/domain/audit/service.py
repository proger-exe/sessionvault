"""Audit-сервис.

Основные принципы (см. ТЗ п.9, план п.9 Этапа 1):
- каждая запись идемпотентна по `request_id`;
- audit пишется в ту же транзакцию, что и доменная операция — снаружи
  вызывающий держит сессию и делает `commit`;
- секреты (кода/пароля/auth key) в audit не попадают, что гарантируется
  контрактом сервиса: он не принимает byte-контент, только имена изменённых
  полей + нормализованный error_code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sessionvault.storage.db.models import AuditEvent, AuditResult, UserRole


@dataclass(frozen=True)
class AuditEntry:
    action: str
    request_id: str
    result: AuditResult
    started_at: datetime
    actor_tg_id: int | None = None
    role: UserRole | None = None
    account_id: uuid.UUID | None = None
    account_tg_user_id: int | None = None
    chat_id: int | None = None
    message_id: int | None = None
    error_code: str | None = None
    changed_fields: list[str] = field(default_factory=list)
    finished_at: datetime | None = None


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, entry: AuditEntry) -> AuditEvent:
        """Записать событие. Идемпотентно по `request_id`.

        Если такое событие уже есть — возвращаем существующее без ошибки.
        """
        stmt = (
            insert(AuditEvent)
            .values(
                id=uuid.uuid4(),
                actor_tg_id=entry.actor_tg_id,
                role=entry.role,
                action=entry.action,
                account_id=entry.account_id,
                account_tg_user_id=entry.account_tg_user_id,
                chat_id=entry.chat_id,
                message_id=entry.message_id,
                request_id=entry.request_id,
                result=entry.result,
                error_code=entry.error_code,
                changed_fields=entry.changed_fields,
                started_at=entry.started_at,
                finished_at=entry.finished_at,
            )
            .on_conflict_do_nothing(index_elements=["request_id"])
        )
        await self._session.execute(stmt)

        existing = await self._session.execute(
            select(AuditEvent).where(AuditEvent.request_id == entry.request_id)
        )
        return existing.scalar_one()

    async def record_now(
        self,
        *,
        action: str,
        request_id: str,
        result: AuditResult,
        actor_tg_id: int | None = None,
        role: UserRole | None = None,
        account_id: uuid.UUID | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
        error_code: str | None = None,
        changed_fields: list[str] | None = None,
    ) -> AuditEvent:
        """Удобный шорткат: started_at = finished_at = now(UTC)."""
        now = datetime.now(UTC)
        return await self.record(
            AuditEntry(
                action=action,
                request_id=request_id,
                result=result,
                actor_tg_id=actor_tg_id,
                role=role,
                account_id=account_id,
                chat_id=chat_id,
                message_id=message_id,
                error_code=error_code,
                changed_fields=changed_fields or [],
                started_at=now,
                finished_at=now,
            )
        )
