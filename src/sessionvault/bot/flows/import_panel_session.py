"""FSM for importing panel-style `.session` + `.json` pairs."""

from __future__ import annotations

import logging
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
from sessionvault.domain.accounts import AccountsService
from sessionvault.domain.audit import AuditService
from sessionvault.mtproto.panel_session_import import (
    PanelSessionImportError,
    import_panel_session_pair,
)
from sessionvault.security.key_provider import KeyProvider
from sessionvault.storage.db.models import AuditResult

log = logging.getLogger(__name__)
router = Router(name="import_panel_session")


class ImportPanelSession(StatesGroup):
    waiting_session = State()
    waiting_json = State()


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Стоп", callback_data="import_panel_session:cancel")]]
    )


@router.message(Command("import_panel_session"))
@router.callback_query(MenuCB.filter(F.section == "import_panel_session"))
async def start_import(event: Message | CallbackQuery, state: FSMContext) -> None:
    await _cleanup_state_file(state)
    await state.clear()
    await state.set_state(ImportPanelSession.waiting_session)
    await state.update_data(imported_count=0)
    text = (
        "Пачечный импорт <code>.session</code> + <code>.json</code>.\n"
        "Кидай пары по очереди: сначала <code>998...session</code>, потом <code>998...json</code>.\n"
        "После импорта сразу жду следующую пару. Выход — кнопка Стоп."
    )
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await event.message.edit_text(text, parse_mode="HTML", reply_markup=_cancel_keyboard())
        await event.answer()
        return
    await event.answer(text, parse_mode="HTML", reply_markup=_cancel_keyboard())


@router.message(ImportPanelSession.waiting_session)
async def receive_session_file(message: Message, state: FSMContext) -> None:
    if message.document is None:
        await message.answer("Нужен файл-документ `.session`.", reply_markup=_cancel_keyboard())
        return
    filename = message.document.file_name or ""
    if not filename.endswith(".session"):
        await message.answer("Сначала нужен `.session` файл.", reply_markup=_cancel_keyboard())
        return
    if message.bot is None:
        await message.answer("Бот недоступен для скачивания файла.")
        return

    await _cleanup_state_file(state)
    tmp_path = Path(tempfile.gettempdir()) / f"sessionvault-panel-{uuid.uuid4()}.session"
    await message.bot.download(message.document, destination=tmp_path)
    await state.update_data(session_path=str(tmp_path), session_stem=Path(filename).stem)
    await state.set_state(ImportPanelSession.waiting_json)
    await message.answer(
        f"Жду matching JSON: <code>{Path(filename).stem}.json</code>.",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard(),
    )


@router.message(ImportPanelSession.waiting_json)
async def receive_json_file(
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    key_provider: KeyProvider,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if message.document is None:
        await message.answer("Нужен файл-документ `.json`.", reply_markup=_cancel_keyboard())
        return
    filename = message.document.file_name or ""
    if not filename.endswith(".json"):
        await message.answer("Теперь нужен `.json` файл.", reply_markup=_cancel_keyboard())
        return

    data = await state.get_data()
    session_path = Path(str(data.get("session_path", "")))
    session_stem = str(data.get("session_stem", ""))
    if not session_path.exists():
        await state.clear()
        await message.answer("Временный `.session` потерян. Начни импорт заново.")
        return
    if Path(filename).stem != session_stem:
        await message.answer(
            f"JSON не совпадает с session. Жду <code>{session_stem}.json</code>.",
            parse_mode="HTML",
            reply_markup=_cancel_keyboard(),
        )
        return
    if message.bot is None:
        await message.answer("Бот недоступен для скачивания файла.")
        return

    json_path = Path(tempfile.gettempdir()) / f"sessionvault-panel-{uuid.uuid4()}.json"
    try:
        await message.bot.download(message.document, destination=json_path)
        imported = import_panel_session_pair(session_path, json_path)
        async with db_session_factory() as session:
            accounts = AccountsService(session, key_provider)
            saved = await accounts.save_after_login(
                phone_e164=imported.phone_e164,
                session_data=imported.session,
                operator_tg_id=identity.tg_user_id,
            )
            await AuditService(session).record_now(
                action="account.import_panel_session",
                request_id=f"import-panel-session-{saved.account_id}-{uuid.uuid4()}",
                result=AuditResult.OK,
                actor_tg_id=identity.tg_user_id,
                role=identity.role,
                account_id=saved.account_id,
                chat_id=message.chat.id,
                message_id=message.message_id,
                changed_fields=["session"],
            )
            await session.commit()
        imported_count = int(data.get("imported_count", 0)) + 1
        await state.update_data(imported_count=imported_count)
    except (PanelSessionImportError, ValueError) as exc:
        await _audit_failed(
            db_session_factory,
            identity=identity,
            message=message,
            error_code=str(exc),
        )
        await state.set_state(ImportPanelSession.waiting_session)
        await state.update_data(session_path=None, session_stem=None)
        await message.answer(
            f"Импорт отклонён: <code>{exc}</code>\nКидай следующую <code>.session</code> или жми Стоп.",
            parse_mode="HTML",
            reply_markup=_cancel_keyboard(),
        )
        return
    finally:
        session_path.unlink(missing_ok=True)
        json_path.unlink(missing_ok=True)

    await state.set_state(ImportPanelSession.waiting_session)
    await state.update_data(session_path=None, session_stem=None)
    await message.answer(
        f"Импортировано: <code>{imported_count}</code>. "
        f"Последний tg_user_id: <code>{imported.session.user_id}</code>.\n"
        "Кидай следующую <code>.session</code> или жми Стоп.",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard(),
    )


@router.callback_query(F.data == "import_panel_session:cancel")
async def cancel_import(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    imported_count = int(data.get("imported_count", 0))
    await _cleanup_state_file(state)
    await state.clear()
    if isinstance(query.message, Message):
        await query.message.edit_text(f"Импорт остановлен. Импортировано: <code>{imported_count}</code>.", parse_mode="HTML")
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
            action="account.import_panel_session",
            request_id=f"import-panel-session-fail-{identity.tg_user_id}-{uuid.uuid4()}",
            result=AuditResult.FAILED,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
            chat_id=message.chat.id,
            message_id=message.message_id,
            error_code=error_code,
        )
        await session.commit()


async def _cleanup_state_file(state: FSMContext) -> None:
    data = await state.get_data()
    session_path = data.get("session_path")
    if isinstance(session_path, str):
        Path(session_path).unlink(missing_ok=True)
