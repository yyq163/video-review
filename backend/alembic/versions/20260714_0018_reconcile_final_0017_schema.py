"""reconcile databases that ran an earlier uncommitted 0017 draft

Revision ID: 20260714_0018
Revises: 20260714_0017
Create Date: 2026-07-14

The fix branch applied a narrower local 0017 draft before the final package
lease and operation-identity fields were added to that uncommitted revision.
Fresh databases already receive the complete 0017 schema, so this migration is
intentionally idempotent and only fills fields missing from those local
databases.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260714_0018"
down_revision = "20260714_0017"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _check_names(table_name: str) -> set[str]:
    return {
        name
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if isinstance((name := constraint.get("name")), str)
    }


def _index_names(table_name: str) -> set[str]:
    return {
        name
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if isinstance((name := index.get("name")), str)
    }


def upgrade() -> None:
    package_columns = _column_names("package_snapshots")
    with op.batch_alter_table("package_snapshots") as batch_op:
        if "build_lease_id" not in package_columns:
            batch_op.add_column(sa.Column("build_lease_id", sa.String(length=64), nullable=True))
        if "build_lease_expires_at" not in package_columns:
            batch_op.add_column(sa.Column("build_lease_expires_at", sa.DateTime(timezone=True), nullable=True))

    if "ck_package_snapshots_build_lease" not in _check_names("package_snapshots"):
        with op.batch_alter_table("package_snapshots") as batch_op:
            batch_op.create_check_constraint(
                "ck_package_snapshots_build_lease",
                "(build_lease_id is null and build_lease_expires_at is null) "
                "or (status = 'preparing' and build_lease_id is not null "
                "and build_lease_expires_at is not null)",
            )
    if "ix_package_snapshots_build_lease" not in _index_names("package_snapshots"):
        op.create_index(
            "ix_package_snapshots_build_lease",
            "package_snapshots",
            ["status", "build_lease_expires_at"],
        )

    operation_columns = _column_names("operation_logs")
    if "operation_identity_hash" not in operation_columns:
        with op.batch_alter_table("operation_logs") as batch_op:
            batch_op.add_column(sa.Column("operation_identity_hash", sa.String(length=64), nullable=True))
    if "ck_operation_logs_operation_identity_hash" not in _check_names("operation_logs"):
        with op.batch_alter_table("operation_logs") as batch_op:
            batch_op.create_check_constraint(
                "ck_operation_logs_operation_identity_hash",
                "operation_identity_hash IS NULL OR length(operation_identity_hash) = 64",
            )
    if "uq_operation_logs_unknown_identity" not in _index_names("operation_logs"):
        op.create_index(
            "uq_operation_logs_unknown_identity",
            "operation_logs",
            ["operation_identity_hash"],
            unique=True,
            postgresql_where=sa.text("result = 'unknown' AND operation_identity_hash IS NOT NULL"),
            sqlite_where=sa.text("result = 'unknown' AND operation_identity_hash IS NOT NULL"),
        )


def downgrade() -> None:
    # 0018 adds no schema beyond the final 0017 definition. A downgrade to
    # 0017 therefore keeps the reconciled fields; 0017's own downgrade removes
    # them when moving to 0016.
    pass
