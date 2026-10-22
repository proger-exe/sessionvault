"""Active Telegram sessions: list and revoke."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pyrogram import Client  # type: ignore[attr-defined]
from pyrogram.errors.rpc_error import RPCError

from sessionvault.mtproto.profile_ops import normalize_rpc_error


@dataclass(frozen=True)
class SessionInfo:
    id: int
    is_current: bool
    device_model: str
    platform: str
    app_name: str
    ip: str
    country: str
    last_active_at: datetime | None


def _ts(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int):
        return datetime.fromtimestamp(value, tz=UTC)
    return None


async def list_sessions(client: Client) -> list[SessionInfo]:
    try:
        active = await client.get_active_sessions()
    except RPCError as exc:
        raise normalize_rpc_error(exc) from exc
    out: list[SessionInfo] = []
    for s in getattr(active, "active_sessions", None) or []:
        out.append(
            SessionInfo(
                id=int(getattr(s, "id", 0) or 0),
                is_current=bool(getattr(s, "is_current", False)),
                device_model=str(getattr(s, "device_model", "") or ""),
                platform=str(getattr(s, "platform", "") or ""),
                app_name=str(getattr(s, "application_name", "") or getattr(s, "app_name", "") or ""),
                ip=str(getattr(s, "ip", "") or getattr(s, "ip_address", "") or ""),
                country=str(getattr(s, "country", "") or ""),
                last_active_at=_ts(getattr(s, "last_active_date", None) or getattr(s, "date_active", None)),
            )
        )
    return out


async def revoke_session(client: Client, *, session_id: int) -> None:
    try:
        await client.reset_session(id=session_id)
    except RPCError as exc:
        raise normalize_rpc_error(exc) from exc
