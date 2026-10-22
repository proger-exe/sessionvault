"""FSM for MVP profile edits."""

from __future__ import annotations

import uuid

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

router = Router(name="edit_profile")


class EditProfile(StatesGroup):
    waiting_value = State()


def _keyboard(account_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Имя и фамилия", callback_data=ProfileCB(action="name", account_id=account_id).pack())],
            [InlineKeyboardButton(text="Username", callback_data=ProfileCB(action="username", account_id=account_id).pack())],
            [InlineKeyboardButton(text="Bio", callback_data=ProfileCB(action="bio", account_id=account_id).pack())],
            [
                InlineKeyboardButton(
                    text="Удалить аватарки",
                    callback_data=ProfileCB(action="photo_delete_all", account_id=account_id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀ К карточке",
                    callback_data=AccountsPageCB(action="open", page=0, account_id=account_id).pack(),
                )
            ],
        ]
    )


def _cancel_keyboard(account_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="◀ Назад",
                callback_data=ProfileCB(action="cancel_edit", account_id=account_id).pack(),
            )
        ]]
    )


@router.callback_query(AccountCardCB.filter(F.action == "profile"))
async def open_profile_menu(query: CallbackQuery, callback_data: AccountCardCB, state: FSMContext) -> None:
    await state.clear()
    text = "Выбери поле профиля для изменения."
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=_keyboard(callback_data.account_id))
    await query.answer()


@router.callback_query(ProfileCB.filter(F.action == "cancel_edit"))
async def cancel_edit(query: CallbackQuery, callback_data: ProfileCB, state: FSMContext) -> None:
    await state.clear()
    if isinstance(query.message, Message):
        await query.message.edit_text(
            "Выбери поле профиля для изменения.",
            reply_markup=_keyboard(callback_data.account_id),
        )
    await query.answer()


@router.callback_query(ProfileCB.filter(F.action.in_({"name", "username", "bio"})))
async def start_text_edit(
    query: CallbackQuery,
    callback_data: ProfileCB,
    state: FSMContext,
) -> None:
    prompts = {
        "name": "Пришли имя и фамилию через пробел. Для пустой фамилии пришли только имя.",
        "username": "Пришли новый username без @ или `-`, чтобы удалить.",
        "bio": "Пришли новый bio или `-`, чтобы очистить.",
    }
    await state.set_state(EditProfile.waiting_value)
    await state.update_data(account_id=callback_data.account_id, action=callback_data.action)
    if isinstance(query.message, Message):
        await query.message.edit_text(
            prompts[callback_data.action],
            reply_markup=_cancel_keyboard(callback_data.account_id),
        )
    await query.answer()


@router.message(EditProfile.waiting_value)
async def receive_value(
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
    action = str(data["action"])
    raw = (message.text or "").strip()
    operation_type, payload = _payload_for(action, raw)

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
            idempotency_key=f"bot:{message.chat.id}:{message.message_id}",
        )
        applied = await service.apply_operation(operation_id=op.id, role=identity.role)
        await session.commit()

    await state.clear()
    await message.answer(format_op_result(applied), parse_mode="HTML")


@router.callback_query(ProfileCB.filter(F.action == "photo_delete_all"))
async def delete_all_photos(
    query: CallbackQuery,
    callback_data: ProfileCB,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        service = ProfileService(
            session,
            settings=settings,
            key_provider=key_provider,
            redis=redis,
        )
        op = await service.create_operation(
            account_id=uuid.UUID(callback_data.account_id),
            operation_type="profile.photo.delete_all",
            payload={},
            operator_tg_id=identity.tg_user_id,
            idempotency_key=f"bot:{query.id}",
        )
        applied = await service.apply_operation(operation_id=op.id, role=identity.role)
        await session.commit()
    await query.answer(format_op_result(applied).replace("<code>", "").replace("</code>", ""), show_alert=True)


def _payload_for(action: str, raw: str) -> tuple[str, dict[str, str | None]]:
    if action == "name":
        first, _, last = raw.partition(" ")
        return "profile.name", {"first_name": first, "last_name": last or None}
    if action == "username":
        return "profile.username", {"username": None if raw == "-" else raw.removeprefix("@")}
    if action == "bio":
        return "profile.bio", {"bio": "" if raw == "-" else raw}
    raise ValueError(f"unsupported profile action: {action}")
