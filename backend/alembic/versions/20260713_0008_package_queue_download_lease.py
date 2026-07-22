"""Bound package queue and add one-shot download leases.

Duplicate preparing snapshots are failed closed during upgrade. Their prior
state is intentionally not restored by schema downgrade; operational rollback
uses the pre-upgrade database backup.

Revision ID: 20260713_0008
Revises: 20260713_0007
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260713_0008"
down_revision = "20260713_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "WITH ranked AS ("
        "SELECT id, row_number() OVER (PARTITION BY project_ref_id ORDER BY created_at, id) AS rank "
        "FROM package_snapshots WHERE status = 'preparing'"
        ") UPDATE package_snapshots SET status = 'failed', "
        "failure_details = '{\"error_code\":\"PACKAGE_QUEUE_CONFLICT\"}', updated_at = CURRENT_TIMESTAMP "
        "WHERE id IN (SELECT id FROM ranked WHERE rank > 1)"
    )
    with op.batch_alter_table("package_snapshots") as batch_op:
        batch_op.add_column(sa.Column("download_session_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("download_session_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("download_lease_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("download_lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_download_finished_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("storage_reclaimed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_check_constraint(
            "ck_package_snapshots_download_session_pair",
            "(download_session_hash is null) = (download_session_expires_at is null)",
        )
        batch_op.create_check_constraint(
            "ck_package_snapshots_download_lease_pair",
            "(download_lease_id is null) = (download_lease_expires_at is null)",
        )
    op.create_index(
        "uq_package_snapshots_preparing_project",
        "package_snapshots",
        ["project_ref_id"],
        unique=True,
        postgresql_where=sa.text("status = 'preparing'"),
        sqlite_where=sa.text("status = 'preparing'"),
    )


def downgrade() -> None:
    op.drop_index("uq_package_snapshots_preparing_project", table_name="package_snapshots")
    with op.batch_alter_table("package_snapshots") as batch_op:
        batch_op.drop_constraint("ck_package_snapshots_download_lease_pair", type_="check")
        batch_op.drop_constraint("ck_package_snapshots_download_session_pair", type_="check")
        batch_op.drop_column("storage_reclaimed_at")
        batch_op.drop_column("last_download_finished_at")
        batch_op.drop_column("download_lease_expires_at")
        batch_op.drop_column("download_lease_id")
        batch_op.drop_column("download_session_expires_at")
        batch_op.drop_column("download_session_hash")
