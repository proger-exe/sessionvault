"""Export account sessions through the private bot."""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionvault.bot.callback_data import AccountCardCB, ConfirmCB, sign_confirm, verify_confirm
from sessionvault.bot.middlewares.auth import IdentityInfo
from sessionvault.config import Settings
from sessionvault.domain.exports import ExportService
from sessionvault.security.key_provider import KeyProvider
from sessionvault.storage.db.models import ExportFormat

router = Router(name="export")


@router.callback_query(AccountCardCB.filter(F.action == "export"))
async def show_export_confirm(
    query: CallbackQuery,
    callback_data: AccountCardCB,
    settings: Settings,
) -> None:
    confirm = sign_confirm(
        ConfirmCB(action="export", account_id=callback_data.account_id),
        settings,
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить .session",
                    callback_data=confirm.pack(),
                )
            ]
        ]
    )
    text = (
        "Экспорт создаст зашифрованный Pyrogram `.session` архив.\n"
        "Риск: личный чат с ботом станет местом доставки файла и ключа."
    )
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=keyboard)
    await query.answer()


@router.callback_query(ConfirmCB.filter(F.action == "export"))
async def confirm_export(
    query: CallbackQuery,
    callback_data: ConfirmCB,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if not verify_confirm(callback_data, settings):
        await query.answer("Подтверждение устарело.", show_alert=True)
        return
    if not isinstance(query.message, Message):
        await query.answer("Сообщение недоступно.", show_alert=True)
        return

    account_id = uuid.UUID(callback_data.account_id)
    await query.message.edit_text("Готовлю экспорт...")
    async with db_session_factory() as session:
        service = ExportService(
            session,
            settings=settings,
            key_provider=key_provider,
            redis=redis,
        )
        export = await service.create_export(
            account_id=account_id,
            format=ExportFormat.PYROGRAM_SESSION,
            operator_tg_id=identity.tg_user_id,
        )
        await session.commit()

    async with db_session_factory() as session:
        service = ExportService(
            session,
            settings=settings,
            key_provider=key_provider,
            redis=redis,
        )
        artifact = await service.build_pyrogram_export(
            export_id=export.id,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
        )
        await session.commit()

    caption = (
        "Зашифрованный `.session` экспорт.\n"
        f"Ключ архива: <code>{artifact.archive_key_b64}</code>\n"
        "Файл и ключ храните раздельно."
    )
    await query.message.answer_document(
        FSInputFile(artifact.path, filename=artifact.filename),
        caption=caption,
        parse_mode="HTML",
    )

    async with db_session_factory() as session:
        service = ExportService(
            session,
            settings=settings,
            key_provider=key_provider,
            redis=redis,
        )
        await service.mark_sent(
            export_id=artifact.export_id,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
        )
        await session.commit()
    await query.message.edit_text("Экспорт отправлен, временный файл удалён.")
    await query.answer()
