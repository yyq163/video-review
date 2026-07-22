"""Persist upload quota reservations and finalization leases.

Revision ID: 20260714_0012
Revises: 20260713_0011
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260714_0012"
down_revision = "20260713_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("upload_sessions") as batch_op:
        batch_op.add_column(sa.Column("reserved_bytes", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("parts_cleanup_confirmed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("finalization_lease_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("finalization_lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("finalization_file_id", sa.String(length=64), nullable=True))

    op.execute("UPDATE upload_sessions SET reserved_bytes = declared_size * 2")

    with op.batch_alter_table("upload_sessions") as batch_op:
        batch_op.alter_column("reserved_bytes", existing_type=sa.BigInteger(), nullable=False)
        batch_op.drop_constraint("ck_upload_sessions_status", type_="check")
        batch_op.create_check_constraint(
            "ck_upload_sessions_status",
            "status in ('initiated','receiving','finalizing','completed','aborted')",
        )
        batch_op.create_check_constraint(
            "ck_upload_sessions_reserved_bytes",
            "reserved_bytes = declared_size * 2",
        )
        batch_op.create_check_constraint(
            "ck_upload_sessions_finalization_lease",
            "(status = 'finalizing' AND finalization_lease_id IS NOT NULL "
            "AND finalization_lease_expires_at IS NOT NULL AND finalization_file_id IS NOT NULL) "
            "OR (status != 'finalizing' AND finalization_lease_id IS NULL "
            "AND finalization_lease_expires_at IS NULL AND finalization_file_id IS NULL)",
        )
        batch_op.create_check_constraint(
            "ck_upload_sessions_cleanup_terminal",
            "parts_cleanup_confirmed_at IS NULL OR status IN ('completed','aborted')",
        )
        batch_op.create_index(
            "ix_upload_sessions_quota",
            ["parts_cleanup_confirmed_at", "owner_principal_kind", "owner_principal_id"],
        )


def downgrade() -> None:
    op.execute(
        "UPDATE upload_sessions SET status = 'receiving', finalization_lease_id = NULL, "
        "finalization_lease_expires_at = NULL, finalization_file_id = NULL "
        "WHERE status = 'finalizing'"
    )
    with op.batch_alter_table("upload_sessions") as batch_op:
        batch_op.drop_index("ix_upload_sessions_quota")
        batch_op.drop_constraint("ck_upload_sessions_cleanup_terminal", type_="check")
        batch_op.drop_constraint("ck_upload_sessions_finalization_lease", type_="check")
        batch_op.drop_constraint("ck_upload_sessions_reserved_bytes", type_="check")
        batch_op.drop_constraint("ck_upload_sessions_status", type_="check")
        batch_op.create_check_constraint(
            "ck_upload_sessions_status",
            "status in ('initiated','receiving','completed','aborted')",
        )
        batch_op.drop_column("finalization_file_id")
        batch_op.drop_column("finalization_lease_expires_at")
        batch_op.drop_column("finalization_lease_id")
        batch_op.drop_column("parts_cleanup_confirmed_at")
        batch_op.drop_column("reserved_bytes")
