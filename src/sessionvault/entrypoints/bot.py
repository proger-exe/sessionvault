"""CLI-точка входа для бота: `sessionvault-bot`."""

from __future__ import annotations

import asyncio

from sessionvault.app import run
from sessionvault.logging import setup_logging


def main() -> None:
    setup_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
