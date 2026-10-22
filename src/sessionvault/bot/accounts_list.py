"""Пагинированный список аккаунтов.

Открывается из главного меню (`MenuCB(section='accounts')`) или командой
`/accounts`. По одному элементу на страницу список показывает первые 8
записей, дальше — inline-кнопки «◀» / «▶».
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionvault.bot.callback_data import (
    AccountsPageCB,
    MenuCB,
    is_valid_uuid,
)
from sessionvault.bot.middlewares.auth import IdentityInfo
from sessionvault.domain.accounts import AccountsService
from sessionvault.storage.db.models import TelegramAccount

router = Router(name="accounts_list")

_PAGE_SIZE = 8


def _format_row(account: TelegramAccount) -> str:
    name = " ".join(x for x in (account.first_name, account.last_name) if x) or "—"
    username = f" @{account.username}" if account.username else ""
    phone = f"…{account.phone_last4}"
    status_marker = {
        "active": "🟢",
        "pending": "⏳",
        "archived": "📦",
    }.get(account.status.value, "•")
    return f"{status_marker} {name}{username} ({phone})"


def _keyboard(
    accounts: list[TelegramAccount], *, page: int, total: int
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for acc in accounts:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_format_row(acc),
                    callback_data=AccountsPageCB(
                        action="open", page=page, account_id=str(acc.id)
                    ).pack(),
                )
            ]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀",
                callback_data=AccountsPageCB(action="page", page=page - 1).pack(),
            )
        )
    if (page + 1) * _PAGE_SIZE < total:
        nav.append(
            InlineKeyboardButton(
                text="▶",
                callback_data=AccountsPageCB(action="page", page=page + 1).pack(),
            )
        )
    if nav:
        rows.append(nav)

    rows.append(
        [InlineKeyboardButton(text="⌂ Меню", callback_data=MenuCB(section="root").pack())]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render(
    *,
    page: int,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, InlineKeyboardMarkup]:
    async with db_session_factory() as session:
        accounts = AccountsService(session, key_provider=None)
        rows, total = await accounts.list_page(offset=page * _PAGE_SIZE, limit=_PAGE_SIZE)
    if not rows:
        text = "Пока ни одного аккаунта.\nДобавь первый через «➕ Добавить аккаунт»."
    else:
        text = f"Аккаунты ({total}):\n"
    return text, _keyboard(rows, page=page, total=total)


@router.message(Command("accounts"))
async def cmd_accounts(
    message: Message,
    identity: IdentityInfo,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    text, keyboard = await _render(page=0, db_session_factory=db_session_factory)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(MenuCB.filter(F.section == "accounts"))
async def open_from_menu(
    query: CallbackQuery,
    identity: IdentityInfo,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert query.message is not None
    text, keyboard = await _render(page=0, db_session_factory=db_session_factory)
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=keyboard)
    await query.answer()


@router.callback_query(AccountsPageCB.filter(F.action == "page"))
async def page_change(
    query: CallbackQuery,
    callback_data: AccountsPageCB,
    identity: IdentityInfo,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert query.message is not None
    text, keyboard = await _render(page=callback_data.page, db_session_factory=db_session_factory)
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=keyboard)
    await query.answer()


@router.callback_query(AccountsPageCB.filter(F.action == "open"))
async def open_card(
    query: CallbackQuery,
    callback_data: AccountsPageCB,
    identity: IdentityInfo,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if not is_valid_uuid(callback_data.account_id):
        await query.answer("Некорректный ID.", show_alert=True)
        return
    # Отправку карточки берёт на себя account_card router — редиректим через
    # AccountCardCB.
    assert query.message is not None
    from sessionvault.bot.account_card import render_card

    text, keyboard = await render_card(
        account_id_str=callback_data.account_id,
        db_session_factory=db_session_factory,
    )
    if text is None:
        await query.answer("Аккаунт не найден.", show_alert=True)
        return
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await query.answer()
