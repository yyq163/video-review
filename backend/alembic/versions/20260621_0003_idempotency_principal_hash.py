"""bind idempotency records to principal

Revision ID: 20260621_0003
Revises: 20260619_0002
Create Date: 2026-06-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260621_0003"
down_revision = "20260619_0002"
branch_labels = None
depends_on = None


LEGACY_PRINCIPAL_HASH = "0" * 64


def upgrade() -> None:
    with op.batch_alter_table("idempotency_records") as batch_op:
        batch_op.add_column(
            sa.Column(
                "principal_hash",
                sa.String(length=64),
                nullable=False,
                server_default=LEGACY_PRINCIPAL_HASH,
            )
        )
    if op.get_bind().dialect.name != "sqlite":
        op.alter_column("idempotency_records", "principal_hash", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("idempotency_records") as batch_op:
        batch_op.drop_column("principal_hash")
