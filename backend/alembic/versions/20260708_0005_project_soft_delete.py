"""add soft-delete marker for review projects

Revision ID: 20260708_0005
Revises: 20260621_0004
Create Date: 2026-07-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260708_0005"
down_revision = "20260621_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("project_refs", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_project_refs_deleted_at", "project_refs", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_project_refs_deleted_at", table_name="project_refs")
    op.drop_column("project_refs", "deleted_at")
