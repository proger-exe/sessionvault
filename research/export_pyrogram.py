# pyright: reportPrivateImportUsage=false, reportArgumentType=false
"""Этап 0.3 — сборка .session из auth key + метаданных.

Kurigram сохраняет upstream-Pyrogram схему таблиц `sessions/peers/version`
(`session_version=3`). Тот же файл читается и оригинальным Pyrogram, что даёт
совместимость наружу.

Шаги:
1. Прочитать `_state/owner.auth_key.bin` и `_state/owner.meta.json`.
2. Создать чистый SQLite-файл `_state/export_pyrogram.session` со схемой Pyrogram/Kurigram.
3. Заполнить таблицу `sessions` (dc_id, api_id, test_mode, auth_key, date, user_id, is_bot)
   и таблицу `version` (номер схемы, 3).
4. Открыть свежим клиентом Kurigram, вызвать `get_me()`, сверить user_id.
5. Записать факт успеха и версию формата в `_state/export_pyrogram.json`.

Никаких форк-специфичных полей — только то, что читает upstream-Pyrogram, чтобы
итоговый экспорт был переносим.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import time
from pathlib import Path

from pyrogram import Client  # type: ignore[attr-defined]

from _common import Env, load_json, save_json, setup_logging

log = setup_logging()

# Схема Pyrogram/Kurigram 2.x, session_version=3:
#   https://github.com/KurimuzonAkuma/pyrogram/blob/dev/pyrogram/storage/file_storage.py
# (совместимо с upstream https://github.com/pyrogram/pyrogram)
SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions
(
    dc_id     INTEGER PRIMARY KEY,
    api_id    INTEGER,
    test_mode INTEGER,
    auth_key  BLOB,
    date      INTEGER NOT NULL,
    user_id   INTEGER,
    is_bot    INTEGER
);

CREATE TABLE IF NOT EXISTS peers
(
    id             INTEGER PRIMARY KEY,
    access_hash    INTEGER,
    type           INTEGER NOT NULL,
    username       TEXT,
    phone_number   TEXT,
    last_update_on INTEGER NOT NULL DEFAULT (CAST(STRFTIME('%s','now') AS INTEGER))
);

CREATE TABLE IF NOT EXISTS version
(
    number INTEGER PRIMARY KEY
);

CREATE INDEX IF NOT EXISTS idx_peers_username ON peers (username);
CREATE INDEX IF NOT EXISTS idx_peers_phone_number ON peers (phone_number);
CREATE TRIGGER IF NOT EXISTS trg_peers_last_update_on
    AFTER UPDATE
    ON peers
BEGIN
    UPDATE peers
    SET last_update_on = CAST(STRFTIME('%s', 'now') AS INTEGER)
    WHERE id = NEW.id;
END;
"""

SESSION_VERSION = 3


def build_session_file(target: Path, *, auth_key: bytes, meta: dict) -> None:
    if target.exists():
        target.unlink()
    conn = sqlite3.connect(target)
    try:
        conn.executescript(SESSION_SCHEMA)
        conn.execute("INSERT OR REPLACE INTO version (number) VALUES (?)", (SESSION_VERSION,))
        conn.execute(
            """
            INSERT OR REPLACE INTO sessions
                (dc_id, api_id, test_mode, auth_key, date, user_id, is_bot)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(meta["dc_id"]),
                int(meta["api_id"]),
                int(bool(meta.get("test_mode", False))),
                auth_key,
                int(time.time()),
                int(meta["user_id"]),
                int(bool(meta.get("is_bot", False))),
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def verify(target: Path, *, env: Env, expected_user_id: int) -> dict:
    """Открыть .session pinned-Kurigram и вызвать get_me."""
    # Копируем в отдельный каталог, чтобы Pyrogram точно не задел рабочую сессию.
    verify_dir = env.state_dir / "verify_pyrogram"
    verify_dir.mkdir(parents=True, exist_ok=True)
    verify_path = verify_dir / "session"
    shutil.copyfile(target, verify_path.with_suffix(".session"))

    client = Client(
        name=str(verify_path),
        api_id=env.api_id,
        api_hash=env.api_hash,
        no_updates=True,
    )
    await client.start()
    try:
        me = await client.get_me()
        result = {
            "user_id": me.id,
            "dc_id": me.dc_id,
            "first_name": me.first_name,
            "username": me.username,
            "match_expected": me.id == expected_user_id,
        }
    finally:
        await client.stop()
    return result


async def run() -> None:
    env = Env.load()
    auth_key_path = env.state_dir / "owner.auth_key.bin"
    meta_path = env.state_dir / "owner.meta.json"
    if not auth_key_path.exists() or not meta_path.exists():
        log.error("Нет %s или %s — сначала запусти login.py", auth_key_path.name, meta_path.name)
        raise SystemExit(1)

    auth_key = auth_key_path.read_bytes()
    meta = load_json(meta_path)
    log.info(
        "Собираем .session: dc=%s user_id=%s kurigram=%s",
        meta["dc_id"],
        meta["user_id"],
        meta.get("pyrogram_version"),
    )

    target = env.state_dir / "export_pyrogram.session"
    build_session_file(target, auth_key=auth_key, meta=meta)
    log.info("файл собран: %s (%d байт)", target.name, target.stat().st_size)

    verified = await verify(target, env=env, expected_user_id=int(meta["user_id"]))
    log.info(
        "verify: user_id=%s dc=%s match=%s",
        verified["user_id"],
        verified["dc_id"],
        verified["match_expected"],
    )

    save_json(
        env.state_dir / "export_pyrogram.json",
        {
            "session_version": SESSION_VERSION,
            "mtproto_library": "kurigram",
            "kurigram_version": meta.get("pyrogram_version"),
            "verified": verified,
        },
    )


if __name__ == "__main__":
    asyncio.run(run())
