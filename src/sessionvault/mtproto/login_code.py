"""Read login codes from Telegram service messages."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from pyrogram import Client  # type: ignore[attr-defined]

SERVICE_CHAT_ID = 777000
CODE_RE = re.compile(r"(?<!\d)(\d{5,6})(?!\d)")


def extract_login_code(text: str) -> str | None:
    """Return the first Telegram-style login code from service text."""
    match = CODE_RE.search(text)
    return match.group(1) if match else None


async def snapshot_service_message_ids(client: Client, *, limit: int = 10) -> set[int]:
    """IDs already present before a new login-code request."""
    ids: set[int] = set()
    async for message in client.get_chat_history(SERVICE_CHAT_ID, limit=limit):
        message_id = getattr(message, "id", None)
        if isinstance(message_id, int):
            ids.add(message_id)
    return ids


def _message_text(message: Any) -> str:
    return str(getattr(message, "text", None) or getattr(message, "caption", None) or "")


def _message_date(message: Any) -> datetime | None:
    value = getattr(message, "date", None)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def wait_for_login_code(
    client: Client,
    *,
    known_message_ids: Iterable[int],
    started_at: datetime,
    timeout_seconds: int = 180,
    poll_interval_seconds: float = 2.0,
) -> str | None:
    """Poll Telegram service chat and return a fresh login code."""
    known = set(known_message_ids)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    started_at_utc = started_at.astimezone(UTC)

    while asyncio.get_running_loop().time() < deadline:
        async for message in client.get_chat_history(SERVICE_CHAT_ID, limit=8):
            message_id = getattr(message, "id", None)
            if isinstance(message_id, int) and message_id in known:
                continue
            date = _message_date(message)
            if date is not None and date < started_at_utc:
                continue
            code = extract_login_code(_message_text(message))
            if code is not None:
                return code
            if isinstance(message_id, int):
                known.add(message_id)
        await asyncio.sleep(poll_interval_seconds)
    return None
