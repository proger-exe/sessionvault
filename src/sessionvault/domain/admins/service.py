"""Управление пользователями бота: Owner + список Admin.

Owner единственный: его `tg_user_id` = `Settings.owner_id` из `.env`. Запись
в таблице `users` создаётся при первом входе для консистентности FK-ов
(actor_id ссылается на users через tg_user_id).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionvault.config import Settings
from sessionvault.storage.db.models import User, UserRole


@dataclass(frozen=True)
class IdentityInfo:
    tg_user_id: int
    role: UserRole
    is_active: bool


class AdminService:
    """CRUD для пользователей бота + проверки доступа.

    Не сталкивается с aiogram напрямую: `AuthMiddleware` вызывает
    `identify()` и получает роль или `None` (rejected).
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def identify(self, tg_user_id: int) -> IdentityInfo | None:
        """Определяет роль пользователя.

        Owner берётся из `.env`; при первом входе Owner-запись создаётся в БД,
        чтобы FK на audit-события работал.
        """
        if tg_user_id == self._settings.owner_id:
            user = await self._get_or_create_owner(tg_user_id)
            return IdentityInfo(
                tg_user_id=user.tg_user_id,
                role=user.role,
                is_active=user.is_active,
            )

        stmt = select(User).where(User.tg_user_id == tg_user_id, User.role == UserRole.ADMIN)
        admin_user = (await self._session.execute(stmt)).scalar_one_or_none()
        if admin_user is None or not admin_user.is_active:
            return None
        return IdentityInfo(
            tg_user_id=admin_user.tg_user_id,
            role=admin_user.role,
            is_active=admin_user.is_active,
        )

    async def _get_or_create_owner(self, tg_user_id: int) -> User:
        stmt = select(User).where(User.tg_user_id == tg_user_id)
        user = (await self._session.execute(stmt)).scalar_one_or_none()
        if user is None:
            user = User(tg_user_id=tg_user_id, role=UserRole.OWNER, is_active=True)
            self._session.add(user)
            await self._session.flush()
        elif user.role != UserRole.OWNER:
            # Owner из .env всегда побеждает: чинит роль, если её кто-то сдвинул.
            user.role = UserRole.OWNER
            user.is_active = True
            await self._session.flush()
        assert user is not None
        return user

    async def list_admins(self) -> list[User]:
        stmt = select(User).where(User.role == UserRole.ADMIN).order_by(User.created_at)
        return list((await self._session.execute(stmt)).scalars())

    async def add_admin(
        self,
        *,
        tg_user_id: int,
        display_name: str | None,
        added_by_tg_id: int,
    ) -> User:
        if tg_user_id == self._settings.owner_id:
            raise ValueError("Owner уже владеет системой — назначать Admin-ом не надо.")

        existing = (
            await self._session.execute(select(User).where(User.tg_user_id == tg_user_id))
        ).scalar_one_or_none()
        if existing is not None:
            existing.role = UserRole.ADMIN
            existing.is_active = True
            existing.display_name = display_name or existing.display_name
            existing.added_by_tg_id = added_by_tg_id
            await self._session.flush()
            return existing

        user = User(
            tg_user_id=tg_user_id,
            role=UserRole.ADMIN,
            display_name=display_name,
            added_by_tg_id=added_by_tg_id,
            is_active=True,
        )
        self._session.add(user)
        await self._session.flush()
        return user

    async def deactivate_admin(self, tg_user_id: int) -> bool:
        stmt = select(User).where(User.tg_user_id == tg_user_id, User.role == UserRole.ADMIN)
        user = (await self._session.execute(stmt)).scalar_one_or_none()
        if user is None:
            return False
        user.is_active = False
        await self._session.flush()
        return True
