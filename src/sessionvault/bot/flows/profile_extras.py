"""FSM for extra profile fields: avatar upload, birthday, personal channel, profile audio."""

from __future__ import annotations

import asyncio
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionvault.bot.callback_data import AccountCardCB, AccountsPageCB, ProfileCB
from sessionvault.bot.middlewares.auth import IdentityInfo
from sessionvault.config import Settings
from sessionvault.domain.profile import ProfileService, format_op_result
from sessionvault.security.key_provider import KeyProvider
from sessionvault.storage.db.models import ProfileOperation

router = Router(name="profile_extras")

_BIRTHDAY_RE = re.compile(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?$")

# ponytail: per-chat in-memory Lock serialises bursts (album uploads) so
# concurrent handlers don't fight over the Redis AccountLock; scope up to a
# distributed queue only if we run >1 bot process.
_bulk_upload_locks: dict[int, asyncio.Lock] = {}


def _bulk_lock(chat_id: int) -> asyncio.Lock:
    lock = _bulk_upload_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _bulk_upload_locks[chat_id] = lock
    return lock


class EditExtras(StatesGroup):
    waiting_photo = State()
    waiting_birthday = State()
    waiting_channel = State()
    waiting_audio = State()


def _extras_keyboard(account_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖼 Загрузить аватарку", callback_data=ProfileCB(action="photo_upload", account_id=account_id).pack())],
            [InlineKeyboardButton(text="📸 Мои аватарки", callback_data=ProfileCB(action="photos_list", account_id=account_id).pack())],
            [InlineKeyboardButton(text="🎵 Загрузить трек", callback_data=ProfileCB(action="audio_upload", account_id=account_id).pack())],
            [InlineKeyboardButton(text="🎂 День рождения", callback_data=ProfileCB(action="birthday", account_id=account_id).pack())],
            [InlineKeyboardButton(text="📣 Личный канал", callback_data=ProfileCB(action="personal_channel", account_id=account_id).pack())],
            [InlineKeyboardButton(text="◀ К карточке", callback_data=AccountsPageCB(action="open", page=0, account_id=account_id).pack())],
        ]
    )


def _cancel_extras_keyboard(account_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="◀ Назад",
                callback_data=ProfileCB(action="cancel_extras", account_id=account_id).pack(),
            )
        ]]
    )


