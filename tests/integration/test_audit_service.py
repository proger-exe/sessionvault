"""Аудит: идемпотентность по request_id + append-only на уровне БД."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sessionvault.domain.audit import AuditService
from sessionvault.storage.db.models import AuditResult, UserRole


async def test_record_now_persists(db_session: AsyncSession) -> None:
    service = AuditService(db_session)
    entry = await service.record_now(
        action="test.append_only",
        request_id=f"req-{uuid.uuid4()}",
        result=AuditResult.OK,
        actor_tg_id=7910099701,
        role=UserRole.OWNER,
        changed_fields=["first_name"],
    )
    assert entry.id is not None
    assert entry.result == AuditResult.OK
    await db_session.commit()


async def test_record_now_idempotent_by_request_id(db_session: AsyncSession) -> None:
    service = AuditService(db_session)
    req_id = f"req-{uuid.uuid4()}"
    a = await service.record_now(action="a", request_id=req_id, result=AuditResult.OK)
    b = await service.record_now(action="different-action", request_id=req_id, result=AuditResult.FAILED)
    await db_session.commit()

    # Второй вызов возвращает существующую запись, не создаёт новую и не
    # перезаписывает.
    assert a.id == b.id
    assert b.action == "a"
    assert b.result == AuditResult.OK


async def test_update_blocked_by_trigger(db_session: AsyncSession) -> None:
    service = AuditService(db_session)
    entry = await service.record_now(
        action="test.no_update",
        request_id=f"req-{uuid.uuid4()}",
        result=AuditResult.OK,
    )
    await db_session.commit()

    with pytest.raises(Exception) as excinfo:
        await db_session.execute(
            text("UPDATE audit_events SET action = 'hacked' WHERE id = :id"),
            {"id": entry.id},
        )
        await db_session.commit()
    assert "append-only" in str(excinfo.value).lower()


async def test_delete_blocked_by_trigger(db_session: AsyncSession) -> None:
    service = AuditService(db_session)
    entry = await service.record_now(
        action="test.no_delete",
        request_id=f"req-{uuid.uuid4()}",
        result=AuditResult.OK,
    )
    await db_session.commit()

    with pytest.raises(Exception) as excinfo:
        await db_session.execute(
            text("DELETE FROM audit_events WHERE id = :id"),
            {"id": entry.id},
        )
        await db_session.commit()
    assert "append-only" in str(excinfo.value).lower()


async def test_truncate_blocked_by_trigger(db_session: AsyncSession) -> None:
    """TRUNCATE защищён отдельным statement-level триггером — см. миграцию 002."""
    with pytest.raises(Exception) as excinfo:
        await db_session.execute(text("TRUNCATE audit_events"))
        await db_session.commit()
    assert "append-only" in str(excinfo.value).lower()
