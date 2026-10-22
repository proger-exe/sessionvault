"""FSM «Добавить аккаунт».

Состояния:
  waiting_phone → waiting_code → (waiting_2fa)? → done

Ключевые инварианты (см. Этап 0 + ТЗ п.4.2):
- один живой Kurigram `Client` держится в оперативной памяти FSM;
- сообщение пользователя с кодом / 2FA удаляется через `bot.delete_message`
  **сразу** после чтения; в state отправляется только «получено», не значение;
- при `PHONE_CODE_EXPIRED` / инвалидном коде даём операторам ещё попытки
  (до 3 на код, до 3 на пароль), потом cancel;
- при отмене — flag → `LoginFlowState.CANCELLED`, клиент disconnected.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionvault.bot.callback_data import MenuCB
from sessionvault.bot.middlewares.auth import IdentityInfo
from sessionvault.config import get_settings
from sessionvault.domain.accounts import AccountsService
from sessionvault.domain.audit import AuditService
from sessionvault.mtproto.errors import LoginError, LoginErrorKind
from sessionvault.mtproto.login import ExtractedSession, LoginSession
from sessionvault.security.key_provider import KeyProvider
from sessionvault.storage.db.models import AuditResult, LoginFlow, LoginFlowState

log = logging.getLogger(__name__)
router = Router(name="add_account")

_MAX_CODE_ATTEMPTS = 3
_MAX_PASSWORD_ATTEMPTS = 3
_PHONE_RE = re.compile(r"^\+\d{7,15}$")


class AddAccount(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_2fa = State()


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖ Отмена", callback_data="add_account:cancel")]]
    )


@router.message(Command("add_account"))
@router.callback_query(MenuCB.filter(F.section == "add_account"))
async def start(event: Message | CallbackQuery, state: FSMContext, identity: IdentityInfo) -> None:
    await state.clear()
    await state.set_state(AddAccount.waiting_phone)
    text = (
        "Пришли номер телефона в формате E.164 (например, <code>+56933617390</code>).\n\n"
        "Никаких пробелов и скобок."
    )
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await event.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=_cancel_keyboard(),
            )
        await event.answer()
    else:
        await event.answer(text, parse_mode="HTML", reply_markup=_cancel_keyboard())


@router.message(AddAccount.waiting_phone)
async def receive_phone(
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = (message.text or "").strip()
    # Немедленно удаляем сообщение с номером из чата.
    await _safe_delete(message)
    if not _PHONE_RE.match(phone):
        await message.answer("Формат не подходит. Ожидаю E.164, например <code>+56933617390</code>.",
                             parse_mode="HTML", reply_markup=_cancel_keyboard())
        return

    settings = get_settings()
    login = LoginSession(
        api_id=settings.pyrogram_api_id,
        api_hash=settings.pyrogram_api_hash.get_secret_value(),
        phone=phone,
    )
    try:
        sent = await login.start()
    except LoginError as e:
        await login.cancel()
        await _report_login_error(message, e, db_session_factory, identity, phone)
        await state.clear()
        return

    status_msg = await message.answer(
        f"Код отправлен ({sent.code_type}). Пришли его сюда одним сообщением.",
        reply_markup=_cancel_keyboard(),
    )

    # Держим LoginSession в памяти FSM. aiogram FSM хранит state в Redis;
    # положить туда сам client объект нельзя — храним как атрибут dispatcher-а
    # через слот `context.data`. Для MVP используем in-memory map по chat_id.
    _sessions_by_chat[message.chat.id] = login
    await _record_flow(
        db_session_factory,
        phone=phone,
        operator_tg_id=identity.tg_user_id,
        chat_id=message.chat.id,
        status_message_id=status_msg.message_id,
        state=LoginFlowState.WAITING_CODE,
        ttl_seconds=settings.login_flow_ttl_seconds,
    )
    await state.update_data(
        phone=phone,
        status_message_id=status_msg.message_id,
        attempts_code=0,
        attempts_password=0,
    )
    await state.set_state(AddAccount.waiting_code)


@router.message(AddAccount.waiting_code)
async def receive_code(
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    db_session_factory: async_sessionmaker[AsyncSession],
    key_provider: KeyProvider,
) -> None:
    code = (message.text or "").strip()
    await _safe_delete(message)

    login = _sessions_by_chat.get(message.chat.id)
    if login is None:
        await message.answer("Сессия login-flow не найдена — начни заново командой /add_account.")
        await state.clear()
        return

    data = await state.get_data()
    attempts = int(data.get("attempts_code", 0)) + 1
    await state.update_data(attempts_code=attempts)

    try:
        result = await login.submit_code(code)
    except LoginError as e:
        if e.kind == LoginErrorKind.PHONE_CODE_INVALID and attempts < _MAX_CODE_ATTEMPTS:
            await message.answer(
                f"Код не подошёл. Попытка {attempts}/{_MAX_CODE_ATTEMPTS}.",
                reply_markup=_cancel_keyboard(),
            )
            return
        await _report_login_error(message, e, db_session_factory, identity, data.get("phone", ""))
        await _finalize(login, message.chat.id, state, db_session_factory, LoginFlowState.FAILED)
        return

    if result is None:
        await state.set_state(AddAccount.waiting_2fa)
        await message.answer(
            "Требуется облачный пароль 2FA. Пришли его одним сообщением.",
            reply_markup=_cancel_keyboard(),
        )
        return

    await _persist_and_finish(
        message=message,
        state=state,
        identity=identity,
        db_session_factory=db_session_factory,
        key_provider=key_provider,
        login=login,
        phone=data.get("phone", ""),
        extracted=result,
    )


@router.message(AddAccount.waiting_2fa)
async def receive_2fa(
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    db_session_factory: async_sessionmaker[AsyncSession],
    key_provider: KeyProvider,
) -> None:
    password = message.text or ""
    await _safe_delete(message)

    login = _sessions_by_chat.get(message.chat.id)
    if login is None:
        await message.answer("Сессия login-flow не найдена — начни заново командой /add_account.")
        await state.clear()
        return

    data = await state.get_data()
    attempts = int(data.get("attempts_password", 0)) + 1
    await state.update_data(attempts_password=attempts)

    try:
        result = await login.submit_password(password)
    except LoginError as e:
        if e.kind == LoginErrorKind.PASSWORD_HASH_INVALID and attempts < _MAX_PASSWORD_ATTEMPTS:
            await message.answer(
                f"Пароль не подошёл. Попытка {attempts}/{_MAX_PASSWORD_ATTEMPTS}.",
                reply_markup=_cancel_keyboard(),
            )
            return
        await _report_login_error(message, e, db_session_factory, identity, data.get("phone", ""))
        await _finalize(login, message.chat.id, state, db_session_factory, LoginFlowState.FAILED)
        return

    await _persist_and_finish(
        message=message,
        state=state,
        identity=identity,
        db_session_factory=db_session_factory,
        key_provider=key_provider,
        login=login,
        phone=data.get("phone", ""),
        extracted=result,
    )


@router.callback_query(F.data == "add_account:cancel")
async def cancel_callback(
    query: CallbackQuery,
    state: FSMContext,
    identity: IdentityInfo,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if not isinstance(query.message, Message):
        await query.answer("Сообщение недоступно.", show_alert=True)
        return
    login = _sessions_by_chat.pop(query.message.chat.id, None)
    if login is not None:
        await login.cancel()
    await _finalize(
        login,
        query.message.chat.id,
        state,
        db_session_factory,
        LoginFlowState.CANCELLED,
    )
    await query.message.edit_text("Отменено.")
    await query.answer()


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

# In-memory кэш активных LoginSession-ов по chat_id.
# TODO Этап 2/3: перенести в отдельный `mtproto_worker`, если понадобится
# HA-масштабирование или множественные инстансы бота.
_sessions_by_chat: dict[int, LoginSession] = {}


async def _persist_and_finish(
    *,
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    db_session_factory: async_sessionmaker[AsyncSession],
    key_provider: KeyProvider,
    login: LoginSession,
    phone: str,
    extracted: ExtractedSession,
) -> None:
    async with db_session_factory() as session:
        accounts = AccountsService(session, key_provider)
        saved = await accounts.save_after_login(
            phone_e164=phone,
            session_data=extracted,
            operator_tg_id=identity.tg_user_id,
        )
        audit = AuditService(session)
        await audit.record_now(
            action="account.login",
            request_id=f"login-{saved.account_id}-{uuid.uuid4()}",
            result=AuditResult.OK,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
            account_id=saved.account_id,
            chat_id=message.chat.id,
            changed_fields=["session"],
        )
        await session.commit()

    await _finalize(login, message.chat.id, state, db_session_factory, LoginFlowState.AUTHORIZED,
                    account_id=saved.account_id)
    await message.answer(
        f"✔ Аккаунт сохранён. tg_user_id: <code>{extracted.user_id}</code>",
        parse_mode="HTML",
    )


async def _finalize(
    login: LoginSession | None,
    chat_id: int,
    state: FSMContext,
    db_session_factory: async_sessionmaker[AsyncSession],
    flow_state: LoginFlowState,
    *,
    account_id: uuid.UUID | None = None,
) -> None:
    _sessions_by_chat.pop(chat_id, None)
    if login is not None:
        await login.cancel()
    async with db_session_factory() as session:
        flow = (
            await session.execute(
                select(LoginFlow)
                .where(LoginFlow.chat_id == chat_id)
                .order_by(LoginFlow.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if flow is not None:
            flow.state = flow_state
            flow.account_id = account_id
            await session.commit()
    await state.clear()


async def _report_login_error(
    message: Message,
    error: LoginError,
    db_session_factory: async_sessionmaker[AsyncSession],
    identity: IdentityInfo,
    phone: str,
) -> None:
    reply = {
        LoginErrorKind.PHONE_CODE_INVALID: "Код не подошёл — попробуй ещё раз.",
        LoginErrorKind.PHONE_CODE_EXPIRED: "Код истёк — запусти /add_account заново.",
        LoginErrorKind.PHONE_NUMBER_INVALID: "Номер не подходит.",
        LoginErrorKind.PHONE_NUMBER_BANNED: "Номер заблокирован Telegram.",
        LoginErrorKind.SESSION_PASSWORD_NEEDED: "Нужен пароль 2FA.",
        LoginErrorKind.PASSWORD_HASH_INVALID: "Пароль 2FA не подошёл.",
        LoginErrorKind.FLOOD_WAIT: (
            f"FLOOD_WAIT: подожди {error.retry_after or '?'} секунд."
        ),
        LoginErrorKind.NETWORK: "Сетевая ошибка — попробуй позже.",
        LoginErrorKind.OTHER: f"Ошибка Telegram: {error.code}",
    }[error.kind]
    await message.answer(reply)

    async with db_session_factory() as session:
        audit = AuditService(session)
        await audit.record_now(
            action="account.login",
            request_id=f"login-fail-{identity.tg_user_id}-{uuid.uuid4()}",
            result=AuditResult.FAILED,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
            chat_id=message.chat.id,
            error_code=error.code,
        )
        await session.commit()

    log.info("login error phone=***REDACTED*** kind=%s code=%s", error.kind.value, error.code)


async def _record_flow(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    phone: str,
    operator_tg_id: int,
    chat_id: int,
    status_message_id: int,
    state: LoginFlowState,
    ttl_seconds: int,
) -> None:
    import hashlib as _hashlib

    async with db_session_factory() as session:
        flow = LoginFlow(
            phone_hash=_hashlib.sha256(phone.encode()).hexdigest(),
            state=state,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            operator_tg_id=operator_tg_id,
            chat_id=chat_id,
            status_message_id=status_message_id,
        )
        session.add(flow)
        await session.commit()


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        log.debug("не удалось удалить сообщение с секретом — оставляем в чате")
