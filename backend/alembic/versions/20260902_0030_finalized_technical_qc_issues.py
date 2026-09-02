"""allow claim-backed technical QC issues on finalized current versions

Revision ID: 20260902_0030
Revises: 20260826_0029
Create Date: 2026-09-02
"""

from __future__ import annotations

import importlib

from alembic import op


revision = "20260902_0030"
down_revision = "20260826_0029"
branch_labels = None
depends_on = None


def _dialect() -> str:
    return op.get_bind().dialect.name


def _replace_finding_issue_foreign_key(*, deferred: bool) -> None:
    recreate = "always" if _dialect() == "sqlite" else "auto"
    with op.batch_alter_table("technical_qc_findings", recreate=recreate) as batch:
        batch.drop_constraint(
            "fk_technical_qc_findings_issue_identity",
            type_="foreignkey",
        )
        options: dict[str, object] = {}
        if deferred:
            options.update(deferrable=True, initially="DEFERRED")
        batch.create_foreign_key(
            "fk_technical_qc_findings_issue_identity",
            "review_issues",
            ["issue_id", "project_ref_id", "review_item_id", "version_id"],
            ["id", "project_ref_id", "review_item_id", "version_id"],
            ondelete="CASCADE",
            **options,
        )


def _sqlite_finalized_technical_qc_issue_authority() -> str:
    return """
      EXISTS (
        SELECT 1
        FROM technical_qc_findings finding
        JOIN technical_qc_tasks task
          ON task.id = finding.task_id
         AND task.project_ref_id = finding.project_ref_id
         AND task.review_item_id = finding.review_item_id
         AND task.version_id = finding.version_id
        JOIN review_items item
          ON item.id = finding.review_item_id
         AND item.project_ref_id = finding.project_ref_id
        JOIN review_versions version
          ON version.id = finding.version_id
         AND version.project_ref_id = finding.project_ref_id
         AND version.review_item_id = finding.review_item_id
        JOIN media_derivative_tasks playback
          ON playback.version_id = task.version_id
         AND playback.project_ref_id = task.project_ref_id
         AND playback.review_item_id = task.review_item_id
         AND playback.kind = 'playback_faststart'
        JOIN finalizations finalization
          ON finalization.id = item.active_finalization_id
         AND finalization.project_ref_id = item.project_ref_id
         AND finalization.review_item_id = item.id
         AND finalization.version_id = item.current_version_id
        WHERE finding.issue_id = NEW.id
          AND finding.project_ref_id = NEW.project_ref_id
          AND finding.review_item_id = NEW.review_item_id
          AND finding.version_id = NEW.version_id
          AND finding.start_ms = NEW.timestamp_ms
          AND finding.start_frame = NEW.frame_number
          AND task.status = 'running'
          AND task.claim_id IS NOT NULL
          AND length(trim(task.claim_id)) > 0
          AND task.lease_expires_at IS NOT NULL
          AND playback.status = 'ready'
          AND playback.output_file_id = task.input_file_id
          AND version.playback_asset_id = task.input_file_id
          AND item.workflow_status = 'finalized'
          AND item.current_version_id = NEW.version_id
          AND item.current_version_is_current = 1
          AND version.is_current = 1
          AND finalization.status = 'active'
          AND NEW.status = 'unresolved'
          AND NEW.deleted_at IS NULL
          AND NEW.lock_version = 1
      )
    """


def _sqlite_linked_technical_qc_issue(alias: str) -> str:
    return f"""
      EXISTS (
        SELECT 1
        FROM technical_qc_findings finding
        WHERE finding.issue_id = {alias}.id
          AND finding.project_ref_id = {alias}.project_ref_id
          AND finding.review_item_id = {alias}.review_item_id
          AND finding.version_id = {alias}.version_id
      )
    """


