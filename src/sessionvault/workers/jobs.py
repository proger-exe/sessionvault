"""arq job functions."""

from __future__ import annotations

import uuid
from typing import Any

from redis.asyncio import Redis

from sessionvault.config import Settings
from sessionvault.domain.exports import ExportService
from sessionvault.domain.profile import ProfileService
from sessionvault.storage.db.models import UserRole
from sessionvault.storage.db.session import get_session_factory


async def check_account_health(ctx: dict[str, Any], account_id: str, actor_tg_id: int, role: str) -> bool:
    async with get_session_factory()() as session:
        service = ProfileService(
            session,
            settings=ctx["settings"],
            key_provider=ctx["key_provider"],
            redis=ctx["redis"],
        )
        ok = await service.check_health(
            account_id=uuid.UUID(account_id),
            actor_tg_id=actor_tg_id,
            role=UserRole(role),
        )
        await session.commit()
        return ok


async def apply_profile_operation(ctx: dict[str, Any], operation_id: str, role: str) -> str:
    async with get_session_factory()() as session:
        service = ProfileService(
            session,
            settings=ctx["settings"],
            key_provider=ctx["key_provider"],
            redis=ctx["redis"],
        )
        op = await service.apply_operation(operation_id=uuid.UUID(operation_id), role=UserRole(role))
        await session.commit()
        return op.state.value


async def build_pyrogram_export(
    ctx: dict[str, Any],
    export_id: str,
    actor_tg_id: int,
    role: str,
) -> str:
    async with get_session_factory()() as session:
        service = ExportService(
            session,
            settings=ctx["settings"],
            key_provider=ctx["key_provider"],
            redis=ctx["redis"],
        )
        artifact = await service.build_pyrogram_export(
            export_id=uuid.UUID(export_id),
            actor_tg_id=actor_tg_id,
            role=UserRole(role),
        )
        await session.commit()
        return str(artifact.path)


async def startup(ctx: dict[str, Any]) -> None:
    from sessionvault.config import get_settings
    from sessionvault.security.key_provider import build_default

    settings: Settings = get_settings()
    ctx["settings"] = settings
    ctx["key_provider"] = build_default(settings)
    ctx["redis"] = Redis.from_url(settings.redis_url, decode_responses=False)


async def shutdown(ctx: dict[str, Any]) -> None:
    redis = ctx.get("redis")
    if redis is not None:
        await redis.close()
