"""reconcile item workflow for unresolved current-version issues

Revision ID: 20260825_0028
Revises: 20260825_0027
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260825_0028"
down_revision = "20260825_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE review_items
            SET workflow_status = 'changes_requested',
                lock_version = lock_version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE workflow_status IN ('pending_review', 'in_review')
              AND current_version_id IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM review_issues
                  WHERE review_issues.project_ref_id = review_items.project_ref_id
                    AND review_issues.review_item_id = review_items.id
                    AND review_issues.version_id = review_items.current_version_id
                    AND review_issues.status = 'unresolved'
                    AND review_issues.deleted_at IS NULL
              )
            """
        )
    )


def downgrade() -> None:
    # The prior state cannot be reconstructed after an authoritative workflow
    # transition. Keeping changes_requested is compatible with 0027 behavior.
    pass
