# pyright: reportMissingImports=false, reportUnknownMemberType=false, reportAttributeAccessIssue=false
"""Этап 0.4 — конвертация Kurigram-сессии в tdata через opentele.

Kurigram использует namespace `pyrogram`, но opentele работает через Telethon.
Поэтому берём наш raw auth key + dc_id и собираем Telethon-`StringSession`,
дальше `opentele.tl.TelegramClient.ToTDesktop()` уже понимает всё как обычный
Telethon-клиент.

Проверка запуском TDesktop — руками через контейнер `tdata-runner/` (VNC).
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timezone
from pathlib import Path

from opentele.api import API, UseCurrentSession
from opentele.td import TDesktop
from opentele.tl import TelegramClient
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from _common import Env, load_json, save_json, setup_logging

log = setup_logging()

# Актуальные IP DC (Production) — Telethon требует, чтобы у session был явно
# заданный DC IP:port; сами адреса берём из Telethon dc_options.
# https://core.telegram.org/api/datacenter
_DC_IPS = {
    1: ("149.154.175.53", 443),
    2: ("149.154.167.51", 443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91", 443),
    5: ("91.108.56.130", 443),
}


def build_telethon_string_session(auth_key: bytes, dc_id: int) -> str:
    """Собираем StringSession c нашим auth key, чтобы Telethon-клиент был
    авторизован без нового login."""
    ip, port = _DC_IPS[dc_id]
    session = StringSession()
    session.set_dc(dc_id, ip, port)
    session.auth_key = AuthKey(auth_key)
    return session.save()


async def convert(session_string: str, tdata_dir: Path, *, api) -> dict:
    if tdata_dir.exists():
        shutil.rmtree(tdata_dir)
    tdata_dir.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(session=StringSession(session_string), api=api)
    await client.connect()
    try:
        is_auth = await client.is_user_authorized()
        if not is_auth:
            raise RuntimeError("Telethon-клиент не авторизован по нашему auth key")
        me = await client.get_me()
        log.info("Telethon видит self: id=%s, first_name=%s", me.id, me.first_name)
        tdesk: TDesktop = await client.ToTDesktop(flag=UseCurrentSession)
    finally:
        await client.disconnect()
    tdesk.SaveTData(str(tdata_dir))

    return {
        "tdata_dir": str(tdata_dir),
        "account_count": len(tdesk.accounts),
        "main_account_dc": tdesk.mainAccount.MainDcId if tdesk.mainAccount else None,
        "opentele_api_class": type(api).__name__,
        "app_version": api.app_version,
    }


async def run() -> None:
    env = Env.load()
    auth_key_path = env.state_dir / "owner.auth_key.bin"
    meta_path = env.state_dir / "owner.meta.json"
    if not (auth_key_path.exists() and meta_path.exists()):
        log.error("Нет auth key/meta — сначала login.py dump")
        raise SystemExit(1)

    auth_key = auth_key_path.read_bytes()
    meta = load_json(meta_path)
    dc_id = int(meta["dc_id"])

    api = API.TelegramDesktop.Generate(system="windows")
    log.info(
        "opentele API-профиль: %s, app_version=%s, system=%s",
        api.__class__.__name__,
        api.app_version,
        api.system_version,
    )

    session_string = build_telethon_string_session(auth_key, dc_id)
    log.info("Telethon StringSession собран (dc=%d)", dc_id)

    tdata_dir = env.state_dir / "tdata"
    info = await convert(session_string, tdata_dir, api=api)
    log.info(
        "tdata собран: %s (accounts=%s, dc=%s)",
        tdata_dir,
        info["account_count"],
        info["main_account_dc"],
    )

    try:
        import opentele

        opentele_version = getattr(opentele, "__version__", "unknown")
    except Exception:
        opentele_version = "unknown"

    save_json(
        env.state_dir / "export_tdata.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "opentele_version": opentele_version,
            "info": info,
        },
    )


if __name__ == "__main__":
    asyncio.run(run())
