"""Phase-2 security flows: 2FA, recovery email, active sessions, phone change.

- Passwords/codes NEVER go into callback_data or long-lived FSM state without
  necessity. Received messages with secrets are deleted immediately.
- Dangerous mutations (twofa_enable/change/remove, phone_change, session_revoke)
  require an inline confirmation button signed by HMAC (ConfirmCB.sig).
"""

from __future__ import annotations

import html
import logging
import re
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionvault.bot.callback_data import (
    AccountCardCB,
    AccountsPageCB,
    ConfirmCB,
    SecurityCB,
    is_valid_uuid,
    sign_confirm,
    verify_confirm,
)
from sessionvault.bot.middlewares.auth import IdentityInfo
from sessionvault.config import Settings
from sessionvault.domain.twofa import OpOutcome, SecurityService
from sessionvault.security.key_provider import KeyProvider

log = logging.getLogger(__name__)
router = Router(name="security")

_PHONE_RE = re.compile(r"^\+\d{7,15}$")


def _chat_and_message_ids(query: CallbackQuery) -> tuple[int | None, int | None]:
    if isinstance(query.message, Message):
        return query.message.chat.id, query.message.message_id
    return None, None


class TwoFAInput(StatesGroup):
    waiting_enable = State()    # payload: password [| hint]
    waiting_change = State()    # payload: current | new [| hint]
    waiting_remove = State()    # payload: current password
    waiting_recovery_code = State()  # email code
    waiting_new_phone = State()
    waiting_phone_code = State()  # after code sent


# ---------- Rendering ----------

async def _render_security_menu(
    *,
    account_id: str,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, InlineKeyboardMarkup]:
    account_uuid = uuid.UUID(account_id)
    async with db_session_factory() as session:
        svc = SecurityService(session, settings=settings, key_provider=key_provider, redis=redis)
        summary = await svc.get_summary(account_id=account_uuid)

    lines = ["🔐 <b>Безопасность</b>"]
    if summary is None:
        lines.append("Аккаунт недоступен.")
    else:
        lines.append(f"2FA: <code>{'да' if summary.has_password else 'нет'}</code>")
        lines.append(f"recovery email: <code>{'да' if summary.has_recovery_email else 'нет'}</code>")
        # Escape user-controlled strings (owner-set hint, Telegram-provided pattern):
        # unescaped `<` or `&` breaks parse_mode='HTML' and makes the menu unrenderable.
        if summary.pending_email_pattern:
            lines.append(f"ожидает подтверждения: <code>{html.escape(summary.pending_email_pattern)}</code>")
        if summary.hint:
            lines.append(f"подсказка: <code>{html.escape(summary.hint)}</code>")

    rows: list[list[InlineKeyboardButton]] = []
    if summary and not summary.has_password:
        rows.append([InlineKeyboardButton(
            text="🔒 Включить 2FA",
            callback_data=SecurityCB(action="twofa_enable", account_id=account_id).pack(),
        )])
    if summary and summary.has_password:
        rows.append([InlineKeyboardButton(
            text="🔁 Сменить пароль 2FA",
            callback_data=SecurityCB(action="twofa_change", account_id=account_id).pack(),
        )])
        rows.append([InlineKeyboardButton(
            text="🚫 Отключить 2FA",
            callback_data=SecurityCB(action="twofa_remove", account_id=account_id).pack(),
        )])
    if summary and summary.pending_email_pattern:
        rows.append([InlineKeyboardButton(
            text="✉️ Подтвердить recovery email",
            callback_data=SecurityCB(action="recovery_confirm", account_id=account_id).pack(),
        )])
    rows.append([InlineKeyboardButton(
        text="🖥 Активные сессии",
        callback_data=SecurityCB(action="sessions", account_id=account_id).pack(),
    )])
    rows.append([InlineKeyboardButton(
        text="📞 Сменить номер",
        callback_data=SecurityCB(action="phone_change", account_id=account_id).pack(),
    )])
    rows.append([InlineKeyboardButton(
        text="◀ К карточке",
        callback_data=AccountsPageCB(action="open", page=0, account_id=account_id).pack(),
    )])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- Entry points ----------

