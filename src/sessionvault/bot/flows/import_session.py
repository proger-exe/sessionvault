"""FSM for importing Pyrogram/Kurigram `.session` files."""

from __future__ import annotations

import logging
import re
import tempfile
import uuid
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionvault.bot.callback_data import MenuCB
from sessionvault.bot.middlewares.auth import IdentityInfo
from sessionvault.config import Settings
from sessionvault.domain.accounts import AccountsService
from sessionvault.domain.audit import AuditService
from sessionvault.mtproto.session_import import SessionImportError, import_pyrogram_session
from sessionvault.security.key_provider import KeyProvider
from sessionvault.storage.db.models import AuditResult

log = logging.getLogger(__name__)
router = Router(name="import_session")

_PHONE_RE = re.compile(r"^\+\d{7,15}$")


class ImportSession(StatesGroup):
    waiting_phone = State()
    waiting_file = State()


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="import_session:cancel")]]
    )


@router.message(Command("import_session"))
@router.callback_query(MenuCB.filter(F.section == "import_session"))
async def start_import(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ImportSession.waiting_phone)
    text = (
        "Импорт Pyrogram/Kurigram `.session`.\n"
        "Сначала пришли номер телефона аккаунта в E.164, например <code>+56933617390</code>."
    )
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await event.message.edit_text(text, parse_mode="HTML", reply_markup=_cancel_keyboard())
        await event.answer()
        return
    await event.answer(text, parse_mode="HTML", reply_markup=_cancel_keyboard())


@router.message(ImportSession.waiting_phone)
async def receive_phone(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    await _safe_delete(message)
    if not _PHONE_RE.match(phone):
        await message.answer(
            "Формат не подходит. Нужен E.164, например <code>+56933617390</code>.",
            parse_mode="HTML",
            reply_markup=_cancel_keyboard(),
        )
        return
    await state.update_data(phone=phone)
    await state.set_state(ImportSession.waiting_file)
    await message.answer(
        "Теперь пришли файл Pyrogram/Kurigram <code>.session</code> документом.",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard(),
    )


@router.message(ImportSession.waiting_file)
async def receive_session_file(
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if message.document is None:
        await message.answer("Нужен файл-документ `.session`.", reply_markup=_cancel_keyboard())
        return
    filename = message.document.file_name or ""
    if not filename.endswith(".session"):
        await message.answer("Поддерживаю только файлы с расширением `.session`.")
        return

    data = await state.get_data()
    phone = str(data["phone"])
    tmp_path = Path(tempfile.gettempdir()) / f"sessionvault-import-{uuid.uuid4()}.session"
    try:
        if message.bot is None:
            raise SessionImportError("BOT_UNAVAILABLE")
        await message.bot.download(message.document, destination=tmp_path)
        imported = await import_pyrogram_session(
            tmp_path,
            api_id=settings.pyrogram_api_id,
            api_hash=settings.pyrogram_api_hash.get_secret_value(),
        )
        async with db_session_factory() as session:
            accounts = AccountsService(session, key_provider)
            saved = await accounts.save_after_login(
                phone_e164=phone,
                session_data=imported,
                operator_tg_id=identity.tg_user_id,
            )
            await AuditService(session).record_now(
                action="account.import_session",
                request_id=f"import-session-{saved.account_id}-{uuid.uuid4()}",
                result=AuditResult.OK,
                actor_tg_id=identity.tg_user_id,
                role=identity.role,
                account_id=saved.account_id,
                chat_id=message.chat.id,
                message_id=message.message_id,
                changed_fields=["session"],
            )
            await session.commit()
    except SessionImportError as exc:
        await _audit_failed(
            db_session_factory,
            identity=identity,
            message=message,
            error_code=str(exc),
        )
        await message.answer(f"Импорт отклонён: <code>{exc}</code>", parse_mode="HTML")
        return
    finally:
        tmp_path.unlink(missing_ok=True)

    await state.clear()
    await message.answer(
        f"Session импортирована. tg_user_id: <code>{imported.user_id}</code>",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "import_session:cancel")
async def cancel_import(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(query.message, Message):
        await query.message.edit_text("Импорт отменён.")
    await query.answer()


async def _audit_failed(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    identity: IdentityInfo,
    message: Message,
    error_code: str,
) -> None:
    async with db_session_factory() as session:
        await AuditService(session).record_now(
            action="account.import_session",
            request_id=f"import-session-fail-{identity.tg_user_id}-{uuid.uuid4()}",
            result=AuditResult.FAILED,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
            chat_id=message.chat.id,
            message_id=message.message_id,
            error_code=error_code,
        )
        await session.commit()


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        log.debug("не удалось удалить сообщение с номером при import_session")
