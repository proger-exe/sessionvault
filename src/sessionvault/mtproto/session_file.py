"""Pyrogram/Kurigram `.session` file builder."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from pyrogram.storage import sqlite_storage

SESSION_VERSION = int(sqlite_storage.SQLiteStorage.VERSION)
SESSION_SCHEMA = str(sqlite_storage.SCHEMA)


def build_pyrogram_session_file(
    target: Path,
    *,
    auth_key: bytes,
    dc_id: int,
    api_id: int,
    user_id: int,
    is_bot: bool = False,
    test_mode: bool = False,
) -> None:
    """Create a clean `.session` file compatible with Pyrogram/Kurigram 2.x."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    conn = sqlite3.connect(target)
    try:
        conn.executescript(SESSION_SCHEMA)
        conn.execute("INSERT OR REPLACE INTO version (number) VALUES (?)", (SESSION_VERSION,))
        server_address = (
            sqlite_storage.TEST[dc_id] if test_mode else sqlite_storage.PROD[dc_id]
        )
        port = 80 if test_mode else 443
        conn.execute(
            """
            INSERT OR REPLACE INTO sessions
                (dc_id, server_address, port, api_id, test_mode, auth_key, date, user_id, is_bot)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dc_id,
                server_address,
                port,
                api_id,
                int(test_mode),
                auth_key,
                int(time.time()),
                user_id,
                int(is_bot),
            ),
        )
        conn.commit()
    finally:
        conn.close()
