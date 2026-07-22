"""Preserve operation-log compatibility defaults for rolling rollback.

Revision ID: 20260714_0016
Revises: 20260714_0015
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260714_0016"
down_revision = "20260714_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("operation_logs") as batch_op:
        batch_op.alter_column(
            "command_type",
            existing_type=sa.String(length=128),
            existing_nullable=False,
            server_default=sa.text("'LegacyOperation'"),
        )
        batch_op.alter_column(
            "principal_kind",
            existing_type=sa.String(length=32),
            existing_nullable=False,
            server_default=sa.text("'anonymous'"),
        )
        batch_op.alter_column(
            "resource_type",
            existing_type=sa.String(length=32),
            existing_nullable=False,
            server_default=sa.text("'request'"),
        )


def downgrade() -> None:
    with op.batch_alter_table("operation_logs") as batch_op:
        batch_op.alter_column(
            "resource_type",
            existing_type=sa.String(length=32),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "principal_kind",
            existing_type=sa.String(length=32),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "command_type",
            existing_type=sa.String(length=128),
            existing_nullable=False,
            server_default=None,
        )
