"""KeyProvider: создание DEK, чтение, ленивая ротация мастер-ключа.

Проверяем главный инвариант плана Этапа 1: ротация мастер-ключа НЕ требует
повторной авторизации аккаунта. Auth key остаётся тем же самым — меняется
только обёртка DEK.
"""

from __future__ import annotations

import os

import pytest

from sessionvault.security.envelope import decrypt_field, encrypt_field
from sessionvault.security.key_provider import KeyProvider, MasterKeySource

ACCOUNT = "00000000-0000-4000-8000-000000000042"


class InMemoryMasterKeySource:
    """Тестовый источник — держим версии в dict."""

    def __init__(self, keys: dict[int, bytes], *, current: int) -> None:
        self._keys = keys
        self._current = current

    @property
    def current_version(self) -> int:
        return self._current

    def get(self, version: int) -> bytes:
        return self._keys[version]

    def promote(self, new_version: int, new_kek: bytes) -> None:
        self._keys[new_version] = new_kek
        self._current = new_version


# Убедимся, что мок соответствует Protocol-у.
_: type[MasterKeySource] = InMemoryMasterKeySource  # type: ignore[assignment,misc]


def _new_kek() -> bytes:
    return os.urandom(32)


def test_create_wrapped_dek_uses_current_version() -> None:
    src = InMemoryMasterKeySource({1: _new_kek()}, current=1)
    kp = KeyProvider(src)
    dek, wrapped = kp.create_wrapped_dek(account_id=ACCOUNT)
    assert wrapped.kek_version == 1
    # DEK можно сразу использовать для шифрования полей.
    ct = encrypt_field(b"auth_key_bytes", dek=dek, account_id=ACCOUNT, field="session")
    assert b"auth_key_bytes" not in ct


def test_unwrap_same_version_no_rotation() -> None:
    src = InMemoryMasterKeySource({1: _new_kek()}, current=1)
    kp = KeyProvider(src)
    dek, wrapped = kp.create_wrapped_dek(account_id=ACCOUNT)

    result = kp.unwrap_or_rotate(encoded_wrapped=wrapped.encoded, account_id=ACCOUNT)
    assert result.dek == dek
    assert result.rotated_wrapped is None


def test_rotation_preserves_dek_and_upgrades_wrapper() -> None:
    """Ключевой тест: после ротации мастер-ключа тот же DEK шифрует те же поля,
    и session_ciphertext, зашифрованный до ротации, продолжает расшифровываться.
    """
    src = InMemoryMasterKeySource({1: _new_kek()}, current=1)
    kp = KeyProvider(src)

    original_dek, wrapped_v1 = kp.create_wrapped_dek(account_id=ACCOUNT)

    session_plain = b"telegram_auth_key_256_bytes_mock"
    session_ct = encrypt_field(
        session_plain, dek=original_dek, account_id=ACCOUNT, field="session"
    )

    # Owner положил новый мастер-ключ.
    src.promote(2, _new_kek())

    result = kp.unwrap_or_rotate(
        encoded_wrapped=wrapped_v1.encoded, account_id=ACCOUNT
    )

    # DEK не поменялся → auth key остаётся расшифровываемым.
    assert result.dek == original_dek
    assert (
        decrypt_field(session_ct, dek=result.dek, account_id=ACCOUNT, field="session")
        == session_plain
    )

    # Wrapped-версия обновилась до v2 — вызывающий сохранит в БД.
    assert result.rotated_wrapped is not None
    assert result.rotated_wrapped.kek_version == 2

    # Ещё раз распакуем — уже без ротации.
    result2 = kp.unwrap_or_rotate(
        encoded_wrapped=result.rotated_wrapped.encoded, account_id=ACCOUNT
    )
    assert result2.dek == original_dek
    assert result2.rotated_wrapped is None


def test_missing_master_key_v2_lookup_error() -> None:
    src = InMemoryMasterKeySource({1: _new_kek()}, current=1)
    with pytest.raises(KeyError):
        src.get(2)


def test_env_master_key_source_reads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """EnvMasterKeySource читает Settings.master_key_bytes."""
    import base64

    from sessionvault.config import Settings
    from sessionvault.security.key_provider import EnvMasterKeySource

    key_b64 = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("BOT_TOKEN", "x")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.setenv("PYROGRAM_API_ID", "1")
    monkeypatch.setenv("PYROGRAM_API_HASH", "y")
    monkeypatch.setenv("MASTER_KEY", key_b64)

    settings = Settings()  # type: ignore[call-arg]
    src = EnvMasterKeySource(settings)
    assert src.current_version == 1
    assert len(src.get(1)) == 32
    with pytest.raises(LookupError):
        src.get(2)


def test_env_master_key_source_promotes_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    from sessionvault.config import Settings
    from sessionvault.security.key_provider import EnvMasterKeySource

    v1 = base64.b64encode(os.urandom(32)).decode()
    v2 = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("BOT_TOKEN", "x")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.setenv("PYROGRAM_API_ID", "1")
    monkeypatch.setenv("PYROGRAM_API_HASH", "y")
    monkeypatch.setenv("MASTER_KEY", v1)
    monkeypatch.setenv("MASTER_KEY_V2", v2)

    settings = Settings()  # type: ignore[call-arg]
    src = EnvMasterKeySource(settings)
    assert src.current_version == 2
    assert src.get(1) != src.get(2)
