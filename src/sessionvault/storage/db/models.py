"""SQLAlchemy 2.x async ORM модели.

Соответствуют разделу «Модель данных» плана. Ключевые правила:
- UUID PK на всех бизнес-таблицах;
- created_at / updated_at через server_default now();
- секреты живут в отдельной таблице telegram_credentials — CIPHERTEXT в BYTEA;
- audit_events append-only на уровне БД (триггер добавляется в миграции 0002);
- никаких on_delete=CASCADE на секретных таблицах — только явное soft-delete.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    Enum as SAEnumBase,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def SAEnum(enum_class: type, **kwargs: Any) -> SAEnumBase:  # noqa: N802
    """Обёртка над sa.Enum: хранит в PG именно values (lowercase), а не NAMES.

    Иначе Postgres пишет ENUM-значения как имена (PENDING), а сервер-дефолт
    у нас — строка value ('pending') → InvalidTextRepresentationError.
    """
    return SAEnumBase(
        enum_class,
        values_callable=lambda cls: [m.value for m in cls],
        **kwargs,
    )


class Base(DeclarativeBase):
    """Общий базовый класс для всех ORM-моделей."""


class UserRole(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"


class AccountStatus(str, enum.Enum):
    PENDING = "pending"          # ждёт первого login
    ACTIVE = "active"            # рабочая
    ARCHIVED = "archived"        # изъят из работы, сессия удалена локально


class RiskState(str, enum.Enum):
    NORMAL = "normal"
    REAUTH_REQUIRED = "reauth_required"
    FLOOD_WAIT = "flood_wait"
    LIMITED = "limited"
    BANNED = "banned"
    INVALID_SESSION = "invalid_session"


class LoginFlowState(str, enum.Enum):
    CREATED = "created"
    CODE_REQUESTED = "code_requested"
    WAITING_CODE = "waiting_code"
    WAITING_2FA = "waiting_2fa"
    AUTHORIZING = "authorizing"
    AUTHORIZED = "authorized"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ProfileOperationState(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"


class ExportFormat(str, enum.Enum):
    PYROGRAM_SESSION = "pyrogram_session"
    TDATA = "tdata"


class ExportState(str, enum.Enum):
    PENDING = "pending"
    BUILDING = "building"
    READY = "ready"
    SENT = "sent"
    EXPIRED = "expired"
    FAILED = "failed"


class AuditResult(str, enum.Enum):
    OK = "ok"
    REJECTED = "rejected"
    FAILED = "failed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole, name="user_role"), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128))
    added_by_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    phone_e164_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    phone_last4: Mapped[str] = mapped_column(String(4), nullable=False)

    first_name: Mapped[str | None] = mapped_column(String(64))
    last_name: Mapped[str | None] = mapped_column(String(64))
    username: Mapped[str | None] = mapped_column(String(32))
    dc_id: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[AccountStatus] = mapped_column(
        SAEnum(AccountStatus, name="account_status"),
        nullable=False,
        server_default=AccountStatus.PENDING.value,
        index=True,
    )
    risk_state: Mapped[RiskState] = mapped_column(
        SAEnum(RiskState, name="risk_state"),
        nullable=False,
        server_default=RiskState.NORMAL.value,
        index=True,
    )
    has_2fa: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    has_recovery_email: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    note: Mapped[str | None] = mapped_column(Text)
    owner_operator_tg_id: Mapped[int | None] = mapped_column(BigInteger)

    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_profile_update_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    credentials: Mapped[TelegramCredentials | None] = relationship(
        back_populates="account", uselist=False, cascade="all, delete-orphan"
    )


class TelegramCredentials(Base):
    __tablename__ = "telegram_credentials"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("telegram_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    dek_wrapped: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    session_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    phone_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    twofa_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    recovery_email_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    account: Mapped[TelegramAccount] = relationship(back_populates="credentials")


class LoginFlow(Base):
    __tablename__ = "login_flows"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("telegram_accounts.id", ondelete="SET NULL")
    )
    state: Mapped[LoginFlowState] = mapped_column(
        SAEnum(LoginFlowState, name="login_flow_state"),
        nullable=False,
        server_default=LoginFlowState.CREATED.value,
    )
    attempts_code: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    attempts_password: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    operator_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # Не даём стартовать два активных flow на один номер.
        Index(
            "ux_login_flows_active_phone",
            "phone_hash",
            unique=True,
            postgresql_where=(
                "state NOT IN ('authorized','failed','expired','cancelled')"
            ),
        ),
    )


class ProfileOperation(Base):
    __tablename__ = "profile_operations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("telegram_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, server_default="{}")
    state: Mapped[ProfileOperationState] = mapped_column(
        SAEnum(ProfileOperationState, name="profile_operation_state"),
        nullable=False,
        server_default=ProfileOperationState.PENDING.value,
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    operator_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("account_id", "idempotency_key", name="ux_profile_ops_idempotency"),
    )


class Export(Base):
    __tablename__ = "exports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("telegram_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    format: Mapped[ExportFormat] = mapped_column(SAEnum(ExportFormat, name="export_format"), nullable=False)
    state: Mapped[ExportState] = mapped_column(
        SAEnum(ExportState, name="export_state"),
        nullable=False,
        server_default=ExportState.PENDING.value,
        index=True,
    )
    local_path: Mapped[str | None] = mapped_column(Text)
    archive_key_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, server_default="{}")
    operator_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_tg_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    role: Mapped[UserRole | None] = mapped_column(SAEnum(UserRole, name="user_role", create_type=False))
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("telegram_accounts.id", ondelete="SET NULL"), index=True
    )
    account_tg_user_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    result: Mapped[AuditResult] = mapped_column(SAEnum(AuditResult, name="audit_result"), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    changed_fields: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EncryptionKeyVersion(Base):
    __tablename__ = "encryption_key_versions"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    env_var_name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountLock(Base):
    """Индекс активных Redis-lock-ов для сводки в UI бота.

    Сама блокировка — в Redis; эта таблица — лишь витрина для дашборда.
    """

    __tablename__ = "account_locks"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("telegram_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    holder: Mapped[str] = mapped_column(String(128), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
