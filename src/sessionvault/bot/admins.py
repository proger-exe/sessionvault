"""Управление администраторами (только Owner).

- список активных Admin;
- добавить нового по tg_user_id (FSM);
- деактивировать существующего (через ConfirmCB с HMAC-подписью).

Owner из `.env` — единственный, добавлять/удалять его нельзя. Попытка
делает audit.reject.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionvault.bot.callback_data import (
    AdminsCB,
    ConfirmCB,
    MenuCB,
    sign_confirm,
    verify_confirm,
)
from sessionvault.bot.middlewares.auth import IdentityInfo
from sessionvault.config import Settings, get_settings
from sessionvault.domain.admins import AdminService
from sessionvault.domain.audit import AuditService
from sessionvault.storage.db.models import AuditResult, User, UserRole

router = Router(name="admins")

_TG_USER_ID_RE = re.compile(r"^\d{5,15}$")


class AddAdmin(StatesGroup):
    waiting_tg_id = State()
    waiting_name = State()


def _list_keyboard(admins: Sequence[User], settings: Settings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for admin in admins:
        confirm = sign_confirm(
            ConfirmCB(action="admin_remove", account_id="", target_id=admin.tg_user_id),
            settings,
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{admin.display_name or 'без имени'} · {admin.tg_user_id}",
                    callback_data=AdminsCB(action="remove", tg_user_id=admin.tg_user_id).pack(),
                ),
                InlineKeyboardButton(
                    text="✖",
                    callback_data=confirm.pack(),
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data=AdminsCB(action="add").pack()),
            InlineKeyboardButton(text="⌂ Меню", callback_data=MenuCB(section="root").pack()),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _ensure_owner_or_reject(query: CallbackQuery, identity: IdentityInfo) -> bool:
    return identity.role == UserRole.OWNER


async def _render_list(
    *,
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> tuple[str, InlineKeyboardMarkup]:
    async with db_session_factory() as session:
        admins = await AdminService(session, settings).list_admins()
    if not admins:
        text = "Администраторов пока нет. Только Owner ходит в систему."
    else:
        text = f"Администраторов: <b>{len(admins)}</b>\nВыбери «✖», чтобы деактивировать."
    return text, _list_keyboard(admins, settings)


@router.callback_query(MenuCB.filter(F.section == "admins"))
async def open_from_menu(
    query: CallbackQuery,
    identity: IdentityInfo,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if not _ensure_owner_or_reject(query, identity):
        await query.answer("Только Owner может смотреть админов.", show_alert=True)
        return
    assert query.message is not None
    text, keyboard = await _render_list(
        db_session_factory=db_session_factory, settings=get_settings()
    )
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await query.answer()


@router.message(Command("admins"))
async def cmd_admins(
    message: Message,
    identity: IdentityInfo,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if identity.role != UserRole.OWNER:
        await message.answer("Только Owner может смотреть админов.")
        return
    text, keyboard = await _render_list(
        db_session_factory=db_session_factory, settings=get_settings()
    )
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(AdminsCB.filter(F.action == "add"))
async def start_add(
    query: CallbackQuery,
    identity: IdentityInfo,
    state: FSMContext,
) -> None:
    if not _ensure_owner_or_reject(query, identity):
        await query.answer("Только Owner.", show_alert=True)
        return
    assert query.message is not None
    await state.clear()
    await state.set_state(AddAdmin.waiting_tg_id)
    if isinstance(query.message, Message):
        await query.message.edit_text(
            "Пришли Telegram user ID нового администратора (число).\n"
            "Можно узнать через @userinfobot."
        )
    await query.answer()


@router.message(AddAdmin.waiting_tg_id)
async def receive_tg_id(
    message: Message,
    identity: IdentityInfo,
    state: FSMContext,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = (message.text or "").strip()
    if not _TG_USER_ID_RE.match(raw):
        await message.answer("Ожидаю Telegram user ID (только цифры).")
        return
    tg_user_id = int(raw)
    settings = get_settings()
    if tg_user_id == settings.owner_id:
        await message.answer("Это Owner — он уже владеет системой.")
        await state.clear()
        return
    await state.update_data(tg_user_id=tg_user_id)
    await state.set_state(AddAdmin.waiting_name)
    await message.answer("Пришли имя для отображения или '-' пропустить.")


@router.message(AddAdmin.waiting_name)
async def receive_name(
    message: Message,
    identity: IdentityInfo,
    state: FSMContext,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    data = await state.get_data()
    tg_user_id = int(data["tg_user_id"])
    raw = (message.text or "").strip()
    display_name = None if raw == "-" else raw[:128]

    async with db_session_factory() as session:
        admins = AdminService(session, get_settings())
        user = await admins.add_admin(
            tg_user_id=tg_user_id,
            display_name=display_name,
            added_by_tg_id=identity.tg_user_id,
        )
        audit = AuditService(session)
        await audit.record_now(
            action="admin.add",
            request_id=f"admin-add-{tg_user_id}-{uuid.uuid4()}",
            result=AuditResult.OK,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
            chat_id=message.chat.id,
            changed_fields=["role", "display_name"],
        )
        await session.commit()

    await state.clear()
    text, keyboard = await _render_list(
        db_session_factory=db_session_factory, settings=get_settings()
    )
    await message.answer(
        f"✔ Admin добавлен: <code>{user.tg_user_id}</code>\n\n" + text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@router.callback_query(ConfirmCB.filter(F.action == "admin_remove"))
async def confirm_remove(
    query: CallbackQuery,
    callback_data: ConfirmCB,
    identity: IdentityInfo,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if not _ensure_owner_or_reject(query, identity):
        await query.answer("Только Owner.", show_alert=True)
        return
    settings = get_settings()
    if not verify_confirm(callback_data, settings):
        await query.answer("Подпись просрочена, обнови список.", show_alert=True)
        return
    if callback_data.target_id == settings.owner_id:
        await query.answer("Owner нельзя удалять.", show_alert=True)
        return

    async with db_session_factory() as session:
        removed = await AdminService(session, settings).deactivate_admin(callback_data.target_id)
        audit = AuditService(session)
        await audit.record_now(
            action="admin.remove",
            request_id=f"admin-rm-{callback_data.target_id}-{uuid.uuid4()}",
            result=AuditResult.OK if removed else AuditResult.FAILED,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
            chat_id=query.message.chat.id if query.message else None,
            error_code=None if removed else "not_found",
        )
        await session.commit()

    assert query.message is not None
    text, keyboard = await _render_list(
        db_session_factory=db_session_factory, settings=settings
    )
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await query.answer("Готово." if removed else "Не найден.", show_alert=not removed)