def _replace_sqlite_technical_qc_freeze_exceptions(*, allow: bool) -> None:
    for table in ("review_issues", "issue_revisions", "annotation_sets"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_freeze_after_finalized_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_review_issues_freeze_after_finalized_update")

    if not allow:
        for table in ("review_issues", "issue_revisions", "annotation_sets"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_freeze_after_finalized_insert
                BEFORE INSERT ON {table}
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
            CREATE TRIGGER trg_review_issues_freeze_after_finalized_update
            BEFORE UPDATE ON review_issues
            WHEN EXISTS (
              SELECT 1 FROM review_items i
              WHERE i.id = OLD.review_item_id
                AND i.project_ref_id = OLD.project_ref_id
                AND i.workflow_status = 'finalized'
            )
            OR EXISTS (
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
        return

    issue_authority = _sqlite_finalized_technical_qc_issue_authority()
    op.execute(
        f"""
        CREATE TRIGGER trg_review_issues_freeze_after_finalized_insert
        BEFORE INSERT ON review_issues
        WHEN EXISTS (
          SELECT 1 FROM review_items i
          WHERE i.id = NEW.review_item_id
            AND i.project_ref_id = NEW.project_ref_id
            AND i.workflow_status = 'finalized'
        )
        AND NOT ({issue_authority})
        BEGIN
          SELECT RAISE(ABORT, 'finalized review item is frozen');
        END
        """
    )

    linked_old = _sqlite_linked_technical_qc_issue("OLD")
    op.execute(
        f"""
        CREATE TRIGGER trg_review_issues_freeze_after_finalized_update
        BEFORE UPDATE ON review_issues
        WHEN (
          EXISTS (
            SELECT 1 FROM review_items i
            WHERE i.id = OLD.review_item_id
              AND i.project_ref_id = OLD.project_ref_id
              AND i.workflow_status = 'finalized'
          )
          OR EXISTS (
            SELECT 1 FROM review_items i
            WHERE i.id = NEW.review_item_id
              AND i.project_ref_id = NEW.project_ref_id
              AND i.workflow_status = 'finalized'
          )
        )
        AND NOT (
          {linked_old}
          AND NEW.id IS OLD.id
          AND NEW.project_ref_id IS OLD.project_ref_id
          AND NEW.review_item_id IS OLD.review_item_id
          AND NEW.version_id IS OLD.version_id
          AND NEW.issue_no IS OLD.issue_no
          AND NEW.timestamp_ms IS OLD.timestamp_ms
          AND NEW.frame_number IS OLD.frame_number
          AND NEW.created_at IS OLD.created_at
        )
        BEGIN
          SELECT RAISE(ABORT, 'finalized review item is frozen');
        END
        """
    )

    for table in ("issue_revisions", "annotation_sets"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_freeze_after_finalized_insert
            BEFORE INSERT ON {table}
            WHEN EXISTS (
              SELECT 1 FROM review_items i
              WHERE i.id = NEW.review_item_id
                AND i.project_ref_id = NEW.project_ref_id
                AND i.workflow_status = 'finalized'
            )
            AND NOT EXISTS (
              SELECT 1
              FROM technical_qc_findings finding
              JOIN review_issues issue
                ON issue.id = finding.issue_id
               AND issue.project_ref_id = finding.project_ref_id
               AND issue.review_item_id = finding.review_item_id
               AND issue.version_id = finding.version_id
              WHERE issue.id = NEW.issue_id
                AND issue.project_ref_id = NEW.project_ref_id
                AND issue.review_item_id = NEW.review_item_id
                AND issue.version_id = NEW.version_id
            )
            BEGIN
              SELECT RAISE(ABORT, 'finalized review item is frozen');
            END
            """
        )


def _postgres_finalized_technical_qc_issue_authority() -> str:
    return """
      EXISTS (
        SELECT 1
        FROM technical_qc_findings finding
        JOIN technical_qc_tasks task
          ON task.id = finding.task_id
         AND task.project_ref_id = finding.project_ref_id
         AND task.review_item_id = finding.review_item_id
         AND task.version_id = finding.version_id
        JOIN review_items item
          ON item.id = finding.review_item_id
         AND item.project_ref_id = finding.project_ref_id
        JOIN review_versions version
          ON version.id = finding.version_id
         AND version.project_ref_id = finding.project_ref_id
         AND version.review_item_id = finding.review_item_id
        JOIN media_derivative_tasks playback
          ON playback.version_id = task.version_id
         AND playback.project_ref_id = task.project_ref_id
         AND playback.review_item_id = task.review_item_id
         AND playback.kind = 'playback_faststart'
        JOIN finalizations finalization
          ON finalization.id = item.active_finalization_id
         AND finalization.project_ref_id = item.project_ref_id
         AND finalization.review_item_id = item.id
         AND finalization.version_id = item.current_version_id
        WHERE finding.issue_id = NEW.id
          AND finding.project_ref_id = NEW.project_ref_id
          AND finding.review_item_id = NEW.review_item_id
          AND finding.version_id = NEW.version_id
          AND finding.start_ms = NEW.timestamp_ms
          AND finding.start_frame = NEW.frame_number
          AND task.status = 'running'
          AND task.claim_id IS NOT NULL
          AND length(trim(task.claim_id)) > 0
          AND task.lease_expires_at IS NOT NULL
          AND playback.status = 'ready'
          AND playback.output_file_id = task.input_file_id
          AND version.playback_asset_id = task.input_file_id
          AND item.workflow_status = 'finalized'
          AND item.current_version_id = NEW.version_id
          AND item.current_version_is_current IS TRUE
          AND version.is_current IS TRUE
          AND finalization.status = 'active'
          AND NEW.status = 'unresolved'
          AND NEW.deleted_at IS NULL
          AND NEW.lock_version = 1
      )
    """


def _replace_postgresql_identity_freeze() -> None:
    previous = importlib.import_module(
        "backend.alembic.versions.20260826_0029_repair_finalized_derivative_freeze",
    )
    derivative_authorization = previous._postgres_derivative_exception()  # type: ignore[attr-defined]
    issue_authority = _postgres_finalized_technical_qc_issue_authority()
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION fcr_check_identity_and_freeze() RETURNS trigger AS $$
        DECLARE
          ref_project_id text;
          ref_review_item_id text;
          allow_derivative_publication boolean := FALSE;
          allow_technical_qc_change boolean := FALSE;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            ref_project_id := OLD.project_ref_id;
            ref_review_item_id := OLD.review_item_id;
          ELSE
            ref_project_id := NEW.project_ref_id;
            ref_review_item_id := NEW.review_item_id;
          END IF;
          IF TG_TABLE_NAME = 'issue_revisions' AND TG_OP = 'INSERT' THEN
            IF NEW.annotation_set_id IS NOT NULL AND NOT EXISTS (
              SELECT 1 FROM annotation_sets a
              WHERE a.id = NEW.annotation_set_id
                AND a.project_ref_id = NEW.project_ref_id
                AND a.review_item_id = NEW.review_item_id
                AND a.version_id = NEW.version_id
                AND a.issue_id = NEW.issue_id
            ) THEN
              RAISE EXCEPTION 'issue revision annotation identity mismatch';
            END IF;
          END IF;
          {derivative_authorization}
          IF TG_TABLE_NAME = 'review_issues' AND TG_OP = 'INSERT' THEN
            allow_technical_qc_change := ({issue_authority});
          ELSIF TG_TABLE_NAME = 'review_issues' AND TG_OP = 'UPDATE' THEN
            allow_technical_qc_change := (
              (to_jsonb(NEW)
                - 'current_revision_id' - 'status' - 'deleted_at'
                - 'lock_version' - 'updated_at')
              = (to_jsonb(OLD)
                - 'current_revision_id' - 'status' - 'deleted_at'
                - 'lock_version' - 'updated_at')
              AND EXISTS (
                SELECT 1
                FROM technical_qc_findings finding
                WHERE finding.issue_id = OLD.id
                  AND finding.project_ref_id = OLD.project_ref_id
                  AND finding.review_item_id = OLD.review_item_id
                  AND finding.version_id = OLD.version_id
              )
            );
          ELSIF TG_TABLE_NAME IN ('issue_revisions', 'annotation_sets')
              AND TG_OP = 'INSERT' THEN
            allow_technical_qc_change := EXISTS (
              SELECT 1
              FROM technical_qc_findings finding
              JOIN review_issues issue
                ON issue.id = finding.issue_id
               AND issue.project_ref_id = finding.project_ref_id
               AND issue.review_item_id = finding.review_item_id
               AND issue.version_id = finding.version_id
              WHERE issue.id = NEW.issue_id
                AND issue.project_ref_id = NEW.project_ref_id
                AND issue.review_item_id = NEW.review_item_id
                AND issue.version_id = NEW.version_id
            );
          END IF;
          IF (
            (
              TG_OP = 'UPDATE'
              AND EXISTS (
                SELECT 1 FROM review_items i
                WHERE i.id = OLD.review_item_id
                  AND i.project_ref_id = OLD.project_ref_id
                  AND i.workflow_status = 'finalized'
              )
            )
            OR EXISTS (
              SELECT 1 FROM review_items i
              WHERE i.id = ref_review_item_id
                AND i.project_ref_id = ref_project_id
                AND i.workflow_status = 'finalized'
            )
          ) AND NOT allow_derivative_publication
            AND NOT allow_technical_qc_change THEN
            RAISE EXCEPTION 'finalized review item is frozen';
          END IF;
          IF TG_OP = 'DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def _restore_0029_identity_freeze() -> None:
    previous = importlib.import_module(
        "backend.alembic.versions.20260826_0029_repair_finalized_derivative_freeze",
    )
    if _dialect() == "sqlite":
        previous._replace_sqlite_review_version_update_trigger(  # type: ignore[attr-defined]
            allow_derivatives=True,
        )
        previous._replace_sqlite_identity_freeze_update_triggers()  # type: ignore[attr-defined]
        _replace_sqlite_technical_qc_freeze_exceptions(allow=False)
    elif _dialect() == "postgresql":
        previous._replace_postgresql_identity_freeze(  # type: ignore[attr-defined]
            allow_derivatives=True,
        )


def upgrade() -> None:
    _replace_finding_issue_foreign_key(deferred=True)
    if _dialect() == "sqlite":
        _replace_sqlite_technical_qc_freeze_exceptions(allow=True)
    elif _dialect() == "postgresql":
        _replace_postgresql_identity_freeze()


def downgrade() -> None:
    _restore_0029_identity_freeze()
    _replace_finding_issue_foreign_key(deferred=False)
