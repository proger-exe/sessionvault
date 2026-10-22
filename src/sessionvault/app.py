"""Сборка приложения: Container, Dispatcher, middleware, routers."""

from __future__ import annotations

import logging

from sessionvault.bot.account_card import router as account_card_router
from sessionvault.bot.accounts_list import router as accounts_list_router
from sessionvault.bot.admins import router as admins_router
from sessionvault.bot.audit import router as audit_router
from sessionvault.bot.flows.add_account import router as add_account_router
from sessionvault.bot.flows.edit_profile import router as edit_profile_router
from sessionvault.bot.flows.export import router as export_router
from sessionvault.bot.flows.import_panel_session import router as import_panel_session_router
from sessionvault.bot.flows.import_session import router as import_session_router
from sessionvault.bot.flows.profile_extras import router as profile_extras_router
from sessionvault.bot.flows.security import router as security_router
from sessionvault.bot.main_menu import router as main_menu_router
from sessionvault.bot.middlewares.auth import AuthMiddleware
from sessionvault.di import Container, build_container
from sessionvault.storage.db.session import get_session_factory

log = logging.getLogger(__name__)


def build_app() -> Container:
    container = build_container()
    dp = container.dispatcher

    auth_mw = AuthMiddleware(
        settings=container.settings,
        session_factory=get_session_factory(),
    )
    dp.message.middleware(auth_mw)
    dp.callback_query.middleware(auth_mw)

    dp.include_router(main_menu_router)
    dp.include_router(add_account_router)
    dp.include_router(import_session_router)
    dp.include_router(import_panel_session_router)
    dp.include_router(edit_profile_router)
    dp.include_router(profile_extras_router)
    dp.include_router(security_router)
    dp.include_router(export_router)
    dp.include_router(accounts_list_router)
    dp.include_router(account_card_router)
    dp.include_router(admins_router)
    dp.include_router(audit_router)

    log.info(
        "app собран: routers=%s",
        [
            "main_menu",
            "add_account",
            "import_session",
            "import_panel_session",
            "edit_profile",
            "profile_extras",
            "security",
            "export",
            "accounts_list",
            "account_card",
            "admins",
            "audit",
        ],
    )
    return container


async def run() -> None:
    container = build_app()
    try:
        await container.dispatcher.start_polling(container.bot)
    finally:
        await container.close()