def _more_or_back_keyboard(account_id: str, kind: str) -> InlineKeyboardMarkup:
    """After a successful upload — hint to add more or bail out."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить ещё",
                    callback_data=ProfileCB(action=f"{kind}_upload", account_id=account_id).pack(),
                ),
                InlineKeyboardButton(
                    text="◀ Вернуться назад",
                    callback_data=ProfileCB(action="cancel_extras", account_id=account_id).pack(),
                ),
            ]
        ]
    )


@router.callback_query(AccountCardCB.filter(F.action == "extras"))
async def open_extras_menu(query: CallbackQuery, callback_data: AccountCardCB, state: FSMContext) -> None:
    await state.clear()
    text = (
        "Расширенные поля профиля.\n"
        "Часть операций может вернуть <code>unsupported</code>, если Telegram не позволяет их в этом аккаунте."
    )
    if isinstance(query.message, Message):
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=_extras_keyboard(callback_data.account_id))
    await query.answer()


@router.callback_query(ProfileCB.filter(F.action == "cancel_extras"))
async def cancel_extras(query: CallbackQuery, callback_data: ProfileCB, state: FSMContext) -> None:
    await state.clear()
    if isinstance(query.message, Message):
        await query.message.edit_text(
            "Расширенные поля профиля.\n"
            "Часть операций может вернуть <code>unsupported</code>, если Telegram не позволяет их в этом аккаунте.",
            parse_mode="HTML",
            reply_markup=_extras_keyboard(callback_data.account_id),
        )
    await query.answer()


@router.callback_query(ProfileCB.filter(F.action == "photos_list"))
async def show_photos(
    query: CallbackQuery,
    callback_data: ProfileCB,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.UUID(callback_data.account_id)
    await query.answer("Читаю…")
    async with db_session_factory() as session:
        service = ProfileService(session, settings=settings, key_provider=key_provider, redis=redis)
        photos = await service.list_photos(account_id=account_id)
    if not photos:
        if isinstance(query.message, Message):
            await query.message.edit_text(
                "Аватарок нет.",
                reply_markup=_extras_keyboard(callback_data.account_id),
            )
        return
    lines = ["Твои аватарки (свежие сверху):"]
    rows: list[list[InlineKeyboardButton]] = []
    for p in photos:
        date_s = p["date"].strftime("%Y-%m-%d") if p["date"] else "—"
        lines.append(f"<code>{p['index'] + 1}</code>. {date_s}")
        rows.append([
            InlineKeyboardButton(
                text=f"🗑 {p['index'] + 1}",
                callback_data=ProfileCB(action=f"photo_del:{p['index']}", account_id=callback_data.account_id).pack(),
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="◀ Назад",
            callback_data=AccountCardCB(action="extras", account_id=callback_data.account_id).pack(),
        )
    ])
    if isinstance(query.message, Message):
        await query.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(ProfileCB.filter(F.action.startswith("photo_del:")))
async def delete_photo(
    query: CallbackQuery,
    callback_data: ProfileCB,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, _, raw_idx = callback_data.action.partition(":")
    if not raw_idx.isdigit():
        await query.answer("Некорректный индекс.", show_alert=True)
        return
    account_id = uuid.UUID(callback_data.account_id)
    applied = await _run(
        operation_type="profile.photo.delete_by_index",
        payload={"index": int(raw_idx)},
        account_id=account_id,
        idempotency_key=f"bot:{query.id}",
        identity=identity,
        settings=settings,
        key_provider=key_provider,
        redis=redis,
        db_session_factory=db_session_factory,
    )
    await query.answer(format_op_result(applied).replace("<code>", "").replace("</code>", ""), show_alert=True)
    # re-render list
    await show_photos(
        query=query,
        callback_data=ProfileCB(action="photos_list", account_id=callback_data.account_id),
        settings=settings,
        key_provider=key_provider,
        redis=redis,
        db_session_factory=db_session_factory,
    )


@router.callback_query(ProfileCB.filter(F.action == "photo_upload"))
async def ask_photo(query: CallbackQuery, callback_data: ProfileCB, state: FSMContext) -> None:
    await state.set_state(EditExtras.waiting_photo)
    await state.update_data(account_id=callback_data.account_id)
    if isinstance(query.message, Message):
        await query.message.edit_text(
            "Пришли фото для новой аватарки — по одной или альбомом (сжатое или файлом-картинкой).",
            reply_markup=_cancel_extras_keyboard(callback_data.account_id),
        )
    await query.answer()


@router.callback_query(ProfileCB.filter(F.action == "audio_upload"))
async def ask_audio(query: CallbackQuery, callback_data: ProfileCB, state: FSMContext) -> None:
    await state.set_state(EditExtras.waiting_audio)
    await state.update_data(account_id=callback_data.account_id)
    if isinstance(query.message, Message):
        await query.message.edit_text(
            "Пришли трек для профиля — аудио-сообщением или файлом. Можно отправлять по одному подряд.",
            reply_markup=_cancel_extras_keyboard(callback_data.account_id),
        )
    await query.answer()


@router.callback_query(ProfileCB.filter(F.action == "birthday"))
async def ask_birthday(query: CallbackQuery, callback_data: ProfileCB, state: FSMContext) -> None:
    await state.set_state(EditExtras.waiting_birthday)
    await state.update_data(account_id=callback_data.account_id)
    if isinstance(query.message, Message):
        await query.message.edit_text(
            "Пришли день рождения: <code>05.03.1990</code>, <code>05.03</code> (без года) или <code>-</code> чтобы очистить.",
            parse_mode="HTML",
            reply_markup=_cancel_extras_keyboard(callback_data.account_id),
        )
    await query.answer()


@router.callback_query(ProfileCB.filter(F.action == "personal_channel"))
async def ask_channel(query: CallbackQuery, callback_data: ProfileCB, state: FSMContext) -> None:
    await state.set_state(EditExtras.waiting_channel)
    await state.update_data(account_id=callback_data.account_id)
    if isinstance(query.message, Message):
        await query.message.edit_text(
            "Пришли <code>@username</code> канала, его числовой id или <code>-</code> чтобы отвязать.",
            parse_mode="HTML",
            reply_markup=_cancel_extras_keyboard(callback_data.account_id),
        )
    await query.answer()


@router.message(EditExtras.waiting_photo)
async def receive_photo(
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    file_id: str | None = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id
    if file_id is None or message.bot is None:
        await message.answer("Нужна картинка (фото или image-документ).")
        return

    data = await state.get_data()
    account_id_str = str(data["account_id"])
    account_id = uuid.UUID(account_id_str)
    tmp_path = Path(tempfile.gettempdir()) / f"sessionvault-avatar-{uuid.uuid4()}.jpg"
    async with _bulk_lock(message.chat.id):
        try:
            await message.bot.download(file_id, destination=tmp_path)
            applied = await _run(
                operation_type="profile.photo.upload",
                payload={"path": str(tmp_path)},
                account_id=account_id,
                idempotency_key=f"bot:{message.chat.id}:{message.message_id}",
                identity=identity,
                settings=settings,
                key_provider=key_provider,
                redis=redis,
                db_session_factory=db_session_factory,
            )
        finally:
            tmp_path.unlink(missing_ok=True)

    # keep waiting_photo state so the user can keep sending — album items
    # keep landing here; kb visible only when it's a single (non-album) send.
    kb = None if message.media_group_id else _more_or_back_keyboard(account_id_str, "photo")
    await message.answer(format_op_result(applied), parse_mode="HTML", reply_markup=kb)


@router.message(EditExtras.waiting_audio)
async def receive_audio(
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    file_id: str | None = None
    duration = 0
    performer: str | None = None
    title: str | None = None
    file_name: str | None = None
    if message.audio:
        file_id = message.audio.file_id
        duration = message.audio.duration or 0
        performer = message.audio.performer
        title = message.audio.title
        file_name = message.audio.file_name
    elif message.document and (message.document.mime_type or "").startswith("audio/"):
        file_id = message.document.file_id
        file_name = message.document.file_name
    if file_id is None or message.bot is None:
        await message.answer("Нужен аудиофайл (audio или audio-документ).")
        return

    data = await state.get_data()
    account_id_str = str(data["account_id"])
    account_id = uuid.UUID(account_id_str)
    suffix = Path(file_name).suffix if file_name else ".mp3"
    tmp_path = Path(tempfile.gettempdir()) / f"sessionvault-audio-{uuid.uuid4()}{suffix}"
    async with _bulk_lock(message.chat.id):
        try:
            await message.bot.download(file_id, destination=tmp_path)
            applied = await _run(
                operation_type="profile.audio.upload",
                payload={
                    "path": str(tmp_path),
                    "duration": duration,
                    "performer": performer,
                    "title": title,
                    "file_name": file_name,
                },
                account_id=account_id,
                idempotency_key=f"bot:{message.chat.id}:{message.message_id}",
                identity=identity,
                settings=settings,
                key_provider=key_provider,
                redis=redis,
                db_session_factory=db_session_factory,
            )
        finally:
            tmp_path.unlink(missing_ok=True)

    kb = None if message.media_group_id else _more_or_back_keyboard(account_id_str, "audio")
    await message.answer(format_op_result(applied), parse_mode="HTML", reply_markup=kb)


@router.message(EditExtras.waiting_birthday)
async def receive_birthday(
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    data = await state.get_data()
    account_id = uuid.UUID(str(data["account_id"]))
    raw = (message.text or "").strip()
    try:
        operation_type, payload = _parse_birthday(raw)
    except ValueError as exc:
        await message.answer(f"Не распарсил: <code>{exc}</code>. Пример: <code>05.03.1990</code>.", parse_mode="HTML")
        return

    applied = await _run(
        operation_type=operation_type,
        payload=payload,
        account_id=account_id,
        idempotency_key=f"bot:{message.chat.id}:{message.message_id}",
        identity=identity,
        settings=settings,
        key_provider=key_provider,
        redis=redis,
        db_session_factory=db_session_factory,
    )
    await state.clear()
    await message.answer(format_op_result(applied), parse_mode="HTML")


@router.message(EditExtras.waiting_channel)
async def receive_channel(
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    data = await state.get_data()
    account_id = uuid.UUID(str(data["account_id"]))
    raw = (message.text or "").strip()
    operation_type, payload = _parse_channel(raw)

    applied = await _run(
        operation_type=operation_type,
        payload=payload,
        account_id=account_id,
        idempotency_key=f"bot:{message.chat.id}:{message.message_id}",
        identity=identity,
        settings=settings,
        key_provider=key_provider,
        redis=redis,
        db_session_factory=db_session_factory,
    )
    await state.clear()
    await message.answer(format_op_result(applied), parse_mode="HTML")


def _parse_birthday(raw: str) -> tuple[str, dict[str, Any]]:
    if raw == "-":
        return "profile.birthday.clear", {}
    m = _BIRTHDAY_RE.match(raw)
    if not m:
        raise ValueError("BIRTHDAY_FORMAT")
    day, month, year_str = int(m.group(1)), int(m.group(2)), m.group(3)
    if not (1 <= day <= 31 and 1 <= month <= 12):
        raise ValueError("BIRTHDAY_RANGE")
    payload: dict[str, Any] = {"day": day, "month": month}
    if year_str:
        year = int(year_str)
        if year < 100:
            year += 1900 if year >= 50 else 2000
        payload["year"] = year
    return "profile.birthday.set", payload


def _parse_channel(raw: str) -> tuple[str, dict[str, Any]]:
    if raw == "-" or raw == "":
        return "profile.personal_channel.clear", {}
    if raw.lstrip("-").isdigit():
        return "profile.personal_channel.set", {"chat": int(raw)}
    return "profile.personal_channel.set", {"chat": raw.removeprefix("@")}


async def _run(
    *,
    operation_type: str,
    payload: dict[str, Any],
    account_id: uuid.UUID,
    idempotency_key: str,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> ProfileOperation:
    async with db_session_factory() as session:
        service = ProfileService(
            session,
            settings=settings,
            key_provider=key_provider,
            redis=redis,
        )
        op = await service.create_operation(
            account_id=account_id,
            operation_type=operation_type,
            payload=payload,
            operator_tg_id=identity.tg_user_id,
            idempotency_key=idempotency_key,
        )
        applied = await service.apply_operation(operation_id=op.id, role=identity.role)
        await session.commit()
        return applied
