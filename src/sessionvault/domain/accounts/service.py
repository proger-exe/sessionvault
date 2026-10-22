"""AccountsService — сохранение аккаунта после успешного login.

- шифрует auth_key/phone через envelope encryption (DEK-per-account);
- пишет несекретные метаданные в `telegram_accounts`;
- пишет зашифрованные секреты в `telegram_credentials`.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionvault.mtproto.login import ExtractedSession
from sessionvault.security.envelope import decrypt_field, encrypt_field
from sessionvault.security.key_provider import KeyProvider
from sessionvault.storage.db.models import (
    AccountStatus,
    RiskState,
    TelegramAccount,
    TelegramCredentials,
)


def _phone_hash(phone_e164: str) -> str:
    return hashlib.sha256(phone_e164.encode()).hexdigest()


@dataclass
class SavedAccount:
    account_id: uuid.UUID
    tg_user_id: int


@dataclass(frozen=True)
class AccountSecrets:
    account: TelegramAccount
    auth_key: bytes
    phone_e164: str
    dek: bytes


class AccountsService:
    def __init__(self, session: AsyncSession, key_provider: KeyProvider | None) -> None:
        self._session = session
        self._key_provider = key_provider

    async def get_by_phone(self, phone_e164: str) -> TelegramAccount | None:
        stmt = select(TelegramAccount).where(TelegramAccount.phone_e164_hash == _phone_hash(phone_e164))
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, account_id: uuid.UUID) -> TelegramAccount | None:
        return await self._session.get(TelegramAccount, account_id)

    async def get_secrets(self, account_id: uuid.UUID) -> AccountSecrets | None:
        """Return decrypted account secrets, rotating wrapped DEK when needed."""
        if self._key_provider is None:
            raise RuntimeError("KeyProvider is required to read account secrets")

        account = await self.get_by_id(account_id)
        if account is None:
            return None
        creds = await self._session.get(TelegramCredentials, account_id)
        if creds is None:
            return None

        account_id_str = str(account.id)
        unwrapped = self._key_provider.unwrap_or_rotate(
            encoded_wrapped=creds.dek_wrapped,
            account_id=account_id_str,
        )
        if unwrapped.rotated_wrapped is not None:
            creds.dek_wrapped = unwrapped.rotated_wrapped.encoded
            await self._session.flush()

        auth_key = decrypt_field(
            creds.session_ciphertext,
            dek=unwrapped.dek,
            account_id=account_id_str,
            field="session",
        )
        phone = decrypt_field(
            creds.phone_ciphertext,
            dek=unwrapped.dek,
            account_id=account_id_str,
            field="phone",
        ).decode()
        return AccountSecrets(account=account, auth_key=auth_key, phone_e164=phone, dek=unwrapped.dek)

    async def list_page(
        self, *, offset: int, limit: int
    ) -> tuple[list[TelegramAccount], int]:
        """Пагинированный список + общее число записей."""
        rows_stmt = (
            select(TelegramAccount)
            .order_by(TelegramAccount.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        count_stmt = select(func.count()).select_from(TelegramAccount)
        rows = list((await self._session.execute(rows_stmt)).scalars())
        total = int((await self._session.execute(count_stmt)).scalar_one())
        return rows, total

    async def summary_counts(self) -> dict[str, int]:
        stmt = select(
            TelegramAccount.status, func.count()
        ).group_by(TelegramAccount.status)
        rows = (await self._session.execute(stmt)).all()
        return {str(status.value): int(count) for status, count in rows}

    async def save_after_login(
        self,
        *,
        phone_e164: str,
        session_data: ExtractedSession,
        operator_tg_id: int,
    ) -> SavedAccount:
        """Идемпотентно по phone_e164_hash: повторный вызов обновит запись,
        не создаёт дубль. Auth key всегда перезаписывается свежим."""
        if self._key_provider is None:
            raise RuntimeError("KeyProvider is required to save account secrets")

        phone_hash = _phone_hash(phone_e164)
        phone_last4 = phone_e164[-4:]

        existing = await self.get_by_phone(phone_e164)
        if existing is None:
            account = TelegramAccount(
                id=uuid.uuid4(),
                tg_user_id=session_data.user_id,
                phone_e164_hash=phone_hash,
                phone_last4=phone_last4,
                first_name=session_data.first_name,
                last_name=session_data.last_name,
                username=session_data.username,
                dc_id=session_data.dc_id,
                status=AccountStatus.ACTIVE,
                risk_state=RiskState.NORMAL,
                has_2fa=session_data.has_2fa,
                owner_operator_tg_id=operator_tg_id,
            )
            self._session.add(account)
            await self._session.flush()
        else:
            account = existing
            account.tg_user_id = session_data.user_id
            account.first_name = session_data.first_name
            account.last_name = session_data.last_name
            account.username = session_data.username
            account.dc_id = session_data.dc_id
            account.status = AccountStatus.ACTIVE
            account.risk_state = RiskState.NORMAL
            account.has_2fa = session_data.has_2fa
            account.owner_operator_tg_id = operator_tg_id
            await self._session.flush()

        # DEK и шифрование секретов.
        account_id_str = str(account.id)
        dek, wrapped = self._key_provider.create_wrapped_dek(account_id=account_id_str)
        session_ct = encrypt_field(
            session_data.auth_key, dek=dek, account_id=account_id_str, field="session"
        )
        phone_ct = encrypt_field(
            phone_e164.encode(), dek=dek, account_id=account_id_str, field="phone"
        )

        creds = TelegramCredentials(
            account_id=account.id,
            dek_wrapped=wrapped.encoded,
            session_ciphertext=session_ct,
            phone_ciphertext=phone_ct,
        )
        # ON CONFLICT-эквивалент — merge (у account_id PK).
        await self._session.merge(creds)
        await self._session.flush()

        return SavedAccount(account_id=account.id, tg_user_id=session_data.user_id)
