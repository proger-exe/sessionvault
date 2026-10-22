"""2FA (cloud password) and recovery-email operations via Kurigram."""

from __future__ import annotations

from dataclasses import dataclass

from pyrogram import Client  # type: ignore[attr-defined]
from pyrogram.errors.rpc_error import RPCError
from pyrogram.raw.functions.account import (  # type: ignore[attr-defined]
    ConfirmPasswordEmail,
    GetPassword,
)

from sessionvault.mtproto.profile_ops import ProfileOperationError, normalize_rpc_error


@dataclass(frozen=True)
class PasswordInfo:
    has_password: bool
    has_recovery_email: bool
    email_unconfirmed_pattern: str  # e.g. "a***@b.com"; empty if none pending
    hint: str


async def get_password_info(client: Client) -> PasswordInfo:
    try:
        pw = await client.invoke(GetPassword())
    except RPCError as exc:
        raise normalize_rpc_error(exc) from exc
    return PasswordInfo(
        has_password=bool(getattr(pw, "has_password", False)),
        has_recovery_email=bool(getattr(pw, "has_recovery", False)),
        email_unconfirmed_pattern=str(getattr(pw, "email_unconfirmed_pattern", "") or ""),
        hint=str(getattr(pw, "hint", "") or ""),
    )


async def enable_2fa(client: Client, *, password: str, hint: str = "", email: str | None = None) -> None:
    """Enable cloud password. If email is given, Telegram will send a verify code."""
    try:
        await client.enable_cloud_password(password=password, hint=hint, email=email or "")  # type: ignore[arg-type]
    except RPCError as exc:
        raise normalize_rpc_error(exc) from exc


async def change_2fa(client: Client, *, current_password: str, new_password: str, new_hint: str = "") -> None:
    try:
        await client.change_cloud_password(
            current_password=current_password,
            new_password=new_password,
            new_hint=new_hint,
        )
    except RPCError as exc:
        raise normalize_rpc_error(exc) from exc


async def remove_2fa(client: Client, *, current_password: str) -> None:
    try:
        await client.remove_cloud_password(password=current_password)
    except RPCError as exc:
        raise normalize_rpc_error(exc) from exc


async def confirm_email_code(client: Client, *, code: str) -> None:
    try:
        await client.invoke(ConfirmPasswordEmail(code=code))
    except RPCError as exc:
        raise normalize_rpc_error(exc) from exc


def is_email_unconfirmed_error(err: ProfileOperationError) -> bool:
    """Telegram signals successful enable with pending email verification."""
    return err.code.startswith("EMAIL_UNCONFIRMED") or err.code == "EMAIL_NOT_SETUP"
