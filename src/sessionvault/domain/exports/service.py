"""Encrypted Pyrogram `.session` exports."""

from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from sessionvault.config import Settings
from sessionvault.domain.accounts import AccountsService
from sessionvault.domain.audit import AuditService
from sessionvault.mtproto.account_client import account_client
from sessionvault.mtproto.health import get_self
from sessionvault.mtproto.session_file import SESSION_VERSION, build_pyrogram_session_file
from sessionvault.security.envelope import encrypt_field
from sessionvault.security.key_provider import KeyProvider
from sessionvault.security.locks import AccountLock
from sessionvault.storage.db.models import (
    AuditResult,
    Export,
    ExportFormat,
    ExportState,
    UserRole,
)


@dataclass(frozen=True)
class ExportArtifact:
    export_id: uuid.UUID
    path: Path
    archive_key_b64: str
    filename: str


class ExportService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        key_provider: KeyProvider,
        redis: Redis[bytes],
    ) -> None:
        self._session = session
        self._settings = settings
        self._key_provider = key_provider
        self._redis = redis

    async def create_export(
        self,
        *,
        account_id: uuid.UUID,
        format: ExportFormat,
        operator_tg_id: int,
    ) -> Export:
        export = Export(account_id=account_id, format=format, operator_tg_id=operator_tg_id)
        self._session.add(export)
        await self._session.flush()
        return export

    async def build_pyrogram_export(
        self,
        *,
        export_id: uuid.UUID,
        actor_tg_id: int,
        role: UserRole,
    ) -> ExportArtifact:
        export = await self._session.get(Export, export_id)
        if export is None:
            raise LookupError(f"export not found: {export_id}")
        if export.format != ExportFormat.PYROGRAM_SESSION:
            raise ValueError(f"unsupported export format: {export.format.value}")

        accounts = AccountsService(self._session, self._key_provider)
        secrets = await accounts.get_secrets(export.account_id)
        if secrets is None:
            export.state = ExportState.FAILED
            export.error_code = "ACCOUNT_NOT_FOUND"
            await self._session.flush()
            raise LookupError("account secrets not found")

        async with AccountLock(
            self._redis,
            account_id=str(export.account_id),
            holder=f"export:{export_id}",
        ) as lock:
            if not lock.acquired:
                export.state = ExportState.FAILED
                export.error_code = "ACCOUNT_LOCKED"
                await self._session.flush()
                raise RuntimeError("account is locked")

            export.state = ExportState.BUILDING
            await self._session.flush()

            root = Path(tempfile.mkdtemp(prefix=f"sessionvault-export-{export_id}-"))
            try:
                session_file = root / "pyrogram.session"
                account = secrets.account
                if account.dc_id is None or account.tg_user_id is None:
                    raise ValueError("account has no dc_id/tg_user_id metadata")

                build_pyrogram_session_file(
                    session_file,
                    auth_key=secrets.auth_key,
                    dc_id=account.dc_id,
                    api_id=self._settings.pyrogram_api_id,
                    user_id=account.tg_user_id,
                )
                async with account_client(
                    secrets=secrets,
                    api_id=self._settings.pyrogram_api_id,
                    api_hash=self._settings.pyrogram_api_hash.get_secret_value(),
                    prefix="export-verify",
                ) as client:
                    verified = await get_self(client)

                manifest = {
                    "format": ExportFormat.PYROGRAM_SESSION.value,
                    "session_version": SESSION_VERSION,
                    "mtproto_library": "kurigram",
                    "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "account_id": str(export.account_id),
                    "verified_user_id": verified.tg_user_id,
                    "verification": "get_me",
                }
                zip_path = root / "payload.zip"
                with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.write(session_file, "pyrogram.session")
                    zf.writestr("manifest.json", json.dumps(manifest, indent=2))

                archive_key = AESGCM.generate_key(bit_length=256)
                nonce = os.urandom(12)
                encrypted = nonce + AESGCM(archive_key).encrypt(
                    nonce,
                    zip_path.read_bytes(),
                    str(export_id).encode(),
                )
                out_dir = Path(self._settings.export_tmp_dir)
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"{export_id}.session.zip.enc"
                out_path.write_bytes(encrypted)

                export.state = ExportState.READY
                export.local_path = str(out_path)
                export.archive_key_ciphertext = encrypt_field(
                    archive_key,
                    dek=secrets.dek,
                    account_id=str(export.account_id),
                    field=f"export_key:{export_id}",
                )
                export.manifest = manifest
                await AuditService(self._session).record_now(
                    action="export.create",
                    request_id=f"export-create-{export_id}-{uuid.uuid4()}",
                    result=AuditResult.OK,
                    actor_tg_id=actor_tg_id,
                    role=role,
                    account_id=export.account_id,
                    changed_fields=["export"],
                )
                await self._session.flush()
                return ExportArtifact(
                    export_id=export.id,
                    path=out_path,
                    archive_key_b64=base64.b64encode(archive_key).decode(),
                    filename=out_path.name,
                )
            except Exception as exc:
                export.state = ExportState.FAILED
                export.error_code = type(exc).__name__
                await AuditService(self._session).record_now(
                    action="export.create",
                    request_id=f"export-fail-{export_id}-{uuid.uuid4()}",
                    result=AuditResult.FAILED,
                    actor_tg_id=actor_tg_id,
                    role=role,
                    account_id=export.account_id,
                    error_code=export.error_code,
                )
                await self._session.flush()
                raise
            finally:
                shutil.rmtree(root, ignore_errors=True)

    async def mark_sent(self, *, export_id: uuid.UUID, actor_tg_id: int, role: UserRole) -> None:
        export = await self._session.get(Export, export_id)
        if export is None:
            return
        local_path = export.local_path
        if local_path:
            Path(local_path).unlink(missing_ok=True)
        export.local_path = None
        export.state = ExportState.SENT
        export.sent_at = datetime.now(UTC)
        await AuditService(self._session).record_now(
            action="export.send",
            request_id=f"export-send-{export_id}-{uuid.uuid4()}",
            result=AuditResult.OK,
            actor_tg_id=actor_tg_id,
            role=role,
            account_id=export.account_id,
            changed_fields=["export"],
        )
        await self._session.flush()