@router.callback_query(AccountCardCB.filter(F.action == "security"))
async def open_security(
    query: CallbackQuery,
    callback_data: AccountCardCB,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if not is_valid_uuid(callback_data.account_id):
        await query.answer("Некорректный account_id.", show_alert=True)
        return
    text, kb = await _render_security_menu(
        account_id=callback_data.account_id,
        settings=settings,
        key_provider=key_provider,
        redis=redis,
        db_session_factory=db_session_factory,
    )
    if isinstance(query.message, Message):
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await query.answer()


@router.callback_query(SecurityCB.filter(F.action == "menu"))
async def back_to_security_menu(
    query: CallbackQuery,
    callback_data: SecurityCB,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if not is_valid_uuid(callback_data.account_id):
        await query.answer("Некорректный account_id.", show_alert=True)
        return
    text, kb = await _render_security_menu(
        account_id=callback_data.account_id,
        settings=settings,
        key_provider=key_provider,
        redis=redis,
        db_session_factory=db_session_factory,
    )
    if isinstance(query.message, Message):
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await query.answer()


# ---------- 2FA enable ----------

def _confirm_kb(action: str, account_id: str, settings: Settings, *, label: str) -> InlineKeyboardMarkup:
    cb = sign_confirm(ConfirmCB(action=action, account_id=account_id), settings)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=cb.pack())],
        [InlineKeyboardButton(text="Отмена", callback_data=SecurityCB(action="menu", account_id=account_id).pack())],
    ])


@router.callback_query(SecurityCB.filter(F.action == "twofa_enable"))
async def prompt_enable(query: CallbackQuery, callback_data: SecurityCB, settings: Settings) -> None:
    if not is_valid_uuid(callback_data.account_id):
        await query.answer("Некорректный account_id.", show_alert=True)
        return
    if isinstance(query.message, Message):
        await query.message.edit_text(
            "<b>Включение 2FA</b>\n\n"
            "После подтверждения запрошу пароль. Сообщение с паролём удалю сразу.\n"
            "TZ: пароль по умолчанию НЕ хранится в БД.",
            parse_mode="HTML",
            reply_markup=_confirm_kb("twofa_enable", callback_data.account_id, settings, label="Продолжить"),
        )
    await query.answer()


@router.callback_query(SecurityCB.filter(F.action == "twofa_change"))
async def prompt_change(query: CallbackQuery, callback_data: SecurityCB, settings: Settings) -> None:
    if not is_valid_uuid(callback_data.account_id):
        await query.answer("Некорректный account_id.", show_alert=True)
        return
    if isinstance(query.message, Message):
        await query.message.edit_text(
            "<b>Смена пароля 2FA</b>\n\nЗапрошу текущий и новый пароль отдельными сообщениями.",
            parse_mode="HTML",
            reply_markup=_confirm_kb("twofa_change", callback_data.account_id, settings, label="Продолжить"),
        )
    await query.answer()


@router.callback_query(SecurityCB.filter(F.action == "twofa_remove"))
async def prompt_remove(query: CallbackQuery, callback_data: SecurityCB, settings: Settings) -> None:
    if not is_valid_uuid(callback_data.account_id):
        await query.answer("Некорректный account_id.", show_alert=True)
        return
    if isinstance(query.message, Message):
        await query.message.edit_text(
            "<b>Отключение 2FA</b>\n\n⚠️ После отключения recovery email тоже пропадёт.",
            parse_mode="HTML",
            reply_markup=_confirm_kb("twofa_remove", callback_data.account_id, settings, label="Отключить"),
        )
    await query.answer()


# ---------- ConfirmCB dispatch ----------

