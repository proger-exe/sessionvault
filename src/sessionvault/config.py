"""Единый источник конфигурации.

Читает `.env`, валидирует через Pydantic. Всё, что чувствительно (BOT_TOKEN,
MASTER_KEY, api_hash), возвращается как `SecretStr` — так их сложнее случайно
залогировать.
"""

from __future__ import annotations

import base64

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram-бот -----------------------------------------------------
    bot_token: SecretStr = Field(...)
    owner_id: int = Field(..., description="Telegram user ID единственного Owner")

    # --- MTProto (Kurigram) -----------------------------------------------
    pyrogram_api_id: int = Field(...)
    pyrogram_api_hash: SecretStr = Field(...)

    # --- Шифрование секретов ----------------------------------------------
    master_key: SecretStr = Field(...)
    master_key_v2: SecretStr | None = Field(default=None)

    # --- Хранилища --------------------------------------------------------
    database_url: str = Field("postgresql+asyncpg://tg:tg@localhost:5432/sessionvault")
    redis_url: str = Field("redis://localhost:6379/0")

    # --- Логика приложения ------------------------------------------------
    exports_tdata_enabled: bool = Field(False)
    store_twofa_passwords: bool = Field(False, description="Owner-policy: если true, 2FA-пароль шифруется и хранится")
    export_tmp_dir: str = Field("/tmp/sessionvault-exports")
    export_archive_ttl_seconds: int = Field(300)
    login_flow_ttl_seconds: int = Field(300)
    log_level: str = Field("INFO")

    @field_validator("master_key", "master_key_v2")
    @classmethod
    def _validate_master_key(cls, v: SecretStr | None) -> SecretStr | None:
        """MASTER_KEY должен быть base64-строкой, дающей ровно 32 байта."""
        if v is None:
            return None
        raw = v.get_secret_value()
        try:
            decoded = base64.b64decode(raw, validate=True)
        except Exception as e:
            raise ValueError("MASTER_KEY* должен быть валидным base64") from e
        if len(decoded) != 32:
            raise ValueError(f"MASTER_KEY* должен быть 32 байта, получено {len(decoded)}")
        return v

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"LOG_LEVEL: {v!r} не в {allowed}")
        return v.upper()

    def master_key_bytes(self, *, version: int = 1) -> bytes:
        """Возвращает decoded 32-байтный ключ выбранной версии."""
        source = self.master_key if version == 1 else self.master_key_v2
        if source is None:
            raise LookupError(f"MASTER_KEY версии {version} не сконфигурирован")
        return base64.b64decode(source.get_secret_value())


_settings: Settings | None = None


def get_settings() -> Settings:
    """Ленивый глобальный синглтон настроек.

    Тесты подменяют его через `override_settings(...)` — не через прямой доступ
    к переменной модуля.
    """
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]  # значения из env
    return _settings


def override_settings(settings: Settings) -> None:
    """Только для тестов — подмена активных настроек."""
    global _settings
    _settings = settings


def reset_settings() -> None:
    """Сбросить кэш; следующий `get_settings()` перечитает env."""
    global _settings
    _settings = None
