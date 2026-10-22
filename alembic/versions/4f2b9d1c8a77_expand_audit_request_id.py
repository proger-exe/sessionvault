"""expand audit request id

Revision ID: 4f2b9d1c8a77
Revises: 854f62e21edc
Create Date: 2026-07-18 03:53:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "4f2b9d1c8a77"
down_revision: Union[str, None] = "854f62e21edc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "audit_events",
        "request_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=128),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "audit_events",
        "request_id",
        existing_type=sa.String(length=128),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
