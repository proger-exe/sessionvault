# SessionVault

<!-- portfolio-card:start -->
[![SessionVault — Собственные аккаунты. Шифрование и журнал действий.](docs/portfolio/card.png)](docs/portfolio/card.svg)
<!-- portfolio-card:end -->

Self-hosted vault for your own Telegram accounts. Sessions are imported once, stored encrypted, and handed out on demand through a private Telegram bot: login codes, profile operations, exports (Pyrogram `.session`, experimental `tdata`). One owner, no groups, every action in an append-only audit log.

Built as an exercise in doing secret handling properly inside an ordinary aiogram project.

## Security model

- **Envelope encryption** (`security/envelope.py`): each account has its own data-encryption key, wrapped by a master key from the environment. Rotation is lazy: set `MASTER_KEY_V2` and workers re-wrap keys as they touch them.
- **Secret-filtering logger** (`security/secret_filter.py`): api hashes, login codes, auth keys and tokens are redacted before they reach any handler; covered by unit tests.
- **Append-only audit** (Alembic migration with Postgres triggers): audit rows and status history cannot be updated or deleted by the application role.
- **Per-account locks** in Redis so two flows never drive the same MTProto session at once.
- **Exports** are written to a tmpfs, encrypted, delivered by the bot and deleted after a short TTL.
- `docs/threat-model.md` lists what the design does and does not protect against.

## Stack

Python 3.12, aiogram 3 + Raito, Kurigram (MTProto), SQLAlchemy 2 + Alembic on PostgreSQL, Redis + arq workers, `cryptography`, structlog. Strict mypy, ruff, pytest (55 unit tests, integration tests against Postgres/Redis).

## Run

```bash
cp .env.example .env     # BOT_TOKEN, OWNER_ID, PYROGRAM_API_ID/HASH, MASTER_KEY
docker compose up -d --build
docker compose exec bot alembic upgrade head
```

Dev loop:

```bash
docker compose up -d postgres redis
uv sync --extra dev
uv run alembic upgrade head
uv run sessionvault-bot          # bot
uv run sessionvault-worker       # arq workers (exports, key rotation)
uv run sessionvault-listener     # MTProto listener for login codes
uv run pytest tests/unit -q
```

## Layout

```
src/sessionvault/
├── app.py          Raito app factory
├── config.py       pydantic settings
├── di.py           DI container
├── bot/            routers + FSM (import session, login, export, profile)
├── domain/         accounts, login flows, exports
├── mtproto/        Kurigram wrappers
├── workers/        arq tasks
├── security/       envelope, key_provider, locks, secret_filter
├── storage/        SQLAlchemy models, Redis
└── entrypoints/    bot / worker / listener CLIs
docs/               threat model, runbooks, deploy, bot map, spec (RU)
research/           stage-0 notes on session formats and tdata
```

## License

MIT
