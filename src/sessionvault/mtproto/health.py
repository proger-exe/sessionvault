"""Read-only session health checks."""

from __future__ import annotations

from dataclasses import dataclass

from pyrogram import Client  # type: ignore[attr-defined]


@dataclass(frozen=True)
class HealthResult:
    tg_user_id: int
    first_name: str | None
    last_name: str | None
    username: str | None
    dc_id: int | None


async def get_self(client: Client) -> HealthResult:
    """Minimal allowed health operation: read own profile."""
    me = await client.get_me()
    return HealthResult(
        tg_user_id=me.id,
        first_name=me.first_name,
        last_name=me.last_name,
        username=me.username,
        dc_id=me.dc_id,
    )
