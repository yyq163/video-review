"""add media derivatives, finalization revocation, and automatic issue workflow state

Revision ID: 20260731_0020
Revises: 20260714_0019
Create Date: 2026-07-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260731_0020"
down_revision = "20260714_0019"
branch_labels = None
depends_on = None


def _dialect() -> str:
    return op.get_bind().dialect.name


def _check_names(table_name: str) -> set[str]:
    return {
        name
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if isinstance((name := constraint.get("name")), str)
    }


def _replace_check(table_name: str, old_names: tuple[str, ...], name: str, expression: str) -> None:
    existing = _check_names(table_name)
    with op.batch_alter_table(table_name) as batch_op:
        for old_name in old_names:
            if old_name in existing:
                batch_op.drop_constraint(old_name, type_="check")
        batch_op.create_check_constraint(name, expression)


def _drop_sqlite_finalization_triggers() -> None:
    if _dialect() != "sqlite":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_review_items_finalization_identity")
    for event in ("insert", "update", "delete"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_finalizations_freeze_after_finalized_{event}")


def _create_sqlite_finalization_freeze_triggers() -> None:
    if _dialect() != "sqlite":
        return
    for event in ("INSERT", "UPDATE"):
        op.execute(
            f"""
            CREATE TRIGGER trg_finalizations_freeze_after_finalized_{event.lower()}
            BEFORE {event} ON finalizations
            WHEN EXISTS (
              SELECT 1 FROM review_items i
              WHERE i.id = NEW.review_item_id
                AND i.project_ref_id = NEW.project_ref_id
                AND i.workflow_status = 'finalized'
            )
            BEGIN
              SELECT RAISE(ABORT, 'finalized review item is frozen');
            END
            """
        )
    op.execute(
        """
        CREATE TRIGGER trg_finalizations_freeze_after_finalized_delete
        BEFORE DELETE ON finalizations
        WHEN EXISTS (
          SELECT 1 FROM review_items i
          WHERE i.id = OLD.review_item_id
            AND i.project_ref_id = OLD.project_ref_id
            AND i.workflow_status = 'finalized'
        )
        BEGIN
          SELECT RAISE(ABORT, 'finalized review item is frozen');
        END
        """
    )


def _create_media_derivative_tasks() -> None:
    op.create_table(
        "media_derivative_tasks",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("project_ref_id", sa.String(length=64), nullable=False),
        sa.Column("review_item_id", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("lease_id", sa.String(length=64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("output_file_id", sa.String(length=64)),
        sa.Column("error_code", sa.String(length=64)),
        sa.Column("failure_details", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("version_id", "kind", name="uq_media_derivative_tasks_version_kind"),
        sa.ForeignKeyConstraint(
            ["version_id", "project_ref_id", "review_item_id"],
            ["review_versions.id", "review_versions.project_ref_id", "review_versions.review_item_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["output_file_id"], ["file_objects.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("kind in ('playback_faststart','thumbnail')", name="ck_media_derivative_tasks_kind"),
        sa.CheckConstraint("status in ('queued','running','ready','failed')", name="ck_media_derivative_tasks_status"),
        sa.CheckConstraint("attempts >= 0", name="ck_media_derivative_tasks_attempts"),
        sa.CheckConstraint(
            "(status = 'running' AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status != 'running' AND lease_id IS NULL AND lease_expires_at IS NULL)",
            name="ck_media_derivative_tasks_lease",
        ),
        sa.CheckConstraint(
            "(status = 'ready' AND output_file_id IS NOT NULL) OR status != 'ready'",
            name="ck_media_derivative_tasks_ready_output",
        ),
    )
    op.create_index(
        "ix_media_derivative_tasks_queue",
        "media_derivative_tasks",
        ["status", "next_attempt_at", "created_at", "id"],
    )
    op.create_index(
        "ix_media_derivative_tasks_version",
        "media_derivative_tasks",
        ["project_ref_id", "review_item_id", "version_id"],
    )


def _create_finalization_package_invalidations() -> None:
    op.create_table(
        "finalization_package_invalidations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("project_ref_id", sa.String(length=64), nullable=False),
        sa.Column("review_item_id", sa.String(length=64), nullable=False),
        sa.Column("finalization_id", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.String(length=64), nullable=False),
        sa.Column("cleanup_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("cleanup_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "finalization_id",
            "package_id",
            name="uq_finalization_package_invalidations_identity",
        ),
        sa.ForeignKeyConstraint(
            ["finalization_id", "project_ref_id", "review_item_id"],
            ["finalizations.id", "finalizations.project_ref_id", "finalizations.review_item_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["package_id", "project_ref_id"],
            ["package_snapshots.id", "package_snapshots.project_ref_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "cleanup_status in ('pending','failed','complete')",
            name="ck_finalization_package_invalidations_status",
        ),
        sa.CheckConstraint(
            "cleanup_attempts >= 0",
            name="ck_finalization_package_invalidations_attempts",
        ),
    )
    op.create_index(
        "ix_finalization_package_invalidations_finalization",
        "finalization_package_invalidations",
        ["finalization_id", "cleanup_status"],
    )
    op.create_index(
        "ix_finalization_package_invalidations_package",
        "finalization_package_invalidations",
        ["package_id"],
    )


def _replace_governance_triggers() -> None:
    if _dialect() == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_review_items_finalization_identity")
        op.execute(
            """
            CREATE TRIGGER trg_review_items_finalization_identity
            BEFORE UPDATE OF active_finalization_id ON review_items
            WHEN NEW.active_finalization_id IS NOT NULL
             AND NOT EXISTS (
               SELECT 1 FROM finalizations f
               WHERE f.id = NEW.active_finalization_id
                 AND f.project_ref_id = NEW.project_ref_id
                 AND f.review_item_id = NEW.id
                 AND f.version_id = NEW.current_version_id
                 AND f.status = 'active'
             )
            BEGIN
              SELECT RAISE(ABORT, 'active finalization identity mismatch');
            END
            """
        )
        op.execute("DROP TRIGGER IF EXISTS trg_review_items_freeze_after_finalized")
        op.execute(
            """
            CREATE TRIGGER trg_review_items_freeze_after_finalized
            BEFORE UPDATE ON review_items
            WHEN OLD.workflow_status = 'finalized'
             AND NOT (
               NEW.workflow_status = 'in_review'
               AND NEW.active_finalization_id IS NULL
               AND NEW.current_version_id = OLD.current_version_id
               AND NEW.current_version_is_current = OLD.current_version_is_current
               AND NEW.project_ref_id = OLD.project_ref_id
               AND NEW.item_code = OLD.item_code
               AND NEW.title = OLD.title
               AND COALESCE(NEW.episode_no, -1) = COALESCE(OLD.episode_no, -1)
               AND NEW.lock_version = OLD.lock_version + 1
             )
            BEGIN
              SELECT RAISE(ABORT, 'finalized review item is frozen');
            END
            """
        )
        _create_sqlite_finalization_freeze_triggers()
        return
    if _dialect() != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fcr_check_review_item_refs_and_freeze() RETURNS trigger AS $$
        BEGIN
          IF OLD.workflow_status = 'finalized' AND NOT (
            NEW.workflow_status = 'in_review'
            AND NEW.active_finalization_id IS NULL
            AND NEW.current_version_id IS NOT DISTINCT FROM OLD.current_version_id
            AND NEW.current_version_is_current IS NOT DISTINCT FROM OLD.current_version_is_current
            AND NEW.project_ref_id = OLD.project_ref_id
            AND NEW.item_code = OLD.item_code
            AND NEW.title = OLD.title
            AND NEW.episode_no IS NOT DISTINCT FROM OLD.episode_no
            AND NEW.lock_version = OLD.lock_version + 1
          ) THEN
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
              AND f.status = 'active'
          ) THEN
            RAISE EXCEPTION 'active finalization identity mismatch';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def _backfill_issue_workflow_state() -> None:
    op.execute(
        """
        UPDATE review_items
        SET workflow_status = CASE
          WHEN EXISTS (
            SELECT 1
            FROM review_issues i
            WHERE i.review_item_id = review_items.id
              AND i.project_ref_id = review_items.project_ref_id
              AND i.version_id = review_items.current_version_id
              AND i.status = 'unresolved'
              AND i.deleted_at IS NULL
          ) THEN 'changes_requested'
          WHEN workflow_status = 'changes_requested' THEN 'in_review'
          ELSE workflow_status
        END,
        lock_version = lock_version + 1,
        updated_at = CURRENT_TIMESTAMP
        WHERE workflow_status != 'finalized'
          AND (
            (
              EXISTS (
                SELECT 1
                FROM review_issues i
                WHERE i.review_item_id = review_items.id
                  AND i.project_ref_id = review_items.project_ref_id
                  AND i.version_id = review_items.current_version_id
                  AND i.status = 'unresolved'
                  AND i.deleted_at IS NULL
              )
              AND workflow_status != 'changes_requested'
            )
            OR (
              workflow_status = 'changes_requested'
              AND NOT EXISTS (
                SELECT 1
                FROM review_issues i
                WHERE i.review_item_id = review_items.id
                  AND i.project_ref_id = review_items.project_ref_id
                  AND i.version_id = review_items.current_version_id
                  AND i.status = 'unresolved'
                  AND i.deleted_at IS NULL
              )
            )
          )
        """
    )


def _restore_governance_triggers() -> None:
    if _dialect() == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_review_items_finalization_identity")
        op.execute(
            """
            CREATE TRIGGER trg_review_items_finalization_identity
            BEFORE UPDATE OF active_finalization_id ON review_items
            WHEN NEW.active_finalization_id IS NOT NULL
             AND NOT EXISTS (
               SELECT 1 FROM finalizations f
               WHERE f.id = NEW.active_finalization_id
                 AND f.project_ref_id = NEW.project_ref_id
                 AND f.review_item_id = NEW.id
                 AND f.version_id = NEW.current_version_id
             )
            BEGIN
              SELECT RAISE(ABORT, 'active finalization identity mismatch');
            END
            """
        )
        op.execute("DROP TRIGGER IF EXISTS trg_review_items_freeze_after_finalized")
        op.execute(
            """
            CREATE TRIGGER trg_review_items_freeze_after_finalized
            BEFORE UPDATE ON review_items
            WHEN OLD.workflow_status = 'finalized'
            BEGIN
              SELECT RAISE(ABORT, 'finalized review item is frozen');
            END
            """
        )
        _create_sqlite_finalization_freeze_triggers()
        return
    if _dialect() != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fcr_check_review_item_refs_and_freeze() RETURNS trigger AS $$
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
        """
    )


