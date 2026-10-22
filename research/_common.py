"""Общие утилиты для research-скриптов Этапа 0.

Загрузка .env, каталог состояния, безопасный логгер, редакция секретов.
Ничего отсюда не должно уехать в production.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

console = Console()


@dataclass(frozen=True)
class Env:
    api_id: int
    api_hash: str
    test_phone: str
    state_dir: Path

    @classmethod
    def load(cls) -> "Env":
        # Основные имена — PYROGRAM_API_ID/HASH; API_ID/API_HASH оставлены как
        # синоним на случай, если у оператора уже был такой .env.
        api_id = os.environ.get("PYROGRAM_API_ID") or os.environ.get("API_ID")
        api_hash = os.environ.get("PYROGRAM_API_HASH") or os.environ.get("API_HASH")
        phone = os.environ.get("TEST_PHONE")
        state = os.environ.get("RESEARCH_STATE_DIR", "./_state")

        missing = [
            name
            for name, value in (
                ("PYROGRAM_API_ID", api_id),
                ("PYROGRAM_API_HASH", api_hash),
                ("TEST_PHONE", phone),
            )
            if not value
        ]
        if missing:
            raise SystemExit(
                f"[env] отсутствуют переменные: {', '.join(missing)}. "
                "Заполни research/.env."
            )
        assert api_id and api_hash and phone
        state_dir = (_HERE / state).resolve() if state.startswith(".") else Path(state).resolve()
        state_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            api_id=int(api_id),
            api_hash=api_hash,
            test_phone=phone,
            state_dir=state_dir,
        )


_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{5,7}\b"),                       # login code
    re.compile(r"(?i)api[_-]?hash[=:\s]*[a-f0-9]+"),
    re.compile(r"(?i)auth[_-]?key[=:\s]*[a-z0-9+/=]+"),
    re.compile(r"\+\d{7,15}"),                        # phone E.164
)


class SecretFilter(logging.Filter):
    """Грубая редакция для research-логов. Production-фильтр отдельный."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pat in _SECRET_PATTERNS:
            msg = pat.sub("***REDACTED***", msg)
        record.msg = msg
        record.args = ()
        return True


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    root = logging.getLogger()
    root.handlers.clear()
    handler = RichHandler(console=console, show_path=False, rich_tracebacks=True)
    handler.addFilter(SecretFilter())
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    return logging.getLogger("research")


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())
