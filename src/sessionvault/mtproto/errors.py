"""Нормализованные ошибки login/profile flow.

Kurigram кидает `pyrogram.errors.*` — конкретный класс/ID сохраняем в
`code`, домен работает через enum.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LoginErrorKind(str, Enum):
    PHONE_CODE_INVALID = "phone_code_invalid"
    PHONE_CODE_EXPIRED = "phone_code_expired"
    PHONE_NUMBER_INVALID = "phone_number_invalid"
    PHONE_NUMBER_BANNED = "phone_number_banned"
    SESSION_PASSWORD_NEEDED = "session_password_needed"
    PASSWORD_HASH_INVALID = "password_hash_invalid"
    FLOOD_WAIT = "flood_wait"
    NETWORK = "network"
    OTHER = "other"


@dataclass(frozen=True)
class LoginError(Exception):
    kind: LoginErrorKind
    code: str
    detail: str = ""
    retry_after: int | None = None

    def __str__(self) -> str:
        parts = [f"{self.kind.value}"]
        if self.code and self.code != self.kind.value:
            parts.append(self.code)
        if self.retry_after is not None:
            parts.append(f"retry_after={self.retry_after}s")
        if self.detail:
            parts.append(self.detail)
        return " ".join(parts)
