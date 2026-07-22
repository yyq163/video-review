"""Add review issue soft-delete marker.

Revision ID: 20260709_0006
Revises: 20260708_0005
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260709_0006"
down_revision = "20260708_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_issues", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_review_issues_active_version",
        "review_issues",
        ["project_ref_id", "review_item_id", "version_id", "deleted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_review_issues_active_version", table_name="review_issues")
    op.drop_column("review_issues", "deleted_at")
