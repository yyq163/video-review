"""Bind upload finalization identity and reserve unreclaimed packages.

Revision ID: 20260714_0013
Revises: 20260714_0012
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260714_0013"
down_revision = "20260714_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("upload_sessions") as batch_op:
        batch_op.drop_constraint("ck_upload_sessions_finalization_lease", type_="check")
        batch_op.add_column(sa.Column("finalization_idempotency_key_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("finalization_request_hash", sa.String(length=64), nullable=True))

    # An in-flight pre-upgrade lease has no durable operation identity. Return
    # it to receiving rather than allowing an unrelated request to take it over.
    op.execute(
        "UPDATE upload_sessions SET status = 'receiving', "
        "finalization_lease_id = NULL, finalization_lease_expires_at = NULL, "
        "finalization_file_id = NULL WHERE status = 'finalizing'"
    )

    with op.batch_alter_table("upload_sessions") as batch_op:
        batch_op.create_check_constraint(
            "ck_upload_sessions_finalization_lease",
            "(status = 'finalizing' AND finalization_lease_id IS NOT NULL "
            "AND finalization_lease_expires_at IS NOT NULL AND finalization_file_id IS NOT NULL "
            "AND length(finalization_idempotency_key_hash) = 64 AND length(finalization_request_hash) = 64) "
            "OR (status != 'finalizing' AND finalization_lease_id IS NULL "
            "AND finalization_lease_expires_at IS NULL AND finalization_file_id IS NULL "
            "AND finalization_idempotency_key_hash IS NULL AND finalization_request_hash IS NULL)",
        )

    # A package may still occupy disk whenever reclamation is unconfirmed,
    # including legacy ready rows that 0007 deliberately expired.
    op.execute(
        "UPDATE package_snapshots SET storage_bytes = "
        "CASE WHEN storage_bytes < total_bytes + 1048576 THEN total_bytes + 1048576 ELSE storage_bytes END "
        "WHERE storage_reclaimed_at IS NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("upload_sessions") as batch_op:
        batch_op.drop_constraint("ck_upload_sessions_finalization_lease", type_="check")
        batch_op.drop_column("finalization_request_hash")
        batch_op.drop_column("finalization_idempotency_key_hash")
        batch_op.create_check_constraint(
            "ck_upload_sessions_finalization_lease",
            "(status = 'finalizing' AND finalization_lease_id IS NOT NULL "
            "AND finalization_lease_expires_at IS NOT NULL AND finalization_file_id IS NOT NULL) "
            "OR (status != 'finalizing' AND finalization_lease_id IS NULL "
            "AND finalization_lease_expires_at IS NULL AND finalization_file_id IS NULL)",
        )
