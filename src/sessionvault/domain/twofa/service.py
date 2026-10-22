"""Security ops: 2FA, recovery email, active sessions, phone change.

One service to keep the dependency wiring simple. Reuses AccountLock+audit
from the ProfileService pattern.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from sessionvault.config import Settings
from sessionvault.domain.accounts import AccountsService
from sessionvault.domain.accounts.service import _phone_hash
from sessionvault.domain.audit import AuditService
from sessionvault.mtproto.account_client import account_client
from sessionvault.mtproto.change_phone import (
    ChangePhoneCodeSent,
    confirm_change_phone,
    send_change_phone_code,
)
from sessionvault.mtproto.profile_ops import ProfileOperationError
from sessionvault.mtproto.sessions import SessionInfo, list_sessions, revoke_session
from sessionvault.mtproto.twofa_ops import (
    PasswordInfo,
    change_2fa,
    confirm_email_code,
    enable_2fa,
    get_password_info,
    is_email_unconfirmed_error,
    remove_2fa,
)
from sessionvault.security.envelope import encrypt_field
from sessionvault.security.key_provider import KeyProvider
from sessionvault.security.locks import AccountLock
from sessionvault.storage.db.models import (
    AuditResult,
    TelegramCredentials,
    UserRole,
)


@dataclass(frozen=True)
class TwoFactorSummary:
    has_password: bool
    has_recovery_email: bool
    pending_email_pattern: str
    hint: str


@dataclass(frozen=True)
class OpOutcome:
    ok: bool
    error_code: str | None = None
    pending_email: bool = False


class SecurityService:
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

    # ---------- Read-only ----------

    async def get_summary(self, *, account_id: uuid.UUID, tmp_dir: Path | None = None) -> TwoFactorSummary | None:
        """Returns None if account not found OR session unusable — caller renders
        'Аккаунт недоступен.' either way. Silent False fields would lie about 2FA
        state and lure the user into retrying against a broken session."""
        secrets = await self._resolve_secrets(account_id)
        if secrets is None:
            return None
        try:
            async with self._client(secrets, prefix="pwinfo", tmp_dir=tmp_dir) as client:
                info: PasswordInfo = await get_password_info(client)
        except ProfileOperationError:
            return None
        return TwoFactorSummary(
            has_password=info.has_password,
            has_recovery_email=info.has_recovery_email,
            pending_email_pattern=info.email_unconfirmed_pattern,
            hint=info.hint,
        )

    async def list_sessions(self, *, account_id: uuid.UUID, tmp_dir: Path | None = None) -> list[SessionInfo]:
        secrets = await self._resolve_secrets(account_id)
        if secrets is None:
            return []
        async with self._client(secrets, prefix="sessions", tmp_dir=tmp_dir) as client:
            return await list_sessions(client)

    # ---------- Write / dangerous ----------

    async def enable_2fa(
        self,
        *,
        account_id: uuid.UUID,
        password: str,
        hint: str,
        email: str | None,
        actor_tg_id: int,
        role: UserRole,
        idempotency_key: str,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> OpOutcome:
        return await self._run_write(
            account_id=account_id,
            action="security.twofa.enable",
            actor_tg_id=actor_tg_id,
            role=role,
            idempotency_key=idempotency_key,
            fields=["twofa"] + (["recovery_email"] if email else []),
            operation=lambda client: enable_2fa(client, password=password, hint=hint, email=email),
            on_success=lambda: self._store_password(account_id=account_id, password=password),
            treat_pending_email_ok=bool(email),
            set_flags={"has_2fa": True, "has_recovery_email": bool(email)},
            chat_id=chat_id,
            message_id=message_id,
        )

    async def change_2fa(
        self,
        *,
        account_id: uuid.UUID,
        current_password: str,
        new_password: str,
        new_hint: str,
        actor_tg_id: int,
        role: UserRole,
        idempotency_key: str,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> OpOutcome:
        return await self._run_write(
            account_id=account_id,
            action="security.twofa.change",
            actor_tg_id=actor_tg_id,
            role=role,
            idempotency_key=idempotency_key,
            fields=["twofa"],
            operation=lambda client: change_2fa(
                client,
                current_password=current_password,
                new_password=new_password,
                new_hint=new_hint,
            ),
            on_success=lambda: self._store_password(account_id=account_id, password=new_password),
            chat_id=chat_id,
            message_id=message_id,
        )

    async def remove_2fa(
        self,
        *,
        account_id: uuid.UUID,
        current_password: str,
        actor_tg_id: int,
        role: UserRole,
        idempotency_key: str,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> OpOutcome:
        return await self._run_write(
            account_id=account_id,
            action="security.twofa.remove",
            actor_tg_id=actor_tg_id,
            role=role,
            idempotency_key=idempotency_key,
            fields=["twofa"],
            operation=lambda client: remove_2fa(client, current_password=current_password),
            on_success=lambda: self._clear_password(account_id=account_id),
            set_flags={"has_2fa": False, "has_recovery_email": False},
            chat_id=chat_id,
            message_id=message_id,
        )

    async def confirm_recovery_email(
        self,
        *,
        account_id: uuid.UUID,
        code: str,
        actor_tg_id: int,
        role: UserRole,
        idempotency_key: str,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> OpOutcome:
        return await self._run_write(
            account_id=account_id,
            action="security.recovery_email.confirm",
            actor_tg_id=actor_tg_id,
            role=role,
            idempotency_key=idempotency_key,
            fields=["recovery_email"],
            operation=lambda client: confirm_email_code(client, code=code),
            set_flags={"has_recovery_email": True},
            chat_id=chat_id,
            message_id=message_id,
        )

    async def revoke_session(
        self,
        *,
        account_id: uuid.UUID,
        session_id: int,
        actor_tg_id: int,
        role: UserRole,
        idempotency_key: str,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> OpOutcome:
        return await self._run_write(
            account_id=account_id,
            action="security.session.revoke",
            actor_tg_id=actor_tg_id,
            role=role,
            idempotency_key=idempotency_key,
            fields=["session"],
            operation=lambda client: revoke_session(client, session_id=session_id),
            chat_id=chat_id,
            message_id=message_id,
        )

    async def send_phone_change_code(
        self,
        *,
        account_id: uuid.UUID,
        new_phone_e164: str,
        actor_tg_id: int,
        role: UserRole,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> tuple[OpOutcome, ChangePhoneCodeSent | None]:
        """No AccountLock — just requests a code; no state mutation on Telegram."""
        secrets = await self._resolve_secrets(account_id)
        if secrets is None:
            await self._audit(
                action="security.phone_change.request",
                account_id=account_id,
                actor_tg_id=actor_tg_id,
                role=role,
                result=AuditResult.REJECTED,
                error_code="ACCOUNT_NOT_FOUND",
                chat_id=chat_id,
                message_id=message_id,
            )
            return OpOutcome(False, "ACCOUNT_NOT_FOUND"), None
        try:
            async with self._client(secrets, prefix="phone-code") as client:
                sent = await send_change_phone_code(client, new_phone_e164=new_phone_e164)
        except ProfileOperationError as exc:
            await self._audit(
                action="security.phone_change.request",
                account_id=account_id,
                actor_tg_id=actor_tg_id,
                role=role,
                result=AuditResult.FAILED,
                error_code=exc.code,
                chat_id=chat_id,
                message_id=message_id,
            )
            return OpOutcome(False, exc.code), None
        await self._audit(
            action="security.phone_change.request",
            account_id=account_id,
            actor_tg_id=actor_tg_id,
            role=role,
            result=AuditResult.OK,
            chat_id=chat_id,
            message_id=message_id,
        )
        return OpOutcome(True), sent

    async def confirm_phone_change(
        self,
        *,
        account_id: uuid.UUID,
        new_phone_e164: str,
        phone_code_hash: str,
        phone_code: str,
        actor_tg_id: int,
        role: UserRole,
        idempotency_key: str,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> OpOutcome:
        outcome = await self._run_write(
            account_id=account_id,
            action="security.phone_change.confirm",
            actor_tg_id=actor_tg_id,
            role=role,
            idempotency_key=idempotency_key,
            fields=["phone"],
            operation=lambda client: confirm_change_phone(
                client,
                new_phone_e164=new_phone_e164,
                phone_code_hash=phone_code_hash,
                phone_code=phone_code,
            ),
            chat_id=chat_id,
            message_id=message_id,
        )
        if outcome.ok:
            await self._replace_stored_phone(account_id=account_id, new_phone=new_phone_e164)
        return outcome

    # ---------- internal helpers ----------

    def _client(self, secrets, *, prefix: str, tmp_dir: Path | None = None):
        return account_client(
            secrets=secrets,
            api_id=self._settings.pyrogram_api_id,
            api_hash=self._settings.pyrogram_api_hash.get_secret_value(),
            prefix=prefix,
            tmp_dir=tmp_dir,
        )

    async def _resolve_secrets(self, account_id: uuid.UUID):
        accounts = AccountsService(self._session, self._key_provider)
        return await accounts.get_secrets(account_id)

    async def _run_write(
        self,
        *,
        account_id: uuid.UUID,
        action: str,
        actor_tg_id: int,
        role: UserRole,
        idempotency_key: str,
        fields: list[str],
        operation,
        on_success=None,
        set_flags: dict[str, bool] | None = None,
        treat_pending_email_ok: bool = False,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> OpOutcome:
        secrets = await self._resolve_secrets(account_id)
        if secrets is None:
            await self._audit(
                action=action,
                account_id=account_id,
                actor_tg_id=actor_tg_id,
                role=role,
                result=AuditResult.REJECTED,
                error_code="ACCOUNT_NOT_FOUND",
                chat_id=chat_id,
                message_id=message_id,
            )
            return OpOutcome(False, "ACCOUNT_NOT_FOUND")

        async with AccountLock(
            self._redis,
            account_id=str(account_id),
            holder=f"{action}:{idempotency_key}",
        ) as lock:
            if not lock.acquired:
                await self._audit(
                    action=action,
                    account_id=account_id,
                    actor_tg_id=actor_tg_id,
                    role=role,
                    result=AuditResult.REJECTED,
                    error_code="ACCOUNT_LOCKED",
                    chat_id=chat_id,
                    message_id=message_id,
                )
                return OpOutcome(False, "ACCOUNT_LOCKED")

            pending_email = False
            try:
                async with self._client(secrets, prefix=action.split(".")[-1]) as client:
                    # No post-mutation sanity read: an RPCError after a successful
                    # write would invert the outcome and desync DB from Telegram.
                    await operation(client)
            except ProfileOperationError as exc:
                if treat_pending_email_ok and is_email_unconfirmed_error(exc):
                    pending_email = True
                else:
                    await self._audit(
                        action=action,
                        account_id=account_id,
                        actor_tg_id=actor_tg_id,
                        role=role,
                        result=AuditResult.FAILED,
                        error_code=exc.code,
                        chat_id=chat_id,
                        message_id=message_id,
                    )
                    return OpOutcome(False, exc.code)

            if set_flags:
                account = secrets.account
                for key, value in set_flags.items():
                    # Don't flip has_recovery_email=True while the email is still
                    # unconfirmed — user hasn't entered the code yet.
                    if pending_email and key == "has_recovery_email":
                        continue
                    setattr(account, key, value)
                account.last_profile_update_at = datetime.now(UTC)
                await self._session.flush()
            if on_success is not None:
                await on_success()

            await self._audit(
                action=action,
                account_id=account_id,
                actor_tg_id=actor_tg_id,
                role=role,
                result=AuditResult.OK,
                changed_fields=fields,
                error_code="EMAIL_UNCONFIRMED" if pending_email else None,
                chat_id=chat_id,
                message_id=message_id,
            )
            return OpOutcome(True, pending_email=pending_email)

    async def _store_password(self, *, account_id: uuid.UUID, password: str) -> None:
        """Store 2FA password only if policy allows. Default: don't store."""
        if not getattr(self._settings, "store_twofa_passwords", False):
            return
        secrets = await self._resolve_secrets(account_id)
        creds = await self._session.get(TelegramCredentials, account_id)
        if secrets is None or creds is None:
            return
        creds.twofa_ciphertext = encrypt_field(
            password.encode(),
            dek=secrets.dek,
            account_id=str(account_id),
            field="twofa",
        )
        await self._session.flush()

    async def _clear_password(self, *, account_id: uuid.UUID) -> None:
        creds = await self._session.get(TelegramCredentials, account_id)
        if creds is None:
            return
        creds.twofa_ciphertext = None
        await self._session.flush()

    async def _replace_stored_phone(self, *, account_id: uuid.UUID, new_phone: str) -> None:
        secrets = await self._resolve_secrets(account_id)
        creds = await self._session.get(TelegramCredentials, account_id)
        if secrets is None or creds is None:
            return
        creds.phone_ciphertext = encrypt_field(
            new_phone.encode(),
            dek=secrets.dek,
            account_id=str(account_id),
            field="phone",
        )
        secrets.account.phone_e164_hash = _phone_hash(new_phone)
        secrets.account.phone_last4 = new_phone[-4:]
        await self._session.flush()

    async def _audit(
        self,
        *,
        action: str,
        account_id: uuid.UUID,
        actor_tg_id: int,
        role: UserRole,
        result: AuditResult,
        changed_fields: list[str] | None = None,
        error_code: str | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> None:
        await AuditService(self._session).record_now(
            action=action,
            request_id=f"{action}-{account_id}-{uuid.uuid4()}",
            result=result,
            actor_tg_id=actor_tg_id,
            role=role,
            account_id=account_id,
            changed_fields=changed_fields or [],
            error_code=error_code,
            chat_id=chat_id,
            message_id=message_id,
        )
