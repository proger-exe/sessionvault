from __future__ import annotations

import sqlite3

import pytest

from sessionvault.mtproto.session_file import SESSION_VERSION, build_pyrogram_session_file
from sessionvault.mtproto.session_import import SessionImportError, inspect_pyrogram_session

AUTH_KEY = b"x" * 256


def test_builds_native_kurigram_session_schema(tmp_path):
    path = tmp_path / "account.session"

    build_pyrogram_session_file(
        path,
        auth_key=AUTH_KEY,
        dc_id=2,
        api_id=12345,
        user_id=67890,
    )

    conn = sqlite3.connect(path)
    try:
        version = conn.execute("SELECT number FROM version").fetchone()[0]
        row = conn.execute(
            "SELECT dc_id, server_address, port, api_id, auth_key, user_id, is_bot FROM sessions"
        ).fetchone()
    finally:
        conn.close()

    assert version == SESSION_VERSION == 7
    assert row == (2, "149.154.167.51", 443, 12345, AUTH_KEY, 67890, 0)


def test_inspects_current_kurigram_session(tmp_path):
    path = tmp_path / "account.session"
    build_pyrogram_session_file(
        path,
        auth_key=AUTH_KEY,
        dc_id=2,
        api_id=12345,
        user_id=67890,
    )

    info = inspect_pyrogram_session(path)

    assert info.dc_id == 2
    assert info.api_id == 12345
    assert info.user_id == 67890
    assert info.version == SESSION_VERSION


def test_inspect_allows_older_pyrogram_schema_for_kurigram_migration(tmp_path):
    path = tmp_path / "old.session"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions
            (
                dc_id     INTEGER PRIMARY KEY,
                api_id    INTEGER,
                test_mode INTEGER,
                auth_key  BLOB,
                date      INTEGER NOT NULL,
                user_id   INTEGER,
                is_bot    INTEGER
            );
            CREATE TABLE peers
            (
                id             INTEGER PRIMARY KEY,
                access_hash    INTEGER,
                type           INTEGER NOT NULL,
                phone_number   TEXT,
                last_update_on INTEGER NOT NULL
            );
            CREATE TABLE version (number INTEGER PRIMARY KEY);
            """
        )
        conn.execute("INSERT INTO version VALUES (3)")
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (2, 12345, 0, AUTH_KEY, 0, 67890, 0),
        )
        conn.commit()
    finally:
        conn.close()

    info = inspect_pyrogram_session(path)

    assert info.version == 3
    assert info.dc_id == 2


def test_rejects_invalid_auth_key(tmp_path):
    path = tmp_path / "bad.session"
    build_pyrogram_session_file(
        path,
        auth_key=AUTH_KEY,
        dc_id=2,
        api_id=12345,
        user_id=67890,
    )
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE sessions SET auth_key = ?", (b"short",))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SessionImportError, match="SESSION_AUTH_KEY_INVALID"):
        inspect_pyrogram_session(path)
