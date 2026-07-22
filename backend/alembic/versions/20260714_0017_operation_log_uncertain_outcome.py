"""add package build leases and conflict-safe uncertain operation outcomes

Revision ID: 20260714_0017
Revises: 20260714_0016
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260714_0017"
down_revision = "20260714_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("package_snapshots") as batch_op:
        batch_op.add_column(sa.Column("build_lease_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("build_lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_check_constraint(
            "ck_package_snapshots_build_lease",
            "(build_lease_id is null and build_lease_expires_at is null) "
            "or (status = 'preparing' and build_lease_id is not null "
            "and build_lease_expires_at is not null)",
        )
    op.create_index(
        "ix_package_snapshots_build_lease",
        "package_snapshots",
        ["status", "build_lease_expires_at"],
    )
    with op.batch_alter_table("operation_logs") as batch_op:
        batch_op.drop_constraint("ck_operation_logs_result", type_="check")
        batch_op.create_check_constraint(
            "ck_operation_logs_result",
            "result in ('ok','error','unknown')",
        )
        batch_op.add_column(sa.Column("operation_identity_hash", sa.String(length=64), nullable=True))
        batch_op.create_check_constraint(
            "ck_operation_logs_operation_identity_hash",
            "operation_identity_hash IS NULL OR length(operation_identity_hash) = 64",
        )
    op.create_index(
        "uq_operation_logs_unknown_identity",
        "operation_logs",
        ["operation_identity_hash"],
        unique=True,
        postgresql_where=sa.text("result = 'unknown' AND operation_identity_hash IS NOT NULL"),
        sqlite_where=sa.text("result = 'unknown' AND operation_identity_hash IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_operation_logs_unknown_identity", table_name="operation_logs")
    op.execute("UPDATE operation_logs SET result = 'error', error_code = COALESCE(error_code, 'COMMIT_OUTCOME_UNKNOWN') WHERE result = 'unknown'")
    with op.batch_alter_table("operation_logs") as batch_op:
        batch_op.drop_constraint(
            "ck_operation_logs_operation_identity_hash",
            type_="check",
        )
        batch_op.drop_constraint("ck_operation_logs_result", type_="check")
        batch_op.create_check_constraint(
            "ck_operation_logs_result",
            "result in ('ok','error')",
        )
        batch_op.drop_column("operation_identity_hash")
    op.drop_index("ix_package_snapshots_build_lease", table_name="package_snapshots")
    with op.batch_alter_table("package_snapshots") as batch_op:
        batch_op.drop_constraint("ck_package_snapshots_build_lease", type_="check")
        batch_op.drop_column("build_lease_expires_at")
        batch_op.drop_column("build_lease_id")
