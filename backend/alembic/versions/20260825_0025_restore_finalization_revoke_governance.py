"""restore exact finalization revoke governance after durable deletion hardening

Revision ID: 20260825_0025
Revises: 20260824_0024
Create Date: 2026-08-25
"""

from __future__ import annotations

import importlib

from alembic import op


revision = "20260825_0025"
down_revision = "20260824_0024"
branch_labels = None
depends_on = None


def _dialect() -> str:
    return op.get_bind().dialect.name


def _restore_exact_revoke_governance() -> None:
    migration = importlib.import_module(
        "backend.alembic.versions.20260731_0020_repair_list_runtime",
    )
    migration._drop_sqlite_finalization_triggers()  # type: ignore[attr-defined]
    migration._replace_governance_triggers()  # type: ignore[attr-defined]


def _restore_strict_0024_review_item_freeze() -> None:
    if _dialect() == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_review_items_freeze_after_finalized")
        op.execute(
            """
            CREATE TRIGGER trg_review_items_freeze_after_finalized
            BEFORE UPDATE ON review_items
            WHEN OLD.workflow_status = 'finalized'
            BEGIN
              SELECT RAISE(ABORT, 'finalized review item is frozen');
            END
            """,
        )
        return
    if _dialect() != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fcr_check_review_item_refs_and_freeze()
        RETURNS trigger AS $$
        BEGIN
          IF OLD.workflow_status = 'finalized' THEN
            RAISE EXCEPTION 'finalized review item is frozen';
          END IF;
          IF NEW.current_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM review_versions v
            WHERE v.id = NEW.current_version_id
              AND v.project_ref_id = NEW.project_ref_id
              AND v.review_item_id = NEW.id
              AND v.is_current IS TRUE
          ) THEN
            RAISE EXCEPTION 'current version identity mismatch';
          END IF;
          IF NEW.active_finalization_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM finalizations f
            WHERE f.id = NEW.active_finalization_id
              AND f.project_ref_id = NEW.project_ref_id
              AND f.review_item_id = NEW.id
              AND f.version_id = NEW.current_version_id
          ) THEN
            RAISE EXCEPTION 'active finalization identity mismatch';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
    )


def upgrade() -> None:
    _restore_exact_revoke_governance()


def downgrade() -> None:
    _restore_strict_0024_review_item_freeze()
