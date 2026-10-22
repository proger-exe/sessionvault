"""Карточка одного аккаунта.

MVP-разделы: профиль (просмотр), безопасность (2FA/recovery), сессии,
экспорт, журнал. Наполнение действий — Этапы 2/3. Здесь только каркас +
переход обратно к списку.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionvault.bot.callback_data import AccountCardCB, AccountsPageCB, MenuCB, is_valid_uuid
from sessionvault.bot.middlewares.auth import IdentityInfo
from sessionvault.config import Settings
from sessionvault.domain.accounts import AccountsService
from sessionvault.domain.accounts.service import AccountSecrets
from sessionvault.domain.audit import AuditService
from sessionvault.domain.profile import ProfileService
from sessionvault.mtproto.account_client import account_client
from sessionvault.mtproto.login_code import snapshot_service_message_ids, wait_for_login_code
from sessionvault.security.key_provider import KeyProvider
from sessionvault.storage.db.models import AuditResult, TelegramAccount

log = logging.getLogger(__name__)
router = Router(name="account_card")
_login_code_tasks: set[asyncio.Task[None]] = set()


def _summary(a: TelegramAccount) -> str:
    name = " ".join(x for x in (a.first_name, a.last_name) if x) or "—"
    lines = [
        f"<b>{name}</b>" + (f" @{a.username}" if a.username else ""),
        f"tg_user_id: <code>{a.tg_user_id}</code>",
        f"телефон: <code>…{a.phone_last4}</code>",
        f"DC: <code>{a.dc_id}</code>",
        f"статус: <code>{a.status.value}</code>",
        f"риск: <code>{a.risk_state.value}</code>",
        f"2FA: <code>{'да' if a.has_2fa else 'нет'}</code>",
        f"recovery email: <code>{'да' if a.has_recovery_email else 'нет'}</code>",
    ]
    if a.last_health_check_at:
        lines.append(f"health-check: <code>{a.last_health_check_at:%Y-%m-%d %H:%M}</code>")
    return "\n".join(lines)


def _keyboard(account_id: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👤 Профиль", callback_data=AccountCardCB(action="profile", account_id=account_id).pack())],
        [InlineKeyboardButton(text="✨ Ещё", callback_data=AccountCardCB(action="extras", account_id=account_id).pack())],
        [InlineKeyboardButton(text="🔐 Безопасность", callback_data=AccountCardCB(action="security", account_id=account_id).pack())],
        [InlineKeyboardButton(text="🔢 Получить код", callback_data=AccountCardCB(action="login_code", account_id=account_id).pack())],
        [InlineKeyboardButton(text="🩺 Проверить сессию", callback_data=AccountCardCB(action="health", account_id=account_id).pack())],
        [InlineKeyboardButton(text="💾 Экспорт", callback_data=AccountCardCB(action="export", account_id=account_id).pack())],
        [InlineKeyboardButton(text="📜 Журнал", callback_data=AccountCardCB(action="audit", account_id=account_id).pack())],
        [
            InlineKeyboardButton(text="◀ К списку", callback_data=AccountsPageCB(action="page", page=0).pack()),
            InlineKeyboardButton(text="⌂ Меню", callback_data=MenuCB(section="root").pack()),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_card(
    *,
    account_id_str: str,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str | None, InlineKeyboardMarkup]:
    if not is_valid_uuid(account_id_str):
        return None, InlineKeyboardMarkup(inline_keyboard=[])
    account_id = uuid.UUID(account_id_str)
    async with db_session_factory() as session:
        accounts = AccountsService(session, key_provider=None)
        acc = await accounts.get_by_id(account_id)
    if acc is None:
        return None, InlineKeyboardMarkup(inline_keyboard=[])
    return _summary(acc), _keyboard(account_id_str)


@router.callback_query(AccountCardCB.filter(F.action == "health"))
async def run_health_check(
    query: CallbackQuery,
    callback_data: AccountCardCB,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    redis: Redis[bytes],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if not is_valid_uuid(callback_data.account_id):
        await query.answer("Некорректный account_id.", show_alert=True)
        return
    await query.answer("Проверяю…")
    account_id = uuid.UUID(callback_data.account_id)
    async with db_session_factory() as session:
        service = ProfileService(session, settings=settings, key_provider=key_provider, redis=redis)
        ok = await service.check_health(
            account_id=account_id,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
        )
        await session.commit()
    if isinstance(query.message, Message):
        text, keyboard = await render_card(
            account_id_str=callback_data.account_id,
            db_session_factory=db_session_factory,
        )
        header = "✅ Сессия жива.\n\n" if ok else "⚠️ Health-check не прошёл — риск обновлён.\n\n"
        if text is not None:
            await query.message.edit_text(header + text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(AccountCardCB.filter(F.action == "audit"))
async def stub_section(
    query: CallbackQuery,
    callback_data: AccountCardCB,
    identity: IdentityInfo,
) -> None:
    await query.answer("Журнал операций аккаунта — Этап 1, задача #15.", show_alert=True)


@router.callback_query(AccountCardCB.filter(F.action == "login_code"))
async def request_login_code(
    query: CallbackQuery,
    callback_data: AccountCardCB,
    identity: IdentityInfo,
    settings: Settings,
    key_provider: KeyProvider,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if not is_valid_uuid(callback_data.account_id):
        await query.answer("Некорректный account_id.", show_alert=True)
        return
    if not isinstance(query.message, Message):
        await query.answer("Сообщение недоступно.", show_alert=True)
        return

    account_id = uuid.UUID(callback_data.account_id)
    async with db_session_factory() as session:
        accounts = AccountsService(session, key_provider)
        secrets = await accounts.get_secrets(account_id)
        if secrets is None:
            await query.answer("Аккаунт не найден.", show_alert=True)
            return
        await AuditService(session).record_now(
            action="account.login_code.request",
            request_id=f"login-code-request-{account_id}-{uuid.uuid4()}",
            result=AuditResult.OK,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
            account_id=account_id,
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            changed_fields=[],
        )
        await session.commit()

    phone = secrets.phone_e164
    await query.message.answer(
        "Номер для запроса кода:\n"
        f"<code>{phone}</code>\n\n"
        "Запроси код в Telegram. Я пришлю его сюда, когда он появится.",
        parse_mode="HTML",
    )
    await query.answer("Жду код.")

    task = asyncio.create_task(
        _watch_login_code(
            message=query.message,
            secrets=secrets,
            settings=settings,
            db_session_factory=db_session_factory,
            identity=identity,
        )
    )
    _login_code_tasks.add(task)
    task.add_done_callback(_login_code_tasks.discard)


async def _watch_login_code(
    *,
    message: Message,
    secrets: AccountSecrets,
    settings: Settings,
    db_session_factory: async_sessionmaker[AsyncSession],
    identity: IdentityInfo,
) -> None:
    account_id = secrets.account.id
    try:
        async with account_client(
            secrets=secrets,
            api_id=settings.pyrogram_api_id,
            api_hash=settings.pyrogram_api_hash.get_secret_value(),
            prefix="login-code",
        ) as client:
            started_at = datetime.now(UTC)
            known_ids = await snapshot_service_message_ids(client)
            code = await wait_for_login_code(
                client,
                known_message_ids=known_ids,
                started_at=started_at,
                timeout_seconds=180,
            )
    except Exception as exc:
        log.warning("login code watch failed account_id=%s error=%s", account_id, type(exc).__name__)
        await _audit_login_code_result(
            db_session_factory,
            identity=identity,
            message=message,
            account_id=account_id,
            result=AuditResult.FAILED,
            error_code=type(exc).__name__,
        )
        await message.answer(f"Код не получил: <code>{type(exc).__name__}</code>", parse_mode="HTML")
        return

    if code is None:
        await _audit_login_code_result(
            db_session_factory,
            identity=identity,
            message=message,
            account_id=account_id,
            result=AuditResult.FAILED,
            error_code="LOGIN_CODE_TIMEOUT",
        )
        await message.answer("Код не пришёл за 3 минуты.")
        return

    await _audit_login_code_result(
        db_session_factory,
        identity=identity,
        message=message,
        account_id=account_id,
        result=AuditResult.OK,
        error_code=None,
    )
    await message.answer(f"Код: <code>{code}</code>", parse_mode="HTML")


async def _audit_login_code_result(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    identity: IdentityInfo,
    message: Message,
    account_id: uuid.UUID,
    result: AuditResult,
    error_code: str | None,
) -> None:
    async with db_session_factory() as session:
        await AuditService(session).record_now(
            action="account.login_code.result",
            request_id=f"login-code-result-{account_id}-{uuid.uuid4()}",
            result=result,
            actor_tg_id=identity.tg_user_id,
            role=identity.role,
            account_id=account_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
            error_code=error_code,
        )
        await session.commit()
