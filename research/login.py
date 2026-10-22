"""Этап 0.1 — вход по коду + 2FA через Kurigram (namespace pyrogram).

Подкоманды:

    uv run python login.py run            # send_code + polling кода из _state/code.txt
    uv run python login.py two-fa         # check_password из env TG_PASSWORD
    uv run python login.py dump           # извлекает auth key + meta

`run` держит соединение открытым между send_code и sign_in — Telegram-серверу
не нравится, когда `phone_code_hash` пытаются использовать после reconnect.
Как только код положен в `_state/code.txt`, скрипт делает sign_in и выходит.
Если требуется 2FA, скрипт печатает `need_2fa=True` и завершается с кодом 10 —
дальше руками через `two-fa`.
Пароль 2FA ни в файлы, ни в CLI не пишем — только env `TG_PASSWORD`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

from pyrogram import Client  # type: ignore[attr-defined]
from pyrogram.errors import (
    FloodWait,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberBanned,
    SessionPasswordNeeded,
)

from _common import Env, load_json, save_json, setup_logging

log = setup_logging()


def _client(env: Env, session_path: Path) -> Client:
    return Client(
        name=str(session_path),
        api_id=env.api_id,
        api_hash=env.api_hash,
        phone_number=env.test_phone,
        in_memory=False,
        no_updates=True,
    )


async def _wait_for_code(env: Env, *, timeout_s: int) -> str | None:
    code_path = env.state_dir / "code.txt"
    if code_path.exists():
        code_path.unlink()
    log.info("Жду код в %s (timeout %ss)…", code_path, timeout_s)
    for _ in range(timeout_s * 2):
        if code_path.exists():
            code = code_path.read_text().strip()
            code_path.unlink(missing_ok=True)
            if code:
                return code
        await asyncio.sleep(0.5)
    return None


async def cmd_run(env: Env, session_path: Path) -> int:
    login_state_path = env.state_dir / "login.json"
    client = _client(env, session_path)
    await client.connect()
    try:
        try:
            sent = await client.send_code(env.test_phone)
        except FloodWait as e:
            log.error("FLOOD_WAIT: подожди %s секунд.", e.value)
            return 2
        except PhoneNumberBanned:
            log.error("PHONE_NUMBER_BANNED.")
            return 3

        save_json(
            login_state_path,
            {
                "phone_code_hash": sent.phone_code_hash,
                "code_type": str(sent.type),
                "next_type": str(sent.next_type),
                "timeout": sent.timeout,
            },
        )
        log.info(
            "Код отправлен: type=%s, next_type=%s. Положи код в _state/code.txt",
            sent.type,
            sent.next_type,
        )

        code = await _wait_for_code(env, timeout_s=180)
        if not code:
            log.error("Не дождались кода — выхожу.")
            return 4

        try:
            await client.sign_in(env.test_phone, sent.phone_code_hash, code)
            log.info("sign_in ok.")
        except SessionPasswordNeeded:
            state = load_json(login_state_path)
            state["need_2fa"] = True
            save_json(login_state_path, state)
            log.info("Требуется 2FA. Запусти: TG_PASSWORD=... uv run python login.py two-fa")
            return 10
        except PhoneCodeInvalid:
            log.error("PHONE_CODE_INVALID — код не подошёл.")
            return 11
        except PhoneCodeExpired:
            log.error("PHONE_CODE_EXPIRED — код успел истечь.")
            return 12
        except FloodWait as e:
            log.error("FLOOD_WAIT: %s секунд.", e.value)
            return 2

        me = await client.get_me()
        log.info("Авторизованы: user_id=%s dc=%s is_bot=%s", me.id, me.dc_id, me.is_bot)
    finally:
        await client.disconnect()

    state = load_json(login_state_path)
    state["need_2fa"] = False
    state["signed_in"] = True
    save_json(login_state_path, state)
    return 0


async def cmd_two_fa(env: Env, session_path: Path) -> int:
    password = os.environ.get("TG_PASSWORD")
    if not password:
        log.error("Нет TG_PASSWORD в окружении.")
        return 1

    client = _client(env, session_path)
    await client.connect()
    try:
        try:
            await client.check_password(password)
            log.info("2FA ok.")
        except FloodWait as e:
            log.error("FLOOD_WAIT: %s секунд.", e.value)
            return 2
        me = await client.get_me()
        log.info("Авторизованы: user_id=%s dc=%s is_bot=%s", me.id, me.dc_id, me.is_bot)
    finally:
        await client.disconnect()

    login_state_path = env.state_dir / "login.json"
    state = load_json(login_state_path) if login_state_path.exists() else {}
    state["signed_in"] = True
    state["need_2fa"] = False
    save_json(login_state_path, state)
    return 0


async def cmd_dump(env: Env, session_path: Path) -> int:
    session_file = session_path.with_suffix(".session")
    if not session_file.exists():
        log.error("Нет %s.", session_file)
        return 1

    conn = sqlite3.connect(session_file)
    try:
        row = conn.execute(
            "SELECT dc_id, api_id, test_mode, auth_key, user_id, is_bot FROM sessions LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        log.error("Таблица sessions пустая.")
        return 2

    dc_id, api_id, test_mode, auth_key, user_id, is_bot = row
    (env.state_dir / "owner.auth_key.bin").write_bytes(auth_key)
    save_json(
        env.state_dir / "owner.meta.json",
        {
            "dc_id": dc_id,
            "api_id": api_id,
            "test_mode": bool(test_mode),
            "user_id": user_id,
            "is_bot": bool(is_bot),
            "pyrogram_version": _pyrogram_version(),
        },
    )
    log.info(
        "auth key + meta записаны: owner.auth_key.bin (%d байт), owner.meta.json",
        len(auth_key),
    )
    return 0


def _pyrogram_version() -> str:
    try:
        import pyrogram
        return pyrogram.__version__
    except Exception:  # noqa: BLE001
        return "unknown"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("two-fa")
    sub.add_parser("dump")
    return p.parse_args()


async def main() -> int:
    env = Env.load()
    session_path = env.state_dir / "owner"
    args = parse_args()
    match args.cmd:
        case "run":
            return await cmd_run(env, session_path)
        case "two-fa":
            return await cmd_two_fa(env, session_path)
        case "dump":
            return await cmd_dump(env, session_path)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
