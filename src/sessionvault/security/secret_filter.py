"""Фильтр логгера, который редактирует секреты перед выводом.

Подключается на root logger в `logging.setup_logging()`. Задача — не пустить
в логи:
- номера в E.164 (+7…);
- login-коды (5–7 цифр в сообщении, где рядом есть слово phone/code/2fa);
- Base64 auth_key / session_string / MASTER_KEY-подобные строки от 32+ символов;
- значения полей `api_hash`, `password`, `code`, `phone` из `record.args`.

Фильтр не панацея: доменный код всё равно обязан не логировать секреты
напрямую (используется `SecretStr`). Это второй эшелон защиты — то самое
«CI-тест, специально роняющий секреты», из плана Этапа 1.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from contextlib import suppress
from typing import Any

_PLACEHOLDER = "***REDACTED***"

# Точные ключи полей, которые никогда не попадают в логи через record.args.
_SENSITIVE_KEYS: tuple[str, ...] = (
    "api_hash",
    "auth_key",
    "code",
    "master_key",
    "password",
    "phone",
    "phone_code_hash",
    "recovery_email",
    "session",
    "twofa",
)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\+\d{7,15}"),                                 # phone E.164
    re.compile(r"(?<![A-Za-z0-9])\d{5,7}(?![A-Za-z0-9])"),      # bare login code
    re.compile(r"(?i)\b(?:api[_-]?hash|master[_-]?key|auth[_-]?key|password)\s*[=:]\s*\S+"),
    re.compile(r"[A-Za-z0-9+/=]{40,}"),                         # long base64 (session/auth key)
)


def _redact_str(value: str) -> str:
    for pat in _SECRET_PATTERNS:
        value = pat.sub(_PLACEHOLDER, value)
    return value


def _redact_arg(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_str(value)
    if isinstance(value, dict):
        return {k: (_PLACEHOLDER if k in _SENSITIVE_KEYS else _redact_arg(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        redacted: Iterable[Any] = (_redact_arg(v) for v in value)
        return type(value)(redacted)
    return value


class SecretFilter(logging.Filter):
    """Редакция секретов в сообщении и args.

    Работает после `%`-форматирования: сначала пропускает `record.getMessage()`
    через regex, потом заменяет содержимое `record.msg` и очищает `record.args`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = _redact_str(rendered)
        record.args = ()

        if record.exc_info and record.exc_info[1]:
            exc = record.exc_info[1]
            with suppress(Exception):
                exc.args = tuple(_redact_arg(a) for a in exc.args)

        return True


def install(logger: logging.Logger | None = None) -> SecretFilter:
    """Подключить фильтр к logger (по умолчанию root) и ко всем его handlers.

    Возвращает установленный экземпляр, чтобы можно было снять в тестах.
    """
    target = logger or logging.getLogger()
    flt = SecretFilter()
    target.addFilter(flt)
    for h in target.handlers:
        h.addFilter(flt)
    return flt
