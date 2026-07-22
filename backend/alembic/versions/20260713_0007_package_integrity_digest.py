"""Add package integrity digest and fail closed for legacy ready packages.

The data invalidation is intentionally not reversed by schema downgrade: a
legacy package has no trustworthy digest and must never become downloadable
again without a backup restore.

Revision ID: 20260713_0007
Revises: 20260709_0006
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260713_0007"
down_revision = "20260709_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("package_snapshots") as batch_op:
        batch_op.add_column(sa.Column("sha256", sa.String(length=64), nullable=True))
        batch_op.create_check_constraint(
            "ck_package_snapshots_sha256",
            "sha256 is null or length(sha256) = 64",
        )
    op.execute(
        "UPDATE package_snapshots "
        "SET status = 'expired', expires_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
        "WHERE status = 'ready'"
    )
    with op.batch_alter_table("package_snapshots") as batch_op:
        batch_op.create_check_constraint(
            "ck_package_snapshots_ready_sha256",
            "status != 'ready' or sha256 is not null",
        )


def downgrade() -> None:
    with op.batch_alter_table("package_snapshots") as batch_op:
        batch_op.drop_constraint("ck_package_snapshots_ready_sha256", type_="check")
        batch_op.drop_constraint("ck_package_snapshots_sha256", type_="check")
        batch_op.drop_column("sha256")
