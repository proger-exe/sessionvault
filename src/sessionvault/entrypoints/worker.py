"""CLI-точка входа для arq-воркера: `sessionvault-worker`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from arq.connections import RedisSettings
from arq.worker import run_worker

from sessionvault.config import get_settings
from sessionvault.logging import setup_logging
from sessionvault.workers.jobs import (
    apply_profile_operation,
    build_pyrogram_export,
    check_account_health,
    shutdown,
    startup,
)


class WorkerSettings:
    functions: ClassVar[list[Callable[..., Any]]] = [
        check_account_health,
        apply_profile_operation,
        build_pyrogram_export,
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)


def main() -> None:
    setup_logging()
    run_worker(WorkerSettings)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
