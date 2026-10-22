# SessionVault

<!-- portfolio-card:start -->
[![SessionVault — Собственные аккаунты. Шифрование и журнал действий.](docs/portfolio/card.png)](docs/portfolio/card.svg)
<!-- portfolio-card:end -->

Хранить собственные Telegram-сессии на своём сервере и обращаться к ним через личного бота. SessionVault принимает сессию один раз, сохраняет её в зашифрованном виде и даёт владельцу доступ к кодам входа, операциям с профилем и экспорту.

Сценарий рассчитан на одного владельца и личный чат. Экспорт поддерживает Pyrogram `.session`; поддержка `tdata` экспериментальная. Действия фиксируются в журнале аудита.

## Главная задача проекта

Работа с секретами затрагивает весь путь данных: хранение, фоновые задачи, логи и временные файлы экспорта. Для каждого аккаунта используется отдельный ключ шифрования, параллельные операции координируются через Redis, а экспорт удаляется после короткого срока хранения.

Эти решения и их границы описаны в `docs/threat-model.md`. Репозиторий позволяет оценить реализацию обращения с чувствительными данными внутри Telegram-приложения.

Обсудить похожий проект: [sh3l.beer](https://sh3l.beer).

## Обращение с секретами

- `security/envelope.py`: отдельный ключ данных для каждого аккаунта шифруется мастер-ключом из окружения. После добавления `MASTER_KEY_V2` воркеры постепенно перепаковывают ключи при обращении к ним.
- `security/secret_filter.py`: API hashes, коды входа, ключи авторизации и токены скрываются до передачи в обработчики логов. Фильтр покрыт unit-тестами.
- PostgreSQL-триггеры из Alembic-миграций запрещают роли приложения менять и удалять строки аудита и историю статусов.
- Блокировки Redis координируют доступ к одной MTProto-сессии из разных сценариев.
- Экспорт записывается в tmpfs, шифруется, передаётся ботом и удаляется по короткому TTL.
- Модель угроз и ограничения: `docs/threat-model.md`.

## Стек и проверки

Python 3.12, aiogram 3 + Raito, Kurigram (MTProto), SQLAlchemy 2 + Alembic, PostgreSQL, Redis + arq, `cryptography` и structlog. Настроены strict mypy, ruff и pytest; есть unit-тесты и интеграционные проверки с Postgres/Redis.

## Запуск

```bash
cp .env.example .env     # BOT_TOKEN, OWNER_ID, PYROGRAM_API_ID/HASH, MASTER_KEY
docker compose up -d --build
docker compose exec bot alembic upgrade head
```

Для разработки:

```bash
docker compose up -d postgres redis
uv sync --extra dev
uv run alembic upgrade head
uv run sessionvault-bot          # bot
uv run sessionvault-worker       # arq workers (exports, key rotation)
uv run sessionvault-listener     # MTProto listener for login codes
uv run pytest tests/unit -q
```

## Структура проекта

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

## Лицензия

MIT
