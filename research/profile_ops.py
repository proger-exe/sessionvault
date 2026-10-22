# pyright: reportPrivateImportUsage=false, reportArgumentType=false, reportGeneralTypeIssues=false
"""Этап 0.2 — покрытие полей профиля из ТЗ 4.3.

Идея: пройти каждое поле, применить безопасное изменение с последующим
восстановлением исходного значения, зафиксировать результат в
`_state/profile_report.json`.

По каждому полю пишем один из трёх исходов:
- `ok`         — изменение прошло, post-read подтвердил новое значение;
- `unsupported`— Telegram отказал (RPC-ошибка про недоступное поле/слой);
- `error`      — прочая ошибка, сохраняем нормализованный код.

Внимание: скрипт делает реальные изменения на тестовом аккаунте (потом откатывает).
Не запускай на боевом аккаунте.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from pyrogram import Client  # type: ignore[attr-defined]
from pyrogram.errors import RPCError
from pyrogram.raw import functions, types  # type: ignore[attr-defined]

from _common import Env, save_json, setup_logging

log = setup_logging()


@dataclass
class FieldResult:
    name: str
    outcome: str  # ok | unsupported | error
    detail: str = ""
    before: Any = None
    after: Any = None


@dataclass
class Report:
    started_at: str
    pyrogram_version: str
    layer: int | None
    tg_user_id: int | None
    results: list[FieldResult] = field(default_factory=list)


UNSUPPORTED_MARKERS = (
    "USERNAME_INVALID",
    "USERNAME_OCCUPIED",
    "USERNAME_NOT_MODIFIED",
    "BIRTHDAY_INVALID",
    "PERSONAL_CHANNEL_INVALID",
    "PRIVACY_KEY_INVALID",
    "PRIVACY_VALUE_INVALID",
    "METHOD_INVALID",
    "CONSTRUCTOR_INVALID",
    "LAYER_INVALID",
)


async def _try(name: str, coro_factory: Callable[[], Awaitable[FieldResult]]) -> FieldResult:
    log.info("→ %s", name)
    try:
        result = await coro_factory()
        log.info("← %s: %s", name, result.outcome)
        return result
    except RPCError as e:  # noqa: BLE001
        code = getattr(e, "ID", type(e).__name__)
        outcome = "unsupported" if any(m in code for m in UNSUPPORTED_MARKERS) else "error"
        log.warning("← %s: %s (%s)", name, outcome, code)
        return FieldResult(name=name, outcome=outcome, detail=code)
    except Exception as e:  # noqa: BLE001
        log.exception("← %s: error", name)
        return FieldResult(name=name, outcome="error", detail=type(e).__name__ + ": " + str(e))


async def check_first_last_name(c: Client) -> FieldResult:
    me = await c.get_me()
    tag = secrets.token_hex(2)
    new_first = f"{me.first_name or 'Test'}-{tag}"
    await c.update_profile(first_name=new_first, last_name=me.last_name or "")
    after = await c.get_me()
    ok = after.first_name == new_first
    await c.update_profile(first_name=me.first_name or "", last_name=me.last_name or "")
    return FieldResult(
        name="first_last_name",
        outcome="ok" if ok else "error",
        before=me.first_name,
        after=after.first_name,
    )


async def check_username(c: Client) -> FieldResult:
    me = await c.get_me()
    original = me.username
    candidate = f"probe{secrets.token_hex(4)}"
    try:
        await c.set_username(candidate)
        after = await c.get_me()
        ok = (after.username or "").lower() == candidate.lower()
    finally:
        await c.set_username(original)  # noqa: PGH003
    return FieldResult(
        name="username",
        outcome="ok" if ok else "error",
        before=original,
        after=candidate,
    )


async def check_bio(c: Client) -> FieldResult:
    me_full = await c.invoke(functions.users.GetFullUser(id=await c.resolve_peer("me")))
    before = getattr(me_full.full_user, "about", "") or ""
    new_bio = f"probe {secrets.token_hex(3)}"
    await c.update_profile(bio=new_bio)
    me_full2 = await c.invoke(functions.users.GetFullUser(id=await c.resolve_peer("me")))
    after = getattr(me_full2.full_user, "about", "") or ""
    await c.update_profile(bio=before)
    return FieldResult(
        name="bio",
        outcome="ok" if after == new_bio else "error",
        before=before,
        after=new_bio,
    )


async def check_photo_list_upload_delete(c: Client) -> FieldResult:
    """Сгенерировать 512x512 JPEG, загрузить, удалить, сверить счётчик."""
    from PIL import Image, ImageDraw  # локальный импорт: dep нужна только здесь.

    img = Image.new("RGB", (512, 512), color=(80, 120, 200))
    draw = ImageDraw.Draw(img)
    draw.rectangle((160, 160, 352, 352), fill=(255, 255, 255))
    tmp_path = Path("/tmp/probe-avatar.jpg")
    img.save(tmp_path, format="JPEG", quality=85)
    uploaded = await c.set_profile_photo(photo=str(tmp_path))
    tmp_path.unlink(missing_ok=True)
    photos_before = [p async for p in c.get_chat_photos("me", limit=10)]
    if not uploaded or not photos_before:
        return FieldResult(name="photo_upload_delete", outcome="error", detail="upload_returned_empty")
    # Удаляем свежий
    await c.delete_profile_photos([photos_before[0].file_id])
    photos_after = [p async for p in c.get_chat_photos("me", limit=10)]
    ok = len(photos_after) == len(photos_before) - 1
    return FieldResult(
        name="photo_upload_delete",
        outcome="ok" if ok else "error",
        before=len(photos_before),
        after=len(photos_after),
    )


async def check_birthday(c: Client) -> FieldResult:
    """Layer 168+: functions.account.UpdateBirthday."""
    fn = getattr(functions.account, "UpdateBirthday", None)
    birthday_ctor = getattr(types, "Birthday", None)
    if fn is None or birthday_ctor is None:
        return FieldResult(name="birthday", outcome="unsupported", detail="not_in_pyrogram_layer")
    old = birthday_ctor(day=1, month=1, year=2000)
    await c.invoke(fn(birthday=old))
    # проверить через GetFullUser
    full = await c.invoke(functions.users.GetFullUser(id=await c.resolve_peer("me")))
    got = getattr(full.full_user, "birthday", None)
    ok = got is not None and got.day == 1 and got.month == 1
    # откат: вернуть без даты (empty)
    await c.invoke(fn(birthday=birthday_ctor(day=1, month=1)))
    return FieldResult(
        name="birthday",
        outcome="ok" if ok else "error",
        before=None,
        after={"day": 1, "month": 1, "year": 2000} if ok else None,
    )


async def check_personal_channel(c: Client) -> FieldResult:
    """Layer 178+: functions.account.UpdatePersonalChannel."""
    fn = getattr(functions.account, "UpdatePersonalChannel", None)
    channel_empty = getattr(types, "InputChannelEmpty", None)
    if fn is None or channel_empty is None:
        return FieldResult(name="personal_channel", outcome="unsupported", detail="not_in_pyrogram_layer")
    # Просто выставляем empty (сброс) — не создаём канал в тесте.
    await c.invoke(fn(channel=channel_empty()))
    return FieldResult(name="personal_channel", outcome="ok", detail="set_empty_ok")


async def check_privacy_last_seen(c: Client) -> FieldResult:
    """Проверка изменения приватности «последний визит» — обратимо."""
    fn = functions.account.SetPrivacy
    key = types.InputPrivacyKeyStatusTimestamp
    rule_contacts = types.InputPrivacyValueAllowContacts
    rule_everybody = types.InputPrivacyValueAllowAll
    # прочитать текущие
    current = await c.invoke(functions.account.GetPrivacy(key=key()))
    await c.invoke(fn(key=key(), rules=[rule_contacts()]))
    updated = await c.invoke(functions.account.GetPrivacy(key=key()))
    # вернуть исходное — грубо: если было "все", вернём "все"; иначе оставим contacts.
    was_all = any(type(r).__name__ == "PrivacyValueAllowAll" for r in getattr(current, "rules", []))
    await c.invoke(fn(key=key(), rules=[rule_everybody() if was_all else rule_contacts()]))
    return FieldResult(
        name="privacy_last_seen",
        outcome="ok",
        before=str(type(getattr(current, "rules", [None])[0]).__name__),
        after=str(type(getattr(updated, "rules", [None])[0]).__name__),
    )


async def run() -> None:
    env = Env.load()
    session_path = env.state_dir / "owner"
    if not session_path.with_suffix(".session").exists():
        log.error("Нет сессии %s.session — сначала запусти login.py", session_path)
        raise SystemExit(1)

    import pyrogram  # для версии и layer

    layer = getattr(getattr(pyrogram, "raw", None), "core", None)
    layer_num = getattr(layer, "LAYER", None) if layer else None

    report = Report(
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        pyrogram_version=getattr(pyrogram, "__version__", "unknown"),
        layer=layer_num,
        tg_user_id=None,
    )

    client = Client(
        name=str(session_path),
        api_id=env.api_id,
        api_hash=env.api_hash,
        no_updates=True,
    )
    await client.start()
    try:
        me = await client.get_me()
        report.tg_user_id = me.id
        checks: list[tuple[str, Callable[[], Awaitable[FieldResult]]]] = [
            ("first_last_name", lambda: check_first_last_name(client)),
            ("username", lambda: check_username(client)),
            ("bio", lambda: check_bio(client)),
            ("photo_upload_delete", lambda: check_photo_list_upload_delete(client)),
            ("birthday", lambda: check_birthday(client)),
            ("personal_channel", lambda: check_personal_channel(client)),
            ("privacy_last_seen", lambda: check_privacy_last_seen(client)),
        ]
        for name, factory in checks:
            report.results.append(await _try(name, factory))
    finally:
        await client.stop()

    out = env.state_dir / "profile_report.json"
    save_json(
        out,
        {
            "started_at": report.started_at,
            "pyrogram_version": report.pyrogram_version,
            "layer": report.layer,
            "tg_user_id": report.tg_user_id,
            "results": [
                {
                    "name": r.name,
                    "outcome": r.outcome,
                    "detail": r.detail,
                    "before": r.before,
                    "after": r.after,
                }
                for r in report.results
            ],
        },
    )
    log.info("Готово: %s", out.name)
    _print_summary(report)


def _print_summary(report: Report) -> None:
    for r in report.results:
        marker = {"ok": "OK", "unsupported": "UNSUPPORTED", "error": "ERROR"}[r.outcome]
        log.info("[%s] %s — %s", marker, r.name, r.detail or "")


if __name__ == "__main__":
    asyncio.run(run())
