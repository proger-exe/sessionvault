"""Phone-number change operations (two-step: send code, confirm)."""

from __future__ import annotations

from dataclasses import dataclass

from pyrogram import Client  # type: ignore[attr-defined]
from pyrogram.errors.rpc_error import RPCError
from pyrogram.raw.functions.account import (  # type: ignore[attr-defined]
    ChangePhone,
    SendChangePhoneCode,
)
from pyrogram.raw.types import CodeSettings  # type: ignore[attr-defined]

from sessionvault.mtproto.profile_ops import normalize_rpc_error


@dataclass(frozen=True)
class ChangePhoneCodeSent:
    phone_code_hash: str
    type_name: str  # "app" | "sms" | "call" | "unknown"


def _sent_code_type(sent_code: object) -> str:
    type_obj = getattr(sent_code, "type", None)
    if type_obj is None:
        return "unknown"
    name = type(type_obj).__name__.lower()
    # Longest matches first: 'call' would swallow 'flashcall'/'missedcall',
    # and 'sms' would swallow 'firebasesms'.
    for label in ("firebasesms", "flashcall", "missedcall", "email", "app", "sms", "call"):
        if label in name:
            return label
    return "unknown"


async def send_change_phone_code(client: Client, *, new_phone_e164: str) -> ChangePhoneCodeSent:
    plain = new_phone_e164.lstrip("+")
    try:
        sent = await client.invoke(
            SendChangePhoneCode(phone_number=plain, settings=CodeSettings())
        )
    except RPCError as exc:
        raise normalize_rpc_error(exc) from exc
    return ChangePhoneCodeSent(
        phone_code_hash=str(getattr(sent, "phone_code_hash", "")),
        type_name=_sent_code_type(sent),
    )


async def confirm_change_phone(
    client: Client,
    *,
    new_phone_e164: str,
    phone_code_hash: str,
    phone_code: str,
) -> int:
    """Apply the phone-number change. Returns the account's tg user id."""
    plain = new_phone_e164.lstrip("+")
    try:
        user = await client.invoke(
            ChangePhone(
                phone_number=plain,
                phone_code_hash=phone_code_hash,
                phone_code=phone_code,
            )
        )
    except RPCError as exc:
        raise normalize_rpc_error(exc) from exc
    return int(getattr(user, "id", 0))
