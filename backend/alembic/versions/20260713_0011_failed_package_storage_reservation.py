"""Conservatively reserve storage for legacy failed packages.

Revision ID: 20260713_0011
Revises: 20260713_0010
"""

from __future__ import annotations

from alembic import op


revision = "20260713_0011"
down_revision = "20260713_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE package_snapshots "
        "SET storage_bytes = CASE "
        "WHEN storage_bytes < total_bytes + 1048576 THEN total_bytes + 1048576 "
        "ELSE storage_bytes END, storage_reclaimed_at = NULL "
        "WHERE status = 'failed' AND storage_reclaimed_at IS NULL"
    )


def downgrade() -> None:
    # Reservation is intentionally not reduced on downgrade; physical cleanup is the only safe release.
    pass
