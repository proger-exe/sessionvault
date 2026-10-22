# Bot Map

## Commands

- `/start`: main menu, account counts, active operations.
- `/health`: bot runtime health.
- `/accounts`: paginated account registry.
- `/add_account`: FSM phone -> code -> 2FA -> encrypted save.
- `/import_session`: FSM phone -> Pyrogram/Kurigram `.session` upload -> verify -> encrypted save.
- `/admins`: Owner-only admin list/add/remove.
- `/audit`: latest audit events.

## Callback Sections

- `menu:*`: main navigation.
- `menu:import_session`: start `.session` import.
- `acc:*`: account pages and open card.
- `card:profile`: profile edit menu.
- `card:export`: signed `.session` export confirmation.
- `card:security`: 2FA/recovery placeholder, intentionally unavailable in MVP.
- `prof:*`: name, username, bio, avatar deletion.
- `cfx:export`: HMAC-signed export confirmation.
- `cfx:admin_remove`: HMAC-signed admin removal.

Callback data contains only IDs/actions/signatures. Phone, code, password,
auth key, archive key and session bytes are never placed in callback data.
