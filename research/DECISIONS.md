# Stage 0 — DECISIONS

Итоги исследовательского прототипа. Прогнано на живом тестовом аккаунте
`user_id=7910099701`, DC1, 2026-07-17.

Всё, что здесь под «Зафиксированные версии», уходит потом в
`docs/compatibility-matrix.md`.

## Зафиксированные версии

| Компонент               | Значение | Как получено |
| ----------------------- | -------- | ------------ |
| MTProto-библиотека      | **Kurigram** (fork Pyrogram, namespace `pyrogram`) | `pyproject.toml`: `kurigram>=2.2.0` |
| Версия Kurigram         | 2.2.24   | `owner.meta.json.pyrogram_version` |
| Формат `.session`       | 3        | `SESSION_VERSION` в `export_pyrogram.py`, совместим с upstream Pyrogram |
| opentele                | 1.15.1   | `pyproject.toml` |
| Telethon (тянет opentele) | 1.44.0 | `uv sync` вывод |
| Telegram Desktop        | 5.7.2    | `tdata-runner/Dockerfile` build-arg |
| Python                  | 3.12.3   | `uv sync` |

**Почему Kurigram, а не upstream Pyrogram:**
- upstream Pyrogram обновляется медленно, layer отстаёт;
- Kurigram ближе к текущему Telegram API layer (важно для `birthday`,
  `personal_channel`, приватности);
- namespace `pyrogram` сохранён → внешние тулзы (opentele) видят как обычный
  Pyrogram, session-файл переносим в оригинал при необходимости.

**Fallback:** оригинальный `pyrogram` доступен через extra `[pyrogram]` для
контрольных сверок; в production не активируем.

## Login-flow

| Пункт                                    | Итог |
| ---------------------------------------- | ---- |
| Вход по коду                             | **ok** (single-process `login.py run`) |
| Тип доставки кода на аккаунте            | `SentCodeType.APP` (в Telegram-приложение, не SMS) |
| 2FA-пароль                               | не запрашивался — на тестовом аккаунте 2FA off |
| `PHONE_CODE_EXPIRED` reproducible?       | **да** — вылетает, если между `send_code` и `sign_in` есть `disconnect()`. Решение: держим соединение открытым в одном процессе. |
| `PHONE_CODE_INVALID` reproducible?       | не тестировали |
| `SESSION_PASSWORD_NEEDED` handled?       | код-путь есть (`cmd_run` → return 10, `cmd_two_fa` через env `TG_PASSWORD`), боевой прогон отложен до тестового аккаунта с 2FA |
| `FLOOD_WAIT` seen during research        | да, в `profile_ops.py` (`FLOOD_WAIT_X` на повторном `UpdateBirthday` — ожидаемо) |

**Ключевой вывод по login-flow:** между `Client.send_code(...)` и
`Client.sign_in(...)` **нельзя** делать `disconnect()`. Telegram отвергает
`phone_code_hash` после реконнекта с `PHONE_CODE_EXPIRED`, даже если код ещё
свежий. В production login-worker обязан держать один живой Client на всю
FSM login-flow (Этап 1, п.7 плана).

## Профиль (ТЗ 4.3)

Из `_state/profile_report.json`:

| Поле                | Статус       | Примечание |
| ------------------- | ------------ | ---------- |
| first_last_name     | **ok**       | `UpdateProfile` |
| username            | **ok**       | `UpdateUsername` |
| bio                 | **ok**       | `UpdateProfile.about` |
| photo_upload_delete | **ok**       | 512×512 JPEG проходит; 1×1 PNG → `PHOTO_CROP_SIZE_SMALL` — в MVP жёстко валидировать размер ≥ 160×160 |
| birthday            | **ok**       | `functions.account.UpdateBirthday` есть в Kurigram; повторный вызов ловит `FLOOD_WAIT_X`, значит write-rate limit — ставим debounce |
| personal_channel    | **ok**       | `functions.account.UpdatePersonalChannel(channel=InputChannelEmpty)` работает; полный e2e с реальным каналом отложен на Этап 2 |
| privacy_last_seen   | **ok**       | `functions.account.SetPrivacy(InputPrivacyKeyStatusTimestamp, ...)` |

**Итог:** все семь групп полей из ТЗ 4.3 доступны на Kurigram 2.2.24.
`unsupported`-варианта в реестре пока нет; включаем реестр в
`mtproto/capabilities.py` заглушкой, чтобы бот не имитировал успех, если
Telegram в будущем ответит `METHOD_INVALID`/`LAYER_INVALID`.

