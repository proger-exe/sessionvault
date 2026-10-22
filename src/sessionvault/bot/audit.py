"""Просмотр аудита из бота (Owner и Admin).

MVP: пагинированный список последних `_PAGE_SIZE` событий с фильтром по
конкретному аккаунту (передаётся через AuditCB.account_id).
"""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionvault.bot.callback_data import AuditCB, MenuCB, is_valid_uuid
from sessionvault.bot.middlewares.auth import IdentityInfo
from sessionvault.storage.db.models import AuditEvent

router = Router(name="audit")

_PAGE_SIZE = 10


def _icon(result: str) -> str:
    return {"ok": "🟢", "rejected": "🚫", "failed": "🔴"}.get(result, "•")


async def _render(
    *,
    page: int,
    account_id: str,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, InlineKeyboardMarkup]:
    stmt = select(AuditEvent).order_by(AuditEvent.created_at.desc())
    if is_valid_uuid(account_id):
        stmt = stmt.where(AuditEvent.account_id == uuid.UUID(account_id))
    stmt = stmt.offset(page * _PAGE_SIZE).limit(_PAGE_SIZE + 1)  # +1 чтобы понять, есть ли следующая страница

    async with db_session_factory() as session:
        rows = list((await session.execute(stmt)).scalars())

    has_next = len(rows) > _PAGE_SIZE
    rows = rows[:_PAGE_SIZE]

    if not rows:
        text = "Пусто."
    else:
        lines = []
        for event in rows:
            when = event.created_at.strftime("%Y-%m-%d %H:%M")
            actor = event.actor_tg_id or "system"
            icon = _icon(event.result.value)
            error = f" · {event.error_code}" if event.error_code else ""
            lines.append(
                f"{icon} <code>{when}</code> · <b>{event.action}</b> · actor <code>{actor}</code>{error}"
            )
        header = "Журнал"
        if is_valid_uuid(account_id):
            header += f" (аккаунт <code>{account_id[:8]}…</code>)"
        text = f"{header}:\n" + "\n".join(lines)

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀",
                callback_data=AuditCB(action="page", page=page - 1, account_id=account_id).pack(),
            )
        )
    if has_next:
        nav.append(
            InlineKeyboardButton(
                text="▶",
                callback_data=AuditCB(action="page", page=page + 1, account_id=account_id).pack(),
            )
        )

    rows_kb: list[list[InlineKeyboardButton]] = []
    if nav:
        rows_kb.append(nav)
    rows_kb.append(
        [InlineKeyboardButton(text="⌂ Меню", callback_data=MenuCB(section="root").pack())]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows_kb)


@router.message(Command("audit"))
async def cmd_audit(
    message: Message,
    identity: IdentityInfo,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    text, keyboard = await _render(
        page=0, account_id="", db_session_factory=db_session_factory
    )
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(MenuCB.filter(F.section == "audit"))
async def open_from_menu(
    query: CallbackQuery,
    identity: IdentityInfo,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert query.message is not None
    text, keyboard = await _render(
        page=0, account_id="", db_session_factory=db_session_factory
    )
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await query.answer()


@router.callback_query(AuditCB.filter(F.action == "page"))
async def page(
    query: CallbackQuery,
    callback_data: AuditCB,
    identity: IdentityInfo,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert query.message is not None
    text, keyboard = await _render(
        page=callback_data.page,
        account_id=callback_data.account_id,
        db_session_factory=db_session_factory,
    )
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await query.answer()
