"""Attribute operation logs and retain auditable command failures.

Revision ID: 20260714_0015
Revises: 20260714_0014
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260714_0015"
down_revision = "20260714_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("operation_logs") as batch_op:
        batch_op.add_column(sa.Column("command_type", sa.String(length=128), nullable=False, server_default="LegacyOperation"))
        batch_op.add_column(sa.Column("principal_kind", sa.String(length=32), nullable=False, server_default="anonymous"))
        batch_op.add_column(sa.Column("principal_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("client_ip", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("user_agent", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("idempotency_key_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("resource_type", sa.String(length=32), nullable=False, server_default="request"))
        batch_op.add_column(sa.Column("resource_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("failure_stage", sa.String(length=32), nullable=True))
        batch_op.create_check_constraint(
            "ck_operation_logs_idempotency_hash",
            "idempotency_key_hash IS NULL OR length(idempotency_key_hash) = 64",
        )
        batch_op.create_index("ix_operation_logs_command_type", ["command_type"], unique=False)

    with op.batch_alter_table("operation_logs") as batch_op:
        batch_op.alter_column("command_type", server_default=None)
        batch_op.alter_column("principal_kind", server_default=None)
        batch_op.alter_column("resource_type", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("operation_logs") as batch_op:
        batch_op.drop_index("ix_operation_logs_command_type")
        batch_op.drop_constraint("ck_operation_logs_idempotency_hash", type_="check")
        batch_op.drop_column("failure_stage")
        batch_op.drop_column("resource_id")
        batch_op.drop_column("resource_type")
        batch_op.drop_column("idempotency_key_hash")
        batch_op.drop_column("user_agent")
        batch_op.drop_column("client_ip")
        batch_op.drop_column("principal_id")
        batch_op.drop_column("principal_kind")
        batch_op.drop_column("command_type")
