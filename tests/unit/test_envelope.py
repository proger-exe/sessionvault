"""Envelope encryption: round-trip, AAD-защита, отказ на порче."""

from __future__ import annotations

import os

import pytest

from sessionvault.security.envelope import (
    DecryptionError,
    decrypt_field,
    encrypt_field,
    generate_dek,
    load_wrapped_dek,
    unwrap_dek,
    wrap_dek,
)

ACCOUNT_A = "00000000-0000-4000-8000-000000000001"
ACCOUNT_B = "00000000-0000-4000-8000-000000000002"


def _kek() -> bytes:
    return os.urandom(32)


def test_wrap_unwrap_round_trip() -> None:
    kek = _kek()
    dek = generate_dek()
    wrapped = wrap_dek(dek, kek=kek, kek_version=1, account_id=ACCOUNT_A)
    restored = unwrap_dek(wrapped, kek=kek, account_id=ACCOUNT_A)
    assert restored == dek


def test_wrap_encoded_prefixes_version() -> None:
    kek = _kek()
    dek = generate_dek()
    wrapped = wrap_dek(dek, kek=kek, kek_version=3, account_id=ACCOUNT_A)
    assert wrapped.encoded[0] == 3
    assert load_wrapped_dek(wrapped.encoded).kek_version == 3


def test_unwrap_wrong_kek_fails() -> None:
    kek = _kek()
    wrapped = wrap_dek(generate_dek(), kek=kek, kek_version=1, account_id=ACCOUNT_A)
    with pytest.raises(DecryptionError):
        unwrap_dek(wrapped, kek=_kek(), account_id=ACCOUNT_A)


def test_unwrap_wrong_account_fails() -> None:
    kek = _kek()
    wrapped = wrap_dek(generate_dek(), kek=kek, kek_version=1, account_id=ACCOUNT_A)
    with pytest.raises(DecryptionError):
        unwrap_dek(wrapped, kek=kek, account_id=ACCOUNT_B)


def test_encrypt_decrypt_field_round_trip() -> None:
    dek = generate_dek()
    plaintext = b"telegram-auth-key-256-bytes-mock"
    ct = encrypt_field(plaintext, dek=dek, account_id=ACCOUNT_A, field="session")
    assert plaintext not in ct
    assert decrypt_field(ct, dek=dek, account_id=ACCOUNT_A, field="session") == plaintext


def test_field_aad_binds_account_and_name() -> None:
    dek = generate_dek()
    ct = encrypt_field(b"secret", dek=dek, account_id=ACCOUNT_A, field="session")
    # Не сможем расшифровать под другим аккаунтом.
    with pytest.raises(DecryptionError):
        decrypt_field(ct, dek=dek, account_id=ACCOUNT_B, field="session")
    # Не сможем расшифровать как «другое поле».
    with pytest.raises(DecryptionError):
        decrypt_field(ct, dek=dek, account_id=ACCOUNT_A, field="twofa")


def test_ciphertext_tamper_detected() -> None:
    dek = generate_dek()
    ct = encrypt_field(b"secret" * 10, dek=dek, account_id=ACCOUNT_A, field="session")
    tampered = bytearray(ct)
    tampered[-1] ^= 0x01
    with pytest.raises(DecryptionError):
        decrypt_field(bytes(tampered), dek=dek, account_id=ACCOUNT_A, field="session")


def test_wrong_key_lengths_rejected() -> None:
    with pytest.raises(ValueError):
        wrap_dek(b"short", kek=os.urandom(32), kek_version=1, account_id=ACCOUNT_A)
    with pytest.raises(ValueError):
        wrap_dek(generate_dek(), kek=b"short", kek_version=1, account_id=ACCOUNT_A)
