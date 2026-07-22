"""Conservatively reserve legacy active package storage.

Revision ID: 20260713_0010
Revises: 20260713_0009
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op


revision = "20260713_0010"
down_revision = "20260713_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The runtime maximum (100 files, 512-byte names) needs less than 1 MiB of
    # ZIP_STORED metadata. Legacy active rows reserve that full bound until a
    # worker records the exact completed archive size.
    op.execute(
        "UPDATE package_snapshots SET storage_bytes = total_bytes + 1048576 "
        "WHERE status IN ('preparing', 'ready') AND storage_bytes <= total_bytes"
    )


def downgrade() -> None:
    # Preserve the conservative reservation when rolling code back. Reducing a
    # live quota reservation during downgrade would reintroduce overcommit.
    pass
