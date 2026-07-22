"""add outbox consumer receipts

Revision ID: 20260619_0002
Revises: 20260619_0001
Create Date: 2026-06-19
"""
from __future__ import annotations

revision = "20260619_0002"
down_revision = "20260619_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op compatibility revision.

    The table and its FK/unique constraints are now part of the explicit
    initial schema so head->0001 downgrade leaves a schema that matches 0001.
    """


def downgrade() -> None:
    """No-op compatibility revision."""
