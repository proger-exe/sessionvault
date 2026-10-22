"""Profile operations and health checks."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from sessionvault.config import Settings
from sessionvault.domain.accounts import AccountsService
from sessionvault.domain.audit import AuditService
from sessionvault.mtproto.account_client import account_client
from sessionvault.mtproto.health import get_self
from sessionvault.mtproto.profile_ops import (
    UNSUPPORTED_MARKERS,
    ProfileOperationError,
    apply_profile_operation,
)
from sessionvault.security.key_provider import KeyProvider
from sessionvault.security.locks import AccountLock
from sessionvault.storage.db.models import (
    AuditResult,
    ProfileOperation,
    ProfileOperationState,
    RiskState,
    UserRole,
)


def format_op_result(op: ProfileOperation) -> str:
    """User-facing one-liner about a finished profile operation."""
    if op.state == ProfileOperationState.OK:
        return "Готово."
    code = op.error_code or "UNKNOWN"
    if any(marker in code for marker in UNSUPPORTED_MARKERS) or code == "UNSUPPORTED_OPERATION":
        return f"Недоступно (<code>unsupported: {code}</code>)."
    if code == "ACCOUNT_LOCKED":
        return "Аккаунт занят другой операцией, попробуй позже."
    if code == "ACCOUNT_NOT_FOUND":
        return "Аккаунт не найден."
    return f"Не удалось: <code>{code}</code>."


class ProfileService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        key_provider: KeyProvider,
        redis: Redis[bytes],
    ) -> None:
        self._session = session
        self._settings = settings
        self._key_provider = key_provider
        self._redis = redis

    async def create_operation(
        self,
        *,
        account_id: uuid.UUID,
        operation_type: str,
        payload: dict[str, Any],
        operator_tg_id: int,
        idempotency_key: str,
    ) -> ProfileOperation:
        op = ProfileOperation(
            account_id=account_id,
            operation_type=operation_type,
            payload=payload,
            operator_tg_id=operator_tg_id,
            idempotency_key=idempotency_key,
        )
        self._session.add(op)
        await self._session.flush()
        return op

    async def list_photos(
        self,
        *,
        account_id: uuid.UUID,
        limit: int = 20,
        tmp_dir: Path | None = None,
    ) -> list[dict[str, Any]]:
        """Read own profile photos. Read-only, no lock. Returns [{index, date}]."""
        accounts = AccountsService(self._session, self._key_provider)
        secrets = await accounts.get_secrets(account_id)
        if secrets is None:
            return []
        async with account_client(
            secrets=secrets,
            api_id=self._settings.pyrogram_api_id,
            api_hash=self._settings.pyrogram_api_hash.get_secret_value(),
            prefix="photos",
            tmp_dir=tmp_dir,
        ) as client:
            photo_iter = client.get_chat_photos("me", limit=limit)
            photos = [p async for p in photo_iter] if photo_iter is not None else []
        return [
            {"index": i, "date": getattr(p, "date", None)}
            for i, p in enumerate(photos)
        ]

    async def check_health(
        self,
        *,
        account_id: uuid.UUID,
        actor_tg_id: int,
        role: UserRole,
        tmp_dir: Path | None = None,
    ) -> bool:
        accounts = AccountsService(self._session, self._key_provider)
        secrets = await accounts.get_secrets(account_id)
        if secrets is None:
            return False

        account = secrets.account
        try:
            async with account_client(
                secrets=secrets,
                api_id=self._settings.pyrogram_api_id,
                api_hash=self._settings.pyrogram_api_hash.get_secret_value(),
                prefix="health",
                tmp_dir=tmp_dir,
            ) as client:
                self_profile = await get_self(client)
        except Exception as exc:
            account.risk_state = RiskState.INVALID_SESSION
            await AuditService(self._session).record_now(
                action="account.health",
                request_id=f"health-{account_id}-{uuid.uuid4()}",
                result=AuditResult.FAILED,
                actor_tg_id=actor_tg_id,
                role=role,
                account_id=account_id,
                error_code=type(exc).__name__,
            )
            await self._session.flush()
            return False

        account.tg_user_id = self_profile.tg_user_id
        account.first_name = self_profile.first_name
        account.last_name = self_profile.last_name
        account.username = self_profile.username
        account.dc_id = self_profile.dc_id or account.dc_id
        account.risk_state = RiskState.NORMAL
        account.last_health_check_at = datetime.now(UTC)
        await AuditService(self._session).record_now(
            action="account.health",
            request_id=f"health-{account_id}-{uuid.uuid4()}",
            result=AuditResult.OK,
            actor_tg_id=actor_tg_id,
            role=role,
            account_id=account_id,
        )
        await self._session.flush()
        return True

    async def apply_operation(
        self,
        *,
        operation_id: uuid.UUID,
        role: UserRole,
        tmp_dir: Path | None = None,
    ) -> ProfileOperation:
        op = await self._session.get(ProfileOperation, operation_id)
        if op is None:
            raise LookupError(f"profile operation not found: {operation_id}")

        accounts = AccountsService(self._session, self._key_provider)
        secrets = await accounts.get_secrets(op.account_id)
        if secrets is None:
            op.state = ProfileOperationState.FAILED
            op.error_code = "ACCOUNT_NOT_FOUND"
            await self._session.flush()
            return op

        async with AccountLock(
            self._redis,
            account_id=str(op.account_id),
            holder=f"profile:{operation_id}",
        ) as lock:
            if not lock.acquired:
                op.state = ProfileOperationState.FAILED
                op.error_code = "ACCOUNT_LOCKED"
                await self._session.flush()
                return op

            op.state = ProfileOperationState.RUNNING
            op.started_at = datetime.now(UTC)
            await self._session.flush()
            try:
                async with account_client(
                    secrets=secrets,
                    api_id=self._settings.pyrogram_api_id,
                    api_hash=self._settings.pyrogram_api_hash.get_secret_value(),
                    prefix="profile",
                    tmp_dir=tmp_dir,
                ) as client:
                    result = await apply_profile_operation(
                        client,
                        operation_type=op.operation_type,
                        payload=op.payload,
                    )
            except ProfileOperationError as exc:
                op.state = ProfileOperationState.FAILED
                op.error_code = exc.code
                op.finished_at = datetime.now(UTC)
                await AuditService(self._session).record_now(
                    action=op.operation_type,
                    request_id=f"profile-{operation_id}-{uuid.uuid4()}",
                    result=AuditResult.FAILED,
                    actor_tg_id=op.operator_tg_id,
                    role=role,
                    account_id=op.account_id,
                    error_code=exc.code,
                )
                await self._session.flush()
                return op

            account = secrets.account
            account.tg_user_id = result.self_profile.tg_user_id
            account.first_name = result.self_profile.first_name
            account.last_name = result.self_profile.last_name
            account.username = result.self_profile.username
            account.dc_id = result.self_profile.dc_id or account.dc_id
            account.last_profile_update_at = datetime.now(UTC)
            op.state = ProfileOperationState.OK
            op.finished_at = datetime.now(UTC)
            await AuditService(self._session).record_now(
                action=op.operation_type,
                request_id=f"profile-{operation_id}-{uuid.uuid4()}",
                result=AuditResult.OK,
                actor_tg_id=op.operator_tg_id,
                role=role,
                account_id=op.account_id,
                changed_fields=result.changed_fields,
            )
            await self._session.flush()
            return op
