# Threat Model

## Assets

- Telegram auth keys and generated session files.
- Phone numbers, login codes, 2FA passwords, recovery email.
- `MASTER_KEY` / `MASTER_KEY_V2`.
- Owner/Admin Telegram identity.

## Controls

- Private-chat-only bot access.
- Fixed Owner/Admin roles; Owner comes from `OWNER_ID`.
- Envelope encryption: per-account DEK wrapped by master key.
- Secret filter on root logger.
- Append-only audit trigger.
- HMAC signatures for dangerous callback confirmations.
- Redis account locks for write operations.
- Export temp files in tmpfs and deleted after send/error.

## Accepted MVP Risks

- `MASTER_KEY` is env-based, not Vault/KMS.
- Bot sends archive key in the private chat caption.
- Live Telegram verification needs real operator-owned accounts.
