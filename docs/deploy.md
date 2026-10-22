# Deploy

## Environment

Copy `.env.example` to `.env` and fill:

- `BOT_TOKEN`, `OWNER_ID`
- `PYROGRAM_API_ID`, `PYROGRAM_API_HASH`
- `MASTER_KEY`: base64 encoded 32-byte key
- `DATABASE_URL`, `REDIS_URL`

Generate `MASTER_KEY`:

```bash
python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

## Local Run

```bash
docker compose up -d postgres redis
uv sync
uv run alembic upgrade head
uv run sessionvault-bot
```

Worker:

```bash
uv run sessionvault-worker
```

## Compose Run

```bash
docker compose up -d --build
docker compose exec bot alembic upgrade head
docker compose logs -f bot worker
```

The bot is the only UI. No public HTTP API is exposed.