**Rate-limits, замеченные на прогоне:**
- `users.GetFullUser` кидает `FLOOD_WAIT` при частых вызовах (даже 8–9 секунд
  на второй-третий подряд). → В production кэшировать full user на минуту
  минимум.

## Экспорт .session

| Пункт                                        | Итог |
| -------------------------------------------- | ---- |
| Собран `_state/export_pyrogram.session`      | **ok**, 20 480 байт |
| `verify.get_me()` вернул user_id == expected | **ok** (`7910099701`) |
| Файл открывается pinned-Kurigram             | **ok** (verify внутри `export_pyrogram.py`) |
| Файл открывается upstream-Pyrogram (cross-check) | не тестировали (fallback-extra) — включим в CI Этапа 3 |

**Формат:** Pyrogram schema, `version=3` в отдельной таблице. Никаких
форк-специфичных полей — session переносим.

## Экспорт tdata

| Пункт                                        | Итог |
| -------------------------------------------- | ---- |
| `_state/tdata/` собрался через opentele      | **ok** (`accounts=1`, `main_account_dc=1`) |
| Структура каталога                           | `D877F783D5D3EF8C/`, `D877F783D5D3EF8Cs`, `key_datas` — стандартный layout TDesktop |
| Контейнер `sessionvault-tdata-runner` собрался | **ok** (Debian 12.7-slim + Xvfb + libgtk-3 + TDesktop 5.7.2) |
| TDesktop 5.7.2 запустился с этим tdata       | **ok** — процесс жил > 25 сек без core dump в headless-режиме |
| Self-user виден в UI (проверка через VNC)    | **ok** — подтверждено оператором через VNC 2026-07-17 |

**Метод конвертации:** Kurigram сам по себе не понимает Telethon-схему,
поэтому:

1. login → сохранили auth key + dc_id в `_state/owner.*`;
2. вручную собрали Telethon `StringSession` с этим auth key
   (`build_telethon_string_session` в `export_tdata.py`);
3. отдали в `opentele.tl.TelegramClient` → `.ToTDesktop(UseCurrentSession)` →
   `TDesktop.SaveTData(...)`.

Эта цепочка в production делается в отдельном `tdata`-воркере (Этап 3, п.5).

**Известные подводные камни:**
- opentele сам подставляет `system="windows"`, `app_version="3.4.3 x64"`.
  Telegram считает такую сессию Desktop-клиентом Windows — это ожидаемое
  поведение для tdata-экспорта, но в аудите фиксируем факт «мы притворились
  TDesktop», чтобы owner понимал.
- В headless-режиме контейнер логирует `qt.qpa.theme.dbus: Session DBus not
  running` и `QPainter::begin: engine == 0` — некритично, Xvfb работает.
- `/tmp/.X11-unix` при `--tmpfs /tmp` уходит вместе с ним; xvfb сам себе
  создаёт сокет — работает.

## Открытые вопросы к Этапу 1

- Формат хранения auth key в БД: у нас 256 байт → шифруем через
  envelope encryption, AAD `= account_id + "session"`. Отдельно храним
  dc_id + user_id + api_id открытым (не секрет).
- Кэш `users.GetFullUser` — сделать на уровне `mtproto/health.py`
  (TTL 60 сек), иначе dashboard будет ловить FLOOD_WAIT.
- opentele-версия не имеет `__version__` — фиксируем через
  `pyproject.toml` pin (сейчас `1.15.1`), в БД `exports.manifest_json`
  пишем строкой.

## Как воспроизвести

```fish
cd research
cp .env.example .env
# заполнить PYROGRAM_API_ID / PYROGRAM_API_HASH / TEST_PHONE
uv sync

uv run python login.py run           # в другом окне: echo 'CODE' > _state/code.txt
uv run python login.py dump          # если 2FA был — TG_PASSWORD=... uv run python login.py two-fa
uv run python profile_ops.py
uv run python export_pyrogram.py
uv run python export_tdata.py

cd tdata-runner
./build.sh
./run.sh
# VNC → localhost:5900 → визуально убедиться, что TDesktop авторизован
```

## Готовность к Этапу 1

- [x] MTProto-библиотека выбрана (Kurigram 2.2.24)
- [x] Все поля профиля из ТЗ 4.3 подтверждены на живом аккаунте
- [x] `.session` собирается и открывается свежим клиентом
- [x] `tdata` собирается и TDesktop 5.7.2 не падает
- [x] VNC-подтверждение «self-user виден в UI» — оператор проверил
      2026-07-17

Этап 0 закрыт. Стартуем Этап 1.
