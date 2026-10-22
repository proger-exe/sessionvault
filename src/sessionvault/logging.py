"""Настройка логирования на все процессы (bot, worker).

Один вход: `setup_logging()`. Ставит структурный формат, устанавливает уровень
из `Settings.log_level`, вешает `SecretFilter` на root logger.
"""

from __future__ import annotations

import logging
import sys

from sessionvault.config import get_settings
from sessionvault.security.secret_filter import install as install_secret_filter

_CONFIGURED = False


def setup_logging() -> None:
    """Идемпотентная настройка root logger. Повторный вызов — no-op."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = getattr(logging, get_settings().log_level, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root.addHandler(handler)

    install_secret_filter(root)

    # Шумные библиотеки — на WARN.
    for noisy in ("pyrogram", "kurigram", "telethon", "aiogram.event", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
