"""Track physical package bytes and bounded worker retries.

Revision ID: 20260713_0009
Revises: 20260713_0008
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260713_0009"
down_revision = "20260713_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("package_snapshots") as batch_op:
        batch_op.add_column(
            sa.Column("storage_bytes", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("build_attempts", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("next_build_attempt_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_package_snapshots_storage_bytes",
            "storage_bytes >= 0",
        )
        batch_op.create_check_constraint(
            "ck_package_snapshots_build_attempts",
            "build_attempts >= 0",
        )
    op.execute(
        "UPDATE package_snapshots SET storage_bytes = total_bytes "
        "WHERE status IN ('preparing', 'ready')"
    )
    with op.batch_alter_table("package_snapshots") as batch_op:
        batch_op.alter_column("storage_bytes", server_default=None)
        batch_op.alter_column("build_attempts", server_default=None)
    op.create_index(
        "ix_package_snapshots_worker_queue",
        "package_snapshots",
        ["status", "next_build_attempt_at", "created_at", "id"],
    )
    op.create_index(
        "ix_package_snapshots_cleanup",
        "package_snapshots",
        ["storage_reclaimed_at", "updated_at", "expires_at", "id"],
    )
    op.create_index(
        "ix_package_snapshots_quota",
        "package_snapshots",
        ["status", "storage_bytes"],
    )
    op.create_index(
        "ix_upload_sessions_cleanup",
        "upload_sessions",
        ["status", "updated_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_upload_sessions_cleanup", table_name="upload_sessions")
    op.drop_index("ix_package_snapshots_quota", table_name="package_snapshots")
    op.drop_index("ix_package_snapshots_cleanup", table_name="package_snapshots")
    op.drop_index("ix_package_snapshots_worker_queue", table_name="package_snapshots")
    with op.batch_alter_table("package_snapshots") as batch_op:
        batch_op.drop_constraint("ck_package_snapshots_build_attempts", type_="check")
        batch_op.drop_constraint("ck_package_snapshots_storage_bytes", type_="check")
        batch_op.drop_column("next_build_attempt_at")
        batch_op.drop_column("build_attempts")
        batch_op.drop_column("storage_bytes")