def upgrade() -> None:
    _create_media_derivative_tasks()
    _drop_sqlite_finalization_triggers()
    with op.batch_alter_table("finalizations") as batch_op:
        batch_op.add_column(sa.Column("revoked_at", sa.DateTime(timezone=True)))
    _replace_check(
        "finalizations",
        ("ck_finalization_active_only", "ck_finalization_status"),
        "ck_finalization_status",
        "status in ('active','revoked')",
    )
    _replace_check(
        "finalizations",
        ("ck_finalizations_revoked_at",),
        "ck_finalizations_revoked_at",
        "(status = 'active' AND revoked_at IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL)",
    )
    _replace_check(
        "package_snapshots",
        ("ck_package_snapshots_status",),
        "ck_package_snapshots_status",
        "status in ('preparing','ready','failed','expired','invalidated')",
    )
    _create_finalization_package_invalidations()
    _replace_governance_triggers()
    _backfill_issue_workflow_state()


def downgrade() -> None:
    revoked = op.get_bind().execute(sa.text("SELECT 1 FROM finalizations WHERE status = 'revoked' LIMIT 1")).first()
    invalidated = op.get_bind().execute(sa.text("SELECT 1 FROM package_snapshots WHERE status = 'invalidated' LIMIT 1")).first()
    if revoked is not None or invalidated is not None:
        raise RuntimeError("cannot downgrade revocation schema after revocation data exists")

    op.drop_table("finalization_package_invalidations")
    _drop_sqlite_finalization_triggers()
    _replace_check(
        "package_snapshots",
        ("ck_package_snapshots_status",),
        "ck_package_snapshots_status",
        "status in ('preparing','ready','failed','expired')",
    )
    _replace_check(
        "finalizations",
        ("ck_finalizations_revoked_at", "ck_finalization_status"),
        "ck_finalization_active_only",
        "status = 'active'",
    )
    with op.batch_alter_table("finalizations") as batch_op:
        batch_op.drop_column("revoked_at")
    _restore_governance_triggers()
    op.drop_table("media_derivative_tasks")
