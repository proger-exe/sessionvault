"""Import panel-style `.session` + `.json` account pairs."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from sessionvault.mtproto.login import ExtractedSession

_PHONE_RE = re.compile(r"^\+\d{7,15}$")


class PanelSessionImportError(Exception):
    """Panel pair import rejected before secrets are persisted."""


@dataclass(frozen=True)
class PanelSessionData:
    phone_e164: str
    session: ExtractedSession


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _read_required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None:
        raise PanelSessionImportError(f"PANEL_JSON_{key.upper()}_MISSING")
    return str(value)


def _read_optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None or value == "":
        return None
    return str(value)


def _normalize_phone(raw: str) -> str:
    phone = raw.strip()
    normalized = phone if phone.startswith("+") else f"+{phone}"
    if not _PHONE_RE.fullmatch(normalized):
        raise PanelSessionImportError("PANEL_JSON_PHONE_INVALID")
    return normalized


def _read_panel_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PanelSessionImportError("PANEL_JSON_READ_FAILED") from exc
    if not isinstance(raw, dict):
        raise PanelSessionImportError("PANEL_JSON_UNSUPPORTED")
    return cast(dict[str, Any], raw)


def _read_panel_session(path: Path) -> tuple[int, bytes]:
    if not path.exists() or path.stat().st_size == 0:
        raise PanelSessionImportError("PANEL_SESSION_FILE_EMPTY")

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise PanelSessionImportError("PANEL_SESSION_SQLITE_OPEN_FAILED") from exc

    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not {"sessions", "version"}.issubset(tables):
            raise PanelSessionImportError("PANEL_SESSION_SCHEMA_UNSUPPORTED")

        version_columns = _table_columns(conn, "version")
        if "version" not in version_columns:
            raise PanelSessionImportError("PANEL_SESSION_SCHEMA_UNSUPPORTED")
        version_row = conn.execute("SELECT version FROM version LIMIT 1").fetchone()
        if version_row is None or int(version_row[0]) < 1:
            raise PanelSessionImportError("PANEL_SESSION_VERSION_UNSUPPORTED")

        session_columns = _table_columns(conn, "sessions")
        if not {"dc_id", "auth_key"}.issubset(session_columns):
            raise PanelSessionImportError("PANEL_SESSION_SCHEMA_UNSUPPORTED")
        row = conn.execute("SELECT dc_id, auth_key FROM sessions LIMIT 1").fetchone()
        if row is None:
            raise PanelSessionImportError("PANEL_SESSION_NO_AUTH_ROW")
        dc_id, auth_key = row
        if not isinstance(auth_key, bytes) or len(auth_key) != 256:
            raise PanelSessionImportError("PANEL_SESSION_AUTH_KEY_INVALID")
        return int(dc_id), auth_key
    except sqlite3.Error as exc:
        raise PanelSessionImportError("PANEL_SESSION_SQLITE_READ_FAILED") from exc
    finally:
        conn.close()


def import_panel_session_pair(session_path: Path, json_path: Path) -> PanelSessionData:
    """Extract account data from panel-style `.session` + same-account `.json`."""
    dc_id, auth_key = _read_panel_session(session_path)
    metadata = _read_panel_json(json_path)

    phone = _normalize_phone(_read_required_str(metadata, "phone"))
    user_id_raw = metadata.get("user_id")
    if user_id_raw is None:
        raise PanelSessionImportError("PANEL_JSON_USER_ID_MISSING")

    return PanelSessionData(
        phone_e164=phone,
        session=ExtractedSession(
            auth_key=auth_key,
            dc_id=dc_id,
            user_id=int(user_id_raw),
            is_bot=False,
            first_name=_read_optional_str(metadata, "first_name"),
            last_name=_read_optional_str(metadata, "last_name"),
            username=_read_optional_str(metadata, "username"),
            has_2fa=bool(metadata.get("twoFA")),
        ),
    )
