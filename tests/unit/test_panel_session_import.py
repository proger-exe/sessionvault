from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sessionvault.mtproto.panel_session_import import (
    PanelSessionImportError,
    import_panel_session_pair,
)

AUTH_KEY = b"y" * 256


def _write_panel_session(path: Path, *, auth_key: bytes = AUTH_KEY) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions
            (
                dc_id INTEGER PRIMARY KEY,
                server_address TEXT,
                port INTEGER,
                auth_key BLOB,
                takeout_id INTEGER
            );
            CREATE TABLE entities
            (
                id INTEGER PRIMARY KEY,
                hash INTEGER,
                username TEXT,
                phone INTEGER,
                name TEXT,
                date INTEGER
            );
            CREATE TABLE sent_files
            (
                md5_digest BLOB,
                file_size INTEGER,
                type INTEGER,
                id INTEGER,
                hash INTEGER
            );
            CREATE TABLE update_state
            (
                id INTEGER PRIMARY KEY,
                pts INTEGER,
                qts INTEGER,
                date INTEGER,
                seq INTEGER
            );
            CREATE TABLE version (version INTEGER);
            """
        )
        conn.execute("INSERT INTO version VALUES (7)")
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            (2, "149.154.167.51", 443, auth_key, None),
        )
        conn.commit()
    finally:
        conn.close()


def _write_panel_json(path: Path, phone: str = "998770269504") -> None:
    path.write_text(
        json.dumps(
            {
                "phone": phone,
                "user_id": 8608788347,
                "first_name": "Cleon",
                "last_name": "Iso",
                "username": "cleonpwws",
                "twoFA": None,
            }
        )
    )


def test_import_panel_session_pair(tmp_path):
    session_path = tmp_path / "998770269504.session"
    json_path = tmp_path / "998770269504.json"
    _write_panel_session(session_path)
    _write_panel_json(json_path)

    imported = import_panel_session_pair(session_path, json_path)

    assert imported.phone_e164 == "+998770269504"
    assert imported.session.dc_id == 2
    assert imported.session.auth_key == AUTH_KEY
    assert imported.session.user_id == 8608788347
    assert imported.session.first_name == "Cleon"
    assert imported.session.last_name == "Iso"
    assert imported.session.username == "cleonpwws"
    assert imported.session.has_2fa is False


def test_rejects_panel_session_bad_auth_key(tmp_path):
    session_path = tmp_path / "bad.session"
    json_path = tmp_path / "bad.json"
    _write_panel_session(session_path, auth_key=b"short")
    _write_panel_json(json_path)

    with pytest.raises(PanelSessionImportError, match="PANEL_SESSION_AUTH_KEY_INVALID"):
        import_panel_session_pair(session_path, json_path)


def test_rejects_panel_json_bad_phone(tmp_path):
    session_path = tmp_path / "bad.session"
    json_path = tmp_path / "bad.json"
    _write_panel_session(session_path)
    _write_panel_json(json_path, phone="not-a-phone")

    with pytest.raises(PanelSessionImportError, match="PANEL_JSON_PHONE_INVALID"):
        import_panel_session_pair(session_path, json_path)
