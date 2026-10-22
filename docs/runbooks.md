# Runbooks

## Session Compromise

1. Stop bot and worker.
2. Revoke the affected Telegram session from Telegram official clients.
3. Archive the local account record or reconnect it through `/add_account`.
4. Review `/audit` for exports and profile/security operations.
5. Rotate `MASTER_KEY` if host/env compromise is suspected.

## Master Key Rotation

1. Generate a new base64 32-byte key.
2. Add it as `MASTER_KEY_V2`.
3. Restart bot/worker.
4. Let reads lazily rewrap DEKs.
5. After all accounts have been touched, promote v2 operationally and keep v1 only until backup retention expires.

## Backup / Restore

1. Stop worker during backup if strict consistency is required.
2. `pg_dump` Postgres.
3. Encrypt backup outside the project tree.
4. Restore into a dry-run DB first.
5. Run `alembic upgrade head` and health-check a test account.
