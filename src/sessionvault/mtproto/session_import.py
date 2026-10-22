"""Import and verify Pyrogram/Kurigram `.session` files."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pyrogram import Client  # type: ignore[attr-defined]

from sessionvault.mtproto.login import ExtractedSession
from sessionvault.mtproto.session_file import SESSION_VERSION


class SessionImportError(Exception):
    """Import rejected before secrets are persisted."""


@dataclass(frozen=True)
class SessionSchemaInfo:
    dc_id: int
    api_id: int | None
    user_id: int | None
    is_bot: bool
    version: int


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def inspect_pyrogram_session(path: Path) -> SessionSchemaInfo:
    """Validate the minimum Pyrogram SQLite schema without exposing auth_key."""
    if not path.exists() or path.stat().st_size == 0:
        raise SessionImportError("SESSION_FILE_EMPTY")

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise SessionImportError("SESSION_SQLITE_OPEN_FAILED") from exc

    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not {"sessions", "version"}.issubset(tables):
            raise SessionImportError("SESSION_SCHEMA_UNSUPPORTED")

        version_row = conn.execute("SELECT number FROM version LIMIT 1").fetchone()
        if version_row is None:
            raise SessionImportError("SESSION_VERSION_UNSUPPORTED")
        version = int(version_row[0])
        if version < 1 or version > SESSION_VERSION:
            raise SessionImportError("SESSION_VERSION_UNSUPPORTED")

        columns = _table_columns(conn, "sessions")
        required = {"dc_id", "auth_key"}
        if not required.issubset(columns):
            raise SessionImportError("SESSION_SCHEMA_UNSUPPORTED")

        selected = ["dc_id", "auth_key"]
        optional = ["api_id", "user_id", "is_bot"]
        selected.extend(name for name in optional if name in columns)
        row = conn.execute(f"SELECT {', '.join(selected)} FROM sessions LIMIT 1").fetchone()
        if row is None:
            raise SessionImportError("SESSION_NO_AUTH_ROW")

        values = dict(zip(selected, row, strict=True))
        dc_id = values["dc_id"]
        auth_key = values["auth_key"]
        if not isinstance(auth_key, bytes) or len(auth_key) != 256:
            raise SessionImportError("SESSION_AUTH_KEY_INVALID")
        return SessionSchemaInfo(
            dc_id=int(dc_id),
            api_id=int(values["api_id"]) if values.get("api_id") is not None else None,
            user_id=int(values["user_id"]) if values.get("user_id") is not None else None,
            is_bot=bool(values.get("is_bot")),
            version=version,
        )
    except sqlite3.Error as exc:
        raise SessionImportError("SESSION_SQLITE_READ_FAILED") from exc
    finally:
        conn.close()


async def import_pyrogram_session(
    path: Path,
    *,
    api_id: int,
    api_hash: str,
) -> ExtractedSession:
    """Open the uploaded session with pinned Kurigram and extract auth metadata."""
    inspect_pyrogram_session(path)

    root = Path(tempfile.mkdtemp(prefix="sessionvault-import-"))
    session_name = root / "imported"
    session_file = session_name.with_suffix(".session")
    shutil.copyfile(path, session_file)

    client = Client(name=str(session_name), api_id=api_id, api_hash=api_hash, no_updates=True)
    await client.start()
    try:
        me = await client.get_me()
        storage = client.storage
        auth_key = await storage.auth_key()
        dc_id = await storage.dc_id()
    except Exception as exc:
        raise SessionImportError(type(exc).__name__) from exc
    finally:
        await client.stop()
        shutil.rmtree(root, ignore_errors=True)

    return ExtractedSession(
        auth_key=auth_key,
        dc_id=dc_id,
        user_id=me.id,
        is_bot=bool(me.is_bot),
        first_name=me.first_name,
        last_name=me.last_name,
        username=me.username,
        has_2fa=False,
    )
