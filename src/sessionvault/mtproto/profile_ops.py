"""Supported Telegram profile operations for MVP."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyrogram import Client  # type: ignore[attr-defined]
from pyrogram.errors.rpc_error import RPCError

from sessionvault.mtproto.health import HealthResult, get_self

UNSUPPORTED_MARKERS = (
    "METHOD_INVALID",
    "CONSTRUCTOR_INVALID",
    "LAYER_INVALID",
    "USERNAME_INVALID",
    "PRIVACY_KEY_INVALID",
    "PRIVACY_VALUE_INVALID",
)


class ProfileOperationError(Exception):
    def __init__(self, code: str, *, unsupported: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.unsupported = unsupported


@dataclass(frozen=True)
class ProfileOperationResult:
    changed_fields: list[str]
    self_profile: HealthResult


def normalize_rpc_error(exc: RPCError) -> ProfileOperationError:
    code = str(getattr(exc, "ID", type(exc).__name__))
    return ProfileOperationError(
        code,
        unsupported=any(marker in code for marker in UNSUPPORTED_MARKERS),
    )


async def apply_profile_operation(
    client: Client,
    *,
    operation_type: str,
    payload: dict[str, Any],
) -> ProfileOperationResult:
    """Apply one supported operation and verify it by reading own profile."""
    try:
        changed = await _apply(client, operation_type=operation_type, payload=payload)
        return ProfileOperationResult(changed_fields=changed, self_profile=await get_self(client))
    except RPCError as exc:
        raise normalize_rpc_error(exc) from exc


async def _apply(
    client: Client,
    *,
    operation_type: str,
    payload: dict[str, Any],
) -> list[str]:
    if operation_type == "profile.name":
        await client.update_profile(
            first_name=str(payload.get("first_name") or ""),
            last_name=str(payload.get("last_name") or ""),
        )
        return ["first_name", "last_name"]

    if operation_type == "profile.username":
        username = payload.get("username")
        await client.set_username(str(username) if username else None)
        return ["username"]

    if operation_type == "profile.bio":
        await client.update_profile(bio=str(payload.get("bio") or ""))
        return ["bio"]

    if operation_type == "profile.photo.upload":
        path = Path(str(payload["path"]))
        uploaded = await client.set_profile_photo(photo=str(path))  # type: ignore[arg-type]
        if not uploaded:
            raise ProfileOperationError("PHOTO_UPLOAD_EMPTY")
        return ["photo"]

    if operation_type == "profile.photo.delete_by_index":
        idx = payload.get("index")
        if not isinstance(idx, int) or idx < 0:
            raise ProfileOperationError("PHOTO_INDEX_INVALID")
        photo_iter = client.get_chat_photos("me", limit=100)
        photos = [p async for p in photo_iter] if photo_iter is not None else []
        if idx >= len(photos):
            raise ProfileOperationError("PHOTO_INDEX_OUT_OF_RANGE")
        await client.delete_profile_photos([photos[idx].file_id])
        return ["photo"]

    if operation_type == "profile.photo.delete_all":
        photo_iter = client.get_chat_photos("me", limit=100)
        photos = [p async for p in photo_iter] if photo_iter is not None else []
        if photos:
            await client.delete_profile_photos([p.file_id for p in photos])
        return ["photo"]

    if operation_type == "profile.audio.upload":
        path = Path(str(payload["path"]))
        uploaded = await client.add_profile_audio(  # type: ignore[attr-defined]
            audio=str(path),
            duration=int(payload.get("duration") or 0),
            performer=payload.get("performer") or None,
            title=payload.get("title") or None,
            file_name=payload.get("file_name") or None,
        )
        if not uploaded:
            raise ProfileOperationError("AUDIO_UPLOAD_EMPTY")
        return ["profile_audio"]

    if operation_type == "profile.birthday.set":
        day = payload.get("day")
        month = payload.get("month")
        if not isinstance(day, int) or not isinstance(month, int):
            raise ProfileOperationError("BIRTHDAY_INVALID")
        year = payload.get("year")
        await client.update_birthday(day=day, month=month, year=year if isinstance(year, int) else None)  # type: ignore[arg-type]
        return ["birthday"]

    if operation_type == "profile.birthday.clear":
        await client.update_birthday()
        return ["birthday"]

    if operation_type == "profile.personal_channel.set":
        chat = payload.get("chat")
        if not isinstance(chat, (int, str)) or chat == "":
            raise ProfileOperationError("PERSONAL_CHANNEL_INVALID")
        await client.set_personal_channel(chat_id=chat)
        return ["personal_channel"]

    if operation_type == "profile.personal_channel.clear":
        await client.set_personal_channel(chat_id=None)
        return ["personal_channel"]

    raise ProfileOperationError("UNSUPPORTED_OPERATION", unsupported=True)
