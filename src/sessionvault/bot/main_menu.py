"""Главное меню и /start.

На старте Этапа 1 показывает роль и заглушки под будущие разделы. По мере
дозаполнения (accounts, admins, audit) кнопки становятся живыми.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sessionvault.bot.callback_data import MenuCB
from sessionvault.bot.middlewares.auth import IdentityInfo
from sessionvault.storage.db.models import UserRole

router = Router(name="main_menu")


def _menu(role: UserRole) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📋 Аккаунты", callback_data=MenuCB(section="accounts").pack())],
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data=MenuCB(section="add_account").pack())],
        [InlineKeyboardButton(text="⬆️ Импорт .session", callback_data=MenuCB(section="import_session").pack())],
        [InlineKeyboardButton(text="📦 Импорт session+json", callback_data=MenuCB(section="import_panel_session").pack())],
        [InlineKeyboardButton(text="📜 Журнал операций", callback_data=MenuCB(section="audit").pack())],
    ]
    if role == UserRole.OWNER:
        rows.append([InlineKeyboardButton(text="👥 Администраторы", callback_data=MenuCB(section="admins").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start"))
async def handle_start(message: Message, identity: IdentityInfo) -> None:
    role_label = "Owner" if identity.role == UserRole.OWNER else "Admin"
    await message.answer(
        f"SessionVault готов.\n"
        f"Роль: <b>{role_label}</b>\n"
        f"tg_user_id: <code>{identity.tg_user_id}</code>\n\n"
        "Выбирай раздел ниже.",
        reply_markup=_menu(identity.role),
        parse_mode="HTML",
    )


@router.message(Command("health"))
async def handle_health(message: Message, identity: IdentityInfo) -> None:
    if identity.role != UserRole.OWNER:
        await message.answer("Команда доступна только Owner.")
        return
    await message.answer("Служебная проверка пока пустая — заполним на Этапе 4.")


@router.callback_query(MenuCB.filter(F.section == "root"))
async def back_to_root(query: CallbackQuery, identity: IdentityInfo) -> None:
    assert query.message is not None
    role_label = "Owner" if identity.role == UserRole.OWNER else "Admin"
    text = (
        f"SessionVault готов.\nРоль: <b>{role_label}</b>\n"
        f"tg_user_id: <code>{identity.tg_user_id}</code>\n\nВыбирай раздел ниже."
    )
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=_menu(identity.role), parse_mode="HTML")
    await query.answer()
