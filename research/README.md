# SessionVault — Stage 0 research

Одноразовые скрипты, которые снимают неопределённость по MTProto-стеку и форматам
экспорта. Ничего из этого кода не должно перекочевать в production напрямую.

## Цели

1. Пройти реальный вход по коду + 2FA через Pyrogram.
2. Пройти все поля профиля из ТЗ 4.3 и зафиксировать `supported`/`unsupported`.
3. Собрать `.session` из auth key + метаданных, открыть свежей Pyrogram, `get_me`.
4. Через `opentele` собрать `tdata`, запустить Telegram Desktop зафиксированной
   версии в изолированном контейнере, убедиться, что клиент авторизуется.
5. Зафиксировать выбор библиотеки, версий Telegram layer и TDesktop в
   `DECISIONS.md`.

## Setup

```fish
cd research
cp .env.example .env
# заполнить API_ID / API_HASH / TEST_PHONE
uv sync
```

## Порядок прогонов

```fish
uv run python login.py          # интерактивный вход, сохраняет auth key в _state/
uv run python profile_ops.py    # покрытие полей профиля
uv run python export_pyrogram.py  # сборка + verify .session
uv run python export_tdata.py     # сборка tdata (см. tdata-runner/ для проверки в TDesktop)
```

## Что НЕ делаем

- не логируем номер, код, пароль 2FA, auth key;
- не отправляем сообщения от имени тестового аккаунта в чужие чаты;
- не пробуем массовые операции — Telegram оштрафует `FLOOD_WAIT`.

## Состояние (не коммитим)

Всё, что создаётся во время прогона, оседает в `_state/`:

```
_state/
├── owner.session          # рабочая Pyrogram-сессия тестового аккаунта
├── owner.auth_key.bin     # сырой auth key, извлечённый из сессии
├── owner.meta.json        # dc_id, user_id, api_id — для сборки нового .session
├── export_pyrogram.session  # результат Этапа 0.3
├── tdata/                  # результат Этапа 0.4
└── profile_report.json     # результат Этапа 0.2
```
