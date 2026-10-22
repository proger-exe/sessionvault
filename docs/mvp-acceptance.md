# MVP Acceptance

- Owner/Admin access works only in private chat.
- Unknown users are ignored and audited as rejected.
- Add-account FSM handles phone -> code -> 2FA and deletes secret messages best-effort.
- Import-session FSM accepts only Pyrogram/Kurigram SQLite `.session` v3, verifies by `get_me`, persists encrypted secrets, and deletes upload temp files.
- Account session and phone are encrypted in DB.
- Account list/card shows metadata, risk and health timestamp.
- Profile name, username, bio and avatar deletion run through MTProto and audit.
- Health check calls only `get_me`.
- `.session` export builds Pyrogram schema v3, verifies by `get_me`, encrypts, sends document, deletes temp file.
- `tdata` is unavailable unless `EXPORTS_TDATA_ENABLED=true`.
- Audit contains operation facts and error codes, not secrets.
- Parallel account writes use Redis lock.

Run local checks:

```bash
.venv/bin/ruff check .
.venv/bin/mypy src
.venv/bin/pytest -q
```
