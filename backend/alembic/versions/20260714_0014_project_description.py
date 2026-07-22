"""Persist project descriptions independently from host identifiers.

Revision ID: 20260714_0014
Revises: 20260714_0013
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260714_0014"
down_revision = "20260714_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("project_refs") as batch_op:
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=False, server_default=""))
        batch_op.alter_column("description", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("project_refs") as batch_op:
        batch_op.drop_column("description")
