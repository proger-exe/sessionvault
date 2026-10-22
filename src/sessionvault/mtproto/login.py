"""Kurigram-обёртка над login-flow.

Ключевой инвариант из Этапа 0: **один живой `Client` на всю FSM login-flow**.
Между `send_code` и `sign_in` нельзя делать `disconnect()` — Telegram кидает
`PHONE_CODE_EXPIRED` даже на свежий код.

Поэтому воркер `mtproto_worker.py` создаёт объект `LoginSession` при
входящей команде «add-account» и держит его в оперативной памяти до
завершения FSM (успех / отмена / TTL).

Ничего не пишем на диск: сессия только в памяти (`in_memory=True`). Как только
получили auth_key + метаданные, воркер возвращает их доменному сервису и
уничтожает клиента.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

from pyrogram import Client  # type: ignore[attr-defined]
from pyrogram.errors import (
    FloodWait,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberBanned,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
)
from pyrogram.errors.exceptions.bad_request_400 import PasswordHashInvalid
from pyrogram.errors.rpc_error import RPCError

from sessionvault.mtproto.errors import LoginError, LoginErrorKind


@dataclass
class ExtractedSession:
    """Всё, что нужно для сохранения аккаунта после успешной авторизации."""

    auth_key: bytes
    dc_id: int
    user_id: int
    is_bot: bool
    first_name: str | None
    last_name: str | None
    username: str | None
    has_2fa: bool


@dataclass
class SentCodeInfo:
    phone_code_hash: str
    code_type: str
    next_type: str | None
    timeout: int | None


class LoginSession:
    """Один экземпляр = одна FSM. Держит живой Kurigram-клиент."""

    def __init__(self, *, api_id: int, api_hash: str, phone: str) -> None:
        self._client = Client(
            name=f"login-{phone}",
            api_id=api_id,
            api_hash=api_hash,
            phone_number=phone,
            in_memory=True,        # ничего не пишем на диск
            no_updates=True,
        )
        self._phone = phone
        self._sent: SentCodeInfo | None = None
        self._has_2fa = False

    async def start(self) -> SentCodeInfo:
        """Connect + send_code."""
        await self._client.connect()
        try:
            sent = await self._client.send_code(self._phone)
        except FloodWait as e:
            await self._safe_disconnect()
            raise LoginError(
                kind=LoginErrorKind.FLOOD_WAIT,
                code="FLOOD_WAIT",
                retry_after=e.value,
            ) from e
        except PhoneNumberInvalid as e:
            await self._safe_disconnect()
            raise LoginError(
                kind=LoginErrorKind.PHONE_NUMBER_INVALID,
                code="PHONE_NUMBER_INVALID",
            ) from e
        except PhoneNumberBanned as e:
            await self._safe_disconnect()
            raise LoginError(
                kind=LoginErrorKind.PHONE_NUMBER_BANNED,
                code="PHONE_NUMBER_BANNED",
            ) from e
        except RPCError as e:
            await self._safe_disconnect()
            raise _wrap_other(e) from e
        self._sent = SentCodeInfo(
            phone_code_hash=sent.phone_code_hash,
            code_type=str(sent.type),
            next_type=str(sent.next_type) if sent.next_type else None,
            timeout=getattr(sent, "timeout", None),
        )
        return self._sent

    async def submit_code(self, code: str) -> ExtractedSession | None:
        """Вернёт ExtractedSession при успехе; None если требуется 2FA."""
        if self._sent is None:
            raise RuntimeError("LoginSession.start() не был вызван")
        try:
            await self._client.sign_in(self._phone, self._sent.phone_code_hash, code)
        except SessionPasswordNeeded:
            self._has_2fa = True
            return None
        except PhoneCodeInvalid as e:
            raise LoginError(kind=LoginErrorKind.PHONE_CODE_INVALID, code="PHONE_CODE_INVALID") from e
        except PhoneCodeExpired as e:
            raise LoginError(kind=LoginErrorKind.PHONE_CODE_EXPIRED, code="PHONE_CODE_EXPIRED") from e
        except FloodWait as e:
            raise LoginError(
                kind=LoginErrorKind.FLOOD_WAIT,
                code="FLOOD_WAIT",
                retry_after=e.value,
            ) from e
        except RPCError as e:
            raise _wrap_other(e) from e
        return await self._extract()

    async def submit_password(self, password: str) -> ExtractedSession:
        try:
            await self._client.check_password(password)
        except PasswordHashInvalid as e:
            raise LoginError(kind=LoginErrorKind.PASSWORD_HASH_INVALID, code="PASSWORD_HASH_INVALID") from e
        except FloodWait as e:
            raise LoginError(
                kind=LoginErrorKind.FLOOD_WAIT,
                code="FLOOD_WAIT",
                retry_after=e.value,
            ) from e
        except RPCError as e:
            raise _wrap_other(e) from e
        return await self._extract()

    async def cancel(self) -> None:
        await self._safe_disconnect()

    async def _extract(self) -> ExtractedSession:
        try:
            me = await self._client.get_me()
            storage = self._client.storage
            auth_key = await storage.auth_key()
            dc_id = await storage.dc_id()
        finally:
            await self._safe_disconnect()

        return ExtractedSession(
            auth_key=auth_key,
            dc_id=dc_id,
            user_id=me.id,
            is_bot=bool(me.is_bot),
            first_name=me.first_name,
            last_name=me.last_name,
            username=me.username,
            has_2fa=self._has_2fa,
        )

    async def _safe_disconnect(self) -> None:
        with suppress(Exception):
            await self._client.disconnect()


def _wrap_other(exc: RPCError) -> LoginError:
    return LoginError(
        kind=LoginErrorKind.OTHER,
        code=getattr(exc, "ID", type(exc).__name__),
        detail=str(exc),
    )
