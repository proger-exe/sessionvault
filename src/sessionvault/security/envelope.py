"""Envelope encryption для секретов Telegram-аккаунтов.

Схема:
- KEK (Key Encryption Key) = мастер-ключ из `Settings.master_key`. Живёт в `.env`.
- DEK (Data Encryption Key) = 32-байтный ключ на *аккаунт*. Генерируется один
  раз при создании записи; зашифрован KEK-ом и хранится в
  `telegram_credentials.dek_wrapped`.
- Секреты (session, twofa, recovery email, phone) шифруются DEK-ом.
- AAD для каждого поля = `f"{account_id}:{field_name}"` — гарантирует, что
  ciphertext одного поля нельзя переклеить в другое поле или другой аккаунт.

Ротация мастер-ключа:
- Owner кладёт `MASTER_KEY_V2` в `.env`.
- При первом чтении DEK воркер расшифровывает старым ключом, перешифровывает
  новым, обновляет запись → `dek_version = 2`.
- Сам auth key не трогается — Telegram-сессия не пересоздаётся.

Всё AES-256-GCM через `cryptography`. Никакого самопального крипто.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_LEN = 32           # AES-256
_NONCE_LEN = 12         # AES-GCM standard
_DEK_LEN = 32


class DecryptionError(Exception):
    """Не удалось расшифровать: неверный ключ, тег, AAD или повреждённый ciphertext."""


@dataclass(frozen=True)
class WrappedDek:
    """DEK, обёрнутый мастер-ключом, версия KEK и nonce."""

    ciphertext: bytes
    kek_version: int

    @property
    def encoded(self) -> bytes:
        """Формат хранения: `версия(1) || nonce(12) || ciphertext+tag(N)`.

        Один blob упрощает миграции — `dek_wrapped BYTEA` в БД.
        """
        return bytes([self.kek_version]) + self.ciphertext


def _split_wrapped_dek(blob: bytes) -> WrappedDek:
    if len(blob) < 1 + _NONCE_LEN + 16:  # 16 = GCM tag length
        raise DecryptionError("Слишком короткий wrapped DEK")
    return WrappedDek(kek_version=blob[0], ciphertext=blob[1:])


def _new_nonce() -> bytes:
    return os.urandom(_NONCE_LEN)


def generate_dek() -> bytes:
    """Свежий 32-байтный DEK для нового аккаунта."""
    return os.urandom(_DEK_LEN)


def wrap_dek(dek: bytes, *, kek: bytes, kek_version: int, account_id: str) -> WrappedDek:
    """Зашифровать DEK мастер-ключом.

    AAD = `wrap:{account_id}` — привязывает wrapped DEK к аккаунту.
    """
    if len(dek) != _DEK_LEN:
        raise ValueError(f"DEK должен быть {_DEK_LEN} байт, получено {len(dek)}")
    if len(kek) != _KEY_LEN:
        raise ValueError(f"KEK должен быть {_KEY_LEN} байт, получено {len(kek)}")

    aad = f"wrap:{account_id}".encode()
    nonce = _new_nonce()
    ct = AESGCM(kek).encrypt(nonce, dek, aad)
    return WrappedDek(kek_version=kek_version, ciphertext=nonce + ct)


def unwrap_dek(wrapped: WrappedDek, *, kek: bytes, account_id: str) -> bytes:
    """Расшифровать wrapped DEK. Возвращает 32 байта DEK."""
    if len(kek) != _KEY_LEN:
        raise ValueError(f"KEK должен быть {_KEY_LEN} байт, получено {len(kek)}")
    aad = f"wrap:{account_id}".encode()
    nonce, ct = wrapped.ciphertext[:_NONCE_LEN], wrapped.ciphertext[_NONCE_LEN:]
    try:
        return AESGCM(kek).decrypt(nonce, ct, aad)
    except Exception as e:
        raise DecryptionError("Не удалось расшифровать DEK") from e


def encrypt_field(plaintext: bytes, *, dek: bytes, account_id: str, field: str) -> bytes:
    """Зашифровать значение поля DEK-ом.

    Формат: `nonce(12) || ciphertext+tag(N)`. AAD = `f"{account_id}:{field}"`.
    """
    if len(dek) != _DEK_LEN:
        raise ValueError(f"DEK должен быть {_DEK_LEN} байт, получено {len(dek)}")
    aad = f"{account_id}:{field}".encode()
    nonce = _new_nonce()
    ct = AESGCM(dek).encrypt(nonce, plaintext, aad)
    return nonce + ct


def decrypt_field(blob: bytes, *, dek: bytes, account_id: str, field: str) -> bytes:
    if len(dek) != _DEK_LEN:
        raise ValueError(f"DEK должен быть {_DEK_LEN} байт")
    if len(blob) < _NONCE_LEN + 16:
        raise DecryptionError("Слишком короткий ciphertext")
    aad = f"{account_id}:{field}".encode()
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    try:
        return AESGCM(dek).decrypt(nonce, ct, aad)
    except Exception as e:
        raise DecryptionError(f"Не удалось расшифровать поле {field}") from e


def load_wrapped_dek(blob: bytes) -> WrappedDek:
    """Обратная функция к `WrappedDek.encoded`."""
    return _split_wrapped_dek(blob)