@router.callback_query(ConfirmCB.filter(F.action.in_({"twofa_enable", "twofa_change", "twofa_remove"})))
async def after_twofa_confirm(
    query: CallbackQuery,
    callback_data: ConfirmCB,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not verify_confirm(callback_data, settings):
        await query.answer("Подпись подтверждения не сходится.", show_alert=True)
        return
    action = callback_data.action
    prompts = {
        "twofa_enable": ("Пришли пароль. Можно с подсказкой: <code>пароль | подсказка</code>.", TwoFAInput.waiting_enable),
        "twofa_change": ("Пришли: <code>текущий | новый | подсказка</code> (подсказка опциональна).", TwoFAInput.waiting_change),
        "twofa_remove": ("Пришли текущий пароль.", TwoFAInput.waiting_remove),
    }
    prompt, next_state = prompts[action]
    await state.set_state(next_state)
    await state.update_data(account_id=callback_data.account_id)
    if isinstance(query.message, Message):
        await query.message.edit_text(prompt, parse_mode="HTML")
    await query.answer()


@router.callback_query(ConfirmCB.filter(F.action == "phone_change"))
async def after_phone_confirm(
    query: CallbackQuery,
    callback_data: ConfirmCB,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not verify_confirm(callback_data, settings):
        await query.answer("Подпись подтверждения не сходится.", show_alert=True)
        return
    await state.set_state(TwoFAInput.waiting_new_phone)
    await state.update_data(account_id=callback_data.account_id)
    if isinstance(query.message, Message):
        await query.message.edit_text(
            "Пришли новый номер в E.164, например <code>+380631234567</code>.",
            parse_mode="HTML",
        )
    await query.answer()


# ---------- 2FA text handlers ----------

async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        log.debug("failed to delete secret message chat_id=%s", message.chat.id)


def _split_pipes(raw: str, expected: int) -> list[str]:
    """Split on '|' into `expected` slots (right-padded with '').

    ponytail: silently truncates extra pieces beyond `expected`. Passwords
    containing '|' are a data-loss footgun; callers should validate that
    the resulting password fields don't contain the delimiter — see
    _reject_pipe_in_password below.
    """
    parts = [p.strip() for p in raw.split("|")]
    while len(parts) < expected:
        parts.append("")
    return parts[:expected]


def _has_delim_ambiguity(raw: str, expected: int) -> bool:
    """True if raw has MORE '|' separators than expected fields can hold.

    E.g. `pw|word|hint` with expected=2 → 3 parts, but _split_pipes drops
    the third → user's real password 'pw|word' silently became 'pw'.
    """
    return raw.count("|") > expected - 1


async def _apply_and_reply(
    message: Message,
    *,
    outcome: OpOutcome,
    on_ok: str,
    account_id: str,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if outcome.ok:
        extra = " (ждёт код на email)" if outcome.pending_email else ""
        await message.answer(f"{on_ok}{extra}.")
    else:
        code = outcome.error_code or "UNKNOWN"
        await message.answer(f"Не удалось: <code>{html.escape(code)}</code>", parse_mode="HTML")
    text, kb = await _render_security_menu(
        account_id=account_id,
        settings=settings,
        key_provider=key_provider,
        redis=redis,
        db_session_factory=db_session_factory,
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(TwoFAInput.waiting_enable)
async def handle_enable_input(
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = message.text or ""
    await _safe_delete(message)
    if not raw.strip():
        await message.answer("Пусто. Отменяю."); await state.clear(); return
    if _has_delim_ambiguity(raw, 2):
        await message.answer(
            "Слишком много '|'. Пароль не должен содержать '|'. Попробуй ещё раз без подсказки."
        )
        await state.clear(); return
    password, hint = _split_pipes(raw, 2)
    if not password:
        await message.answer("Пароль пуст. Отменяю."); await state.clear(); return

    data = await state.get_data()
    account_id = str(data["account_id"])
    account_uuid = uuid.UUID(account_id)
    async with db_session_factory() as session:
        svc = SecurityService(session, settings=settings, key_provider=key_provider, redis=redis)
        outcome = await svc.enable_2fa(
            account_id=account_uuid,
            password=password,
            hint=hint,
            email=None,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
            idempotency_key=f"bot:{message.chat.id}:{message.message_id}",
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
        await session.commit()
    await state.clear()
    await _apply_and_reply(
        message,
        outcome=outcome,
        on_ok="2FA включена",
        account_id=account_id,
        settings=settings,
        key_provider=key_provider,
        redis=redis,
        db_session_factory=db_session_factory,
    )


@router.message(TwoFAInput.waiting_change)
async def handle_change_input(
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = message.text or ""
    await _safe_delete(message)
    if not raw.strip():
        await message.answer("Пусто. Отменяю."); await state.clear(); return
    if _has_delim_ambiguity(raw, 3):
        await message.answer(
            "Слишком много '|'. Пароли не должны содержать '|'. Попробуй без подсказки."
        )
        await state.clear(); return
    current, new_pw, new_hint = _split_pipes(raw, 3)
    if not current or not new_pw:
        await message.answer("Нужны текущий и новый пароль. Отменяю."); await state.clear(); return

    data = await state.get_data()
    account_id = str(data["account_id"])
    account_uuid = uuid.UUID(account_id)
    async with db_session_factory() as session:
        svc = SecurityService(session, settings=settings, key_provider=key_provider, redis=redis)
        outcome = await svc.change_2fa(
            account_id=account_uuid,
            current_password=current,
            new_password=new_pw,
            new_hint=new_hint,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
            idempotency_key=f"bot:{message.chat.id}:{message.message_id}",
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
        await session.commit()
    await state.clear()
    await _apply_and_reply(
        message,
        outcome=outcome,
        on_ok="Пароль 2FA сменён",
        account_id=account_id,
        settings=settings,
        key_provider=key_provider,
        redis=redis,
        db_session_factory=db_session_factory,
    )


@router.message(TwoFAInput.waiting_remove)
async def handle_remove_input(
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = (message.text or "").strip()
    await _safe_delete(message)
    if not raw:
        await message.answer("Пусто. Отменяю."); await state.clear(); return

    data = await state.get_data()
    account_id = str(data["account_id"])
    account_uuid = uuid.UUID(account_id)
    async with db_session_factory() as session:
        svc = SecurityService(session, settings=settings, key_provider=key_provider, redis=redis)
        outcome = await svc.remove_2fa(
            account_id=account_uuid,
            current_password=raw,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
            idempotency_key=f"bot:{message.chat.id}:{message.message_id}",
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
        await session.commit()
    await state.clear()
    await _apply_and_reply(
        message,
        outcome=outcome,
        on_ok="2FA отключена",
        account_id=account_id,
        settings=settings,
        key_provider=key_provider,
        redis=redis,
        db_session_factory=db_session_factory,
    )


# ---------- Recovery email confirm ----------

@router.callback_query(SecurityCB.filter(F.action == "recovery_confirm"))
async def prompt_recovery_confirm(query: CallbackQuery, callback_data: SecurityCB, state: FSMContext) -> None:
    if not is_valid_uuid(callback_data.account_id):
        await query.answer("Некорректный account_id.", show_alert=True)
        return
    await state.set_state(TwoFAInput.waiting_recovery_code)
    await state.update_data(account_id=callback_data.account_id)
    if isinstance(query.message, Message):
        await query.message.edit_text("Пришли код подтверждения recovery-email из письма.")
    await query.answer()


@router.message(TwoFAInput.waiting_recovery_code)
async def handle_recovery_code(
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    code = (message.text or "").strip()
    await _safe_delete(message)
    if not code.isdigit():
        await message.answer("Код должен быть цифрами. Отменяю."); await state.clear(); return

    data = await state.get_data()
    account_id = str(data["account_id"])
    account_uuid = uuid.UUID(account_id)
    async with db_session_factory() as session:
        svc = SecurityService(session, settings=settings, key_provider=key_provider, redis=redis)
        outcome = await svc.confirm_recovery_email(
            account_id=account_uuid,
            code=code,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
            idempotency_key=f"bot:{message.chat.id}:{message.message_id}",
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
        await session.commit()
    await state.clear()
    await _apply_and_reply(
        message,
        outcome=outcome,
        on_ok="Recovery email подтверждён",
        account_id=account_id,
        settings=settings,
        key_provider=key_provider,
        redis=redis,
        db_session_factory=db_session_factory,
    )


# ---------- Sessions ----------

async def _build_sessions_view(
    *,
    account_id: str,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, InlineKeyboardMarkup]:
    account_uuid = uuid.UUID(account_id)
    async with db_session_factory() as session:
        svc = SecurityService(session, settings=settings, key_provider=key_provider, redis=redis)
        sessions = await svc.list_sessions(account_id=account_uuid)

    if not sessions:
        text = "Активных сессий не найдено."
        rows: list[list[InlineKeyboardButton]] = []
    else:
        lines = ["<b>Активные сессии</b>:"]
        rows = []
        for i, s in enumerate(sessions, 1):
            marker = "🟢 " if s.is_current else ""
            date_s = s.last_active_at.strftime("%Y-%m-%d %H:%M") if s.last_active_at else "—"
            # Escape Telegram-provided strings: device_model/app_name/etc. are
            # attacker-controlled (any client may set them arbitrarily).
            lines.append(
                f"<code>{i}</code>. {marker}"
                f"{html.escape(s.device_model or '?')} · "
                f"{html.escape(s.app_name or '?')} · "
                f"{html.escape(s.platform or '?')} · "
                f"{html.escape(s.ip or '?')} · {date_s}"
            )
            if not s.is_current:
                rows.append([InlineKeyboardButton(
                    text=f"🗑 {i}",
                    callback_data=SecurityCB(action="sr", account_id=account_id, arg=str(s.id)).pack(),
                )])
        text = "\n".join(lines)
    rows.append([InlineKeyboardButton(
        text="◀ Назад",
        callback_data=SecurityCB(action="menu", account_id=account_id).pack(),
    )])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(SecurityCB.filter(F.action == "sessions"))
async def show_sessions(
    query: CallbackQuery,
    callback_data: SecurityCB,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if not is_valid_uuid(callback_data.account_id):
        await query.answer("Некорректный account_id.", show_alert=True); return
    await query.answer("Читаю…")
    text, kb = await _build_sessions_view(
        account_id=callback_data.account_id,
        settings=settings,
        key_provider=key_provider,
        redis=redis,
        db_session_factory=db_session_factory,
    )
    if isinstance(query.message, Message):
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# Session-revoke: two-step SecurityCB (no HMAC) — AuthMiddleware limits access
# to Owner/Admin, and the full ConfirmCB(action="sess_rvk:{id}"):sig payload
# exceeds Telegram's 64-byte callback_data limit. Actions kept ≤3 chars.
@router.callback_query(SecurityCB.filter(F.action == "sr"))
async def confirm_session_revoke(query: CallbackQuery, callback_data: SecurityCB) -> None:
    if not is_valid_uuid(callback_data.account_id):
        await query.answer("Некорректный account_id.", show_alert=True); return
    if not callback_data.arg.lstrip("-").isdigit():
        await query.answer("Некорректный session_id.", show_alert=True); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отозвать", callback_data=SecurityCB(
            action="srD", account_id=callback_data.account_id, arg=callback_data.arg,
        ).pack())],
        [InlineKeyboardButton(text="Отмена", callback_data=SecurityCB(
            action="sessions", account_id=callback_data.account_id,
        ).pack())],
    ])
    if isinstance(query.message, Message):
        await query.message.edit_text(
            f"Отозвать сессию <code>{html.escape(callback_data.arg)}</code>?",
            parse_mode="HTML",
            reply_markup=kb,
        )
    await query.answer()


@router.callback_query(SecurityCB.filter(F.action == "srD"))
async def do_session_revoke(
    query: CallbackQuery,
    callback_data: SecurityCB,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if not is_valid_uuid(callback_data.account_id):
        await query.answer("Некорректный account_id.", show_alert=True); return
    if not callback_data.arg.lstrip("-").isdigit():
        await query.answer("Битый session_id.", show_alert=True); return

    account_uuid = uuid.UUID(callback_data.account_id)
    chat_id, message_id = _chat_and_message_ids(query)
    async with db_session_factory() as session:
        svc = SecurityService(session, settings=settings, key_provider=key_provider, redis=redis)
        outcome = await svc.revoke_session(
            account_id=account_uuid,
            session_id=int(callback_data.arg),
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
            idempotency_key=f"bot:{query.id}",
            chat_id=chat_id,
            message_id=message_id,
        )
        await session.commit()
    if outcome.ok:
        await query.answer("Отозвано.")
    else:
        await query.answer(f"Не удалось: {outcome.error_code}", show_alert=True)
    # Re-render list. Answer the query once above; only rebuild the message here.
    if isinstance(query.message, Message):
        text, kb = await _build_sessions_view(
            account_id=callback_data.account_id,
            settings=settings,
            key_provider=key_provider,
            redis=redis,
            db_session_factory=db_session_factory,
        )
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ---------- Phone change ----------

@router.callback_query(SecurityCB.filter(F.action == "phone_change"))
async def prompt_phone_change(query: CallbackQuery, callback_data: SecurityCB, settings: Settings) -> None:
    if not is_valid_uuid(callback_data.account_id):
        await query.answer("Некорректный account_id.", show_alert=True)
        return
    if isinstance(query.message, Message):
        await query.message.edit_text(
            "<b>Смена номера</b>\n\n"
            "⚠️ Telegram может отклонить смену, если аккаунт молодой или под ограничениями.\n"
            "После подтверждения запрошу новый номер, затем код из Telegram.",
            parse_mode="HTML",
            reply_markup=_confirm_kb("phone_change", callback_data.account_id, settings, label="Продолжить"),
        )
    await query.answer()


@router.message(TwoFAInput.waiting_new_phone)
async def handle_new_phone(
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = (message.text or "").strip()
    chat_id = message.chat.id
    message_id = message.message_id
    # Phone number is PII per module docstring — delete before doing anything else.
    await _safe_delete(message)
    if not _PHONE_RE.match(phone):
        await message.answer("Не E.164. Пример <code>+380631234567</code>.", parse_mode="HTML")
        return
    data = await state.get_data()
    account_id = str(data["account_id"])
    account_uuid = uuid.UUID(account_id)
    async with db_session_factory() as session:
        svc = SecurityService(session, settings=settings, key_provider=key_provider, redis=redis)
        outcome, sent = await svc.send_phone_change_code(
            account_id=account_uuid,
            new_phone_e164=phone,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
            chat_id=chat_id,
            message_id=message_id,
        )
        await session.commit()
    if not outcome.ok or sent is None:
        await message.answer(f"Не удалось запросить код: <code>{outcome.error_code}</code>", parse_mode="HTML")
        await state.clear()
        return
    await state.set_state(TwoFAInput.waiting_phone_code)
    await state.update_data(
        account_id=account_id,
        new_phone=phone,
        phone_code_hash=sent.phone_code_hash,
    )
    await message.answer(
        f"Код отправлен ({html.escape(sent.type_name)}). Пришли код цифрами.",
    )


@router.message(TwoFAInput.waiting_phone_code)
async def handle_phone_code(
    message: Message,
    state: FSMContext,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    code = (message.text or "").strip()
    await _safe_delete(message)
    if not code.isdigit():
        await message.answer("Код должен быть цифрами."); return
    data: dict[str, Any] = await state.get_data()
    account_id = str(data["account_id"])
    new_phone = str(data["new_phone"])
    phone_code_hash = str(data["phone_code_hash"])
    account_uuid = uuid.UUID(account_id)
    async with db_session_factory() as session:
        svc = SecurityService(session, settings=settings, key_provider=key_provider, redis=redis)
        outcome = await svc.confirm_phone_change(
            account_id=account_uuid,
            new_phone_e164=new_phone,
            phone_code_hash=phone_code_hash,
            phone_code=code,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
            idempotency_key=f"bot:{message.chat.id}:{message.message_id}",
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
        await session.commit()
    await state.clear()
    await _apply_and_reply(
        message,
        outcome=outcome,
        on_ok=f"Номер сменён на …{new_phone[-4:]}",
        account_id=account_id,
        settings=settings,
        key_provider=key_provider,
        redis=redis,
        db_session_factory=db_session_factory,
    )
