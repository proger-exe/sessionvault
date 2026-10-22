"""Temporary Kurigram client from encrypted account credentials."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from pyrogram import Client  # type: ignore[attr-defined]

from sessionvault.domain.accounts.service import AccountSecrets
from sessionvault.mtproto.session_file import build_pyrogram_session_file


@dataclass(frozen=True)
class TempSessionPaths:
    root: Path
    name: Path
    file: Path


@asynccontextmanager
async def account_client(
    *,
    secrets: AccountSecrets,
    api_id: int,
    api_hash: str,
    prefix: str,
    tmp_dir: Path | None = None,
) -> AsyncIterator[Client]:
    """Build, open and remove a temporary `.session` for one account operation."""
    root = Path(
        tempfile.mkdtemp(
            prefix=f"sessionvault-{prefix}-",
            dir=str(tmp_dir) if tmp_dir is not None else None,
        )
    )
    session_name = root / "account"
    session_file = session_name.with_suffix(".session")
    account = secrets.account
    if account.dc_id is None or account.tg_user_id is None:
        shutil.rmtree(root, ignore_errors=True)
        raise ValueError("Account has no dc_id/tg_user_id metadata")

    build_pyrogram_session_file(
        session_file,
        auth_key=secrets.auth_key,
        dc_id=account.dc_id,
        api_id=api_id,
        user_id=account.tg_user_id,
    )
    client = Client(name=str(session_name), api_id=api_id, api_hash=api_hash, no_updates=True)
    await client.start()
    try:
        yield client
    finally:
        await client.stop()
        shutil.rmtree(root, ignore_errors=True)
