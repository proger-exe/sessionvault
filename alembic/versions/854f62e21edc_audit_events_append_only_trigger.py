"""audit events append only trigger

Revision ID: 854f62e21edc
Revises: 10be95664d0f
Create Date: 2026-07-18 01:47:13.532660

Триггер запрещает UPDATE и DELETE на `audit_events` для всех сессий.
Обойти можно только миграцией — RAISE EXCEPTION гасит любую попытку
операторского пути.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "854f62e21edc"
down_revision: Union[str, None] = "10be95664d0f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TRIGGER_FN_UP = """
CREATE OR REPLACE FUNCTION audit_events_reject_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only; % denied', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;
"""

_TRIGGER_UPDATE_UP = """
CREATE TRIGGER audit_events_no_update
    BEFORE UPDATE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_reject_mutation();
"""

_TRIGGER_DELETE_UP = """
CREATE TRIGGER audit_events_no_delete
    BEFORE DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_reject_mutation();
"""

# TRUNCATE не срабатывает DELETE-триггер — нужен отдельный statement-level.
_TRIGGER_TRUNCATE_UP = """
CREATE TRIGGER audit_events_no_truncate
    BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit_events_reject_mutation();
"""


def upgrade() -> None:
    # asyncpg не принимает multi-statement в один execute — разбиваем.
    op.execute(_TRIGGER_FN_UP)
    op.execute(_TRIGGER_UPDATE_UP)
    op.execute(_TRIGGER_DELETE_UP)
    op.execute(_TRIGGER_TRUNCATE_UP)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events")
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_delete ON audit_events")
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_truncate ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_reject_mutation()")
