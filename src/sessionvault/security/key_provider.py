"""KeyProvider: единая точка доступа к DEK и ротации мастер-ключей.

Абстрагирует источник мастер-ключей (сейчас env, позже Vault/KMS). Домен
работает только через `KeyProvider.unwrap_or_rotate(...)` и не знает, откуда
берётся KEK.

Инвариант ротации:
- если DEK был обёрнут KEK v1, и Owner теперь положил MASTER_KEY_V2, то при
  первом чтении мы:
    1. распаковываем старым KEK v1;
    2. заново оборачиваем KEK v2 (`rewrap`);
    3. возвращаем DEK + свежий `WrappedDek(kek_version=2)`, вызывающий сохраняет.
- сам auth key не пересоздаётся: тот же самый DEK по-прежнему шифрует
  session_ciphertext, никакой перешифровки полей не требуется.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sessionvault.config import Settings
from sessionvault.security.envelope import (
    WrappedDek,
    generate_dek,
    load_wrapped_dek,
    unwrap_dek,
    wrap_dek,
)

log = logging.getLogger(__name__)


class MasterKeySource(Protocol):
    """Абстракция над источником мастер-ключей.

    Реализации: `EnvMasterKeySource` (сейчас), позже — VaultMasterKeySource,
    KMSMasterKeySource.
    """

    @property
    def current_version(self) -> int: ...
    def get(self, version: int) -> bytes: ...


class EnvMasterKeySource:
    """Читает `MASTER_KEY` / `MASTER_KEY_V2` из настроек.

    Правило: если задан `MASTER_KEY_V2`, он считается «текущим». Старый
    остаётся доступным для расшифровки, пока не завершится ленивая ротация.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._v1 = settings.master_key_bytes(version=1)
        self._v2 = (
            settings.master_key_bytes(version=2)
            if settings.master_key_v2 is not None
            else None
        )

    @property
    def current_version(self) -> int:
        return 2 if self._v2 is not None else 1

    def get(self, version: int) -> bytes:
        if version == 1:
            return self._v1
        if version == 2:
            if self._v2 is None:
                raise LookupError("MASTER_KEY_V2 не сконфигурирован")
            return self._v2
        raise LookupError(f"Неизвестная версия KEK: {version}")


@dataclass(frozen=True)
class UnwrappedDek:
    """Расшифрованный DEK + опциональный `rotated_wrapped` для сохранения.

    Если `rotated_wrapped is not None`, вызывающий обязан обновить БД: тот
    же самый DEK теперь обёрнут свежим KEK.
    """

    dek: bytes
    rotated_wrapped: WrappedDek | None


class KeyProvider:
    """Управляет DEK-ами: создаёт новые, распаковывает существующие,
    выполняет ленивую ротацию мастер-ключа."""

    def __init__(self, source: MasterKeySource) -> None:
        self._source = source

    def create_wrapped_dek(self, *, account_id: str) -> tuple[bytes, WrappedDek]:
        """Свежий DEK для нового аккаунта + wrapped-версию для сохранения."""
        version = self._source.current_version
        kek = self._source.get(version)
        dek = generate_dek()
        wrapped = wrap_dek(dek, kek=kek, kek_version=version, account_id=account_id)
        return dek, wrapped

    def unwrap_or_rotate(self, *, encoded_wrapped: bytes, account_id: str) -> UnwrappedDek:
        """Основной путь чтения.

        Если версия wrapped совпадает с текущей — распаковываем и возвращаем.
        Если ниже — распаковываем старым KEK, оборачиваем новым, возвращаем
        оба (вызывающий сохраняет rotated_wrapped).
        """
        wrapped = load_wrapped_dek(encoded_wrapped)
        stored_version = wrapped.kek_version
        current_version = self._source.current_version

        old_kek = self._source.get(stored_version)
        dek = unwrap_dek(wrapped, kek=old_kek, account_id=account_id)

        if stored_version == current_version:
            return UnwrappedDek(dek=dek, rotated_wrapped=None)

        # Ленивая ротация.
        new_kek = self._source.get(current_version)
        rotated = wrap_dek(
            dek,
            kek=new_kek,
            kek_version=current_version,
            account_id=account_id,
        )
        log.info(
            "DEK для account_id=%s перешифрован KEK v%d → v%d",
            account_id,
            stored_version,
            current_version,
        )
        return UnwrappedDek(dek=dek, rotated_wrapped=rotated)


def build_default(settings: Settings) -> KeyProvider:
    return KeyProvider(EnvMasterKeySource(settings))
