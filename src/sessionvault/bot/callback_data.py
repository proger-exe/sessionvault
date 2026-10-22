"""Единственный источник callback-схем бота.

Правила:
- никаких секретов в callback_data (номер, код, пароль, auth_key, session);
- версионирование через префикс `v=` — при breaking-изменении инкрементим,
  старые callback идут в отказ через глобальный handler;
- account_id/user_id — UUID/int, безопасно передавать;
- для критичных действий (export, delete, phone_change, twofa) callback
  включает HMAC-подпись; проверяется на границе хендлера — см. `sign()`.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid

from aiogram.filters.callback_data import CallbackData

from sessionvault.config import Settings

SCHEMA_VERSION = 1


class MenuCB(CallbackData, prefix="menu"):
    """Главное меню: `menu:section`."""

    section: str


class AccountsPageCB(CallbackData, prefix="acc"):
    """Список аккаунтов с пагинацией."""

    action: str  # page | open | back
    page: int = 0
    account_id: str = ""  # UUID для action=open


class AccountCardCB(CallbackData, prefix="card"):
    """Действия внутри карточки аккаунта."""

    action: str  # profile | security | export | audit | login_code | close
    account_id: str


class ProfileCB(CallbackData, prefix="prof"):
    """Profile edit actions."""

    action: str
    account_id: str


class AdminsCB(CallbackData, prefix="adm"):
    """Управление админами (только Owner)."""

    action: str  # list | add | remove | back
    tg_user_id: int = 0


class AuditCB(CallbackData, prefix="aud"):
    """Просмотр аудита."""

    action: str  # page | filter | back
    page: int = 0
    account_id: str = ""


class SecurityCB(CallbackData, prefix="sec"):
    """Security submenu: 2FA / recovery / sessions / phone."""

    action: str  # menu | twofa_menu | twofa_enable | twofa_change | twofa_remove
                 # | recovery_confirm | sessions | session_revoke | phone_change
                 # | back
    account_id: str = ""
    arg: str = ""  # session_id / any small string; NEVER a secret


class ConfirmCB(CallbackData, prefix="cfx"):
    """Опасные подтверждения. Используется вместе с sign()/verify()."""

    action: str        # export | phone_change | twofa | admin_remove
    account_id: str    # UUID или пустая
    target_id: int = 0  # tg_user_id для admin_remove
    sig: str = ""       # HMAC-SHA256 hex-truncated


def _sig_payload(cb: ConfirmCB) -> bytes:
    return f"v{SCHEMA_VERSION}:{cb.action}:{cb.account_id}:{cb.target_id}".encode()


def sign_confirm(cb: ConfirmCB, settings: Settings) -> ConfirmCB:
    """Возвращает callback с подписью — вызывается перед формированием кнопки.

    Telegram callback_data limit is 64 bytes: `cfx:{action}:{uuid36}:0:{sig}`
    with 12-char action already reaches 64 with an 8-char sig. 8 hex chars =
    32 bits of HMAC truncation — enough for anti-tamper on owner-only buttons.
    ponytail: sig=8; move to Redis-stored token if actions ever exceed 12 chars.
    """
    key = settings.master_key_bytes(version=1)  # BOT_TOKEN тоже подошёл бы, но мастер-ключ уже есть
    digest = hmac.new(key, _sig_payload(cb), hashlib.sha256).hexdigest()
    return cb.model_copy(update={"sig": digest[:8]})


def verify_confirm(cb: ConfirmCB, settings: Settings) -> bool:
    """Проверяет HMAC подпись критичного callback. Возвращает True/False."""
    if not cb.sig:
        return False
    expected = sign_confirm(cb.model_copy(update={"sig": ""}), settings).sig
    return hmac.compare_digest(cb.sig, expected)


def is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


__all__ = [
    "SCHEMA_VERSION",
    "AccountCardCB",
    "AccountsPageCB",
    "AdminsCB",
    "AuditCB",
    "ConfirmCB",
    "MenuCB",
    "ProfileCB",
    "SecurityCB",
    "is_valid_uuid",
    "sign_confirm",
    "verify_confirm",
]
