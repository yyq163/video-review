"""reconcile constraints and indexes from earlier uncommitted 0018 drafts

Revision ID: 20260714_0019
Revises: 20260714_0018
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260714_0019"
down_revision = "20260714_0018"
branch_labels = None
depends_on = None


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


def _replace_check(table_name: str, name: str, expression: str) -> None:
    if name in _check_names(table_name):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(name, type_="check")
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.create_check_constraint(name, expression)


def _drop_index_if_present(table_name: str, name: str) -> None:
    if name in _index_names(table_name):
        op.drop_index(name, table_name=table_name)


def upgrade() -> None:
    _replace_check(
        "package_snapshots",
        "ck_package_snapshots_build_lease",
        "(build_lease_id is null and build_lease_expires_at is null) "
        "or (status = 'preparing' and build_lease_id is not null "
        "and build_lease_expires_at is not null)",
    )
    _drop_index_if_present("package_snapshots", "ix_package_snapshots_build_lease")
    op.create_index(
        "ix_package_snapshots_build_lease",
        "package_snapshots",
        ["status", "build_lease_expires_at"],
    )

    _replace_check(
        "operation_logs",
        "ck_operation_logs_operation_identity_hash",
        "operation_identity_hash IS NULL OR length(operation_identity_hash) = 64",
    )
    _drop_index_if_present("operation_logs", "uq_operation_logs_unknown_identity")
    op.create_index(
        "uq_operation_logs_unknown_identity",
        "operation_logs",
        ["operation_identity_hash"],
        unique=True,
        postgresql_where=sa.text("result = 'unknown' AND operation_identity_hash IS NOT NULL"),
        sqlite_where=sa.text("result = 'unknown' AND operation_identity_hash IS NOT NULL"),
    )


def downgrade() -> None:
    # 0019 only restores the final 0017 definitions. The schema remains valid
    # at 0018, whose downgrade path delegates field removal to 0017.
    pass
