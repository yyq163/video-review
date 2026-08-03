"""allow task-backed derivative pointers on finalized versions

Revision ID: 20260803_0021
Revises: 20260731_0020
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op


revision = "20260803_0021"
down_revision = "20260731_0020"
branch_labels = None
depends_on = None


def _dialect() -> str:
    return op.get_bind().dialect.name


def _sqlite_derivative_exception() -> str:
    immutable = """
      NEW.id IS OLD.id
      AND NEW.project_ref_id IS OLD.project_ref_id
      AND NEW.review_item_id IS OLD.review_item_id
      AND NEW.previous_version_id IS OLD.previous_version_id
      AND NEW.version_no IS OLD.version_no
      AND NEW.version_label IS OLD.version_label
      AND NEW.is_current IS OLD.is_current
      AND NEW.original_file_id IS OLD.original_file_id
      AND NEW.original_filename IS OLD.original_filename
      AND NEW.mime_type IS OLD.mime_type
      AND NEW.file_size IS OLD.file_size
      AND NEW.sha256 IS OLD.sha256
      AND NEW.duration_ms IS OLD.duration_ms
      AND NEW.width IS OLD.width
      AND NEW.height IS OLD.height
      AND NEW.fps_num IS OLD.fps_num
      AND NEW.fps_den IS OLD.fps_den
      AND NEW.media_probe_version IS OLD.media_probe_version
      AND NEW.version_note IS OLD.version_note
      AND NEW.change_summary IS OLD.change_summary
      AND NEW.lock_version IS OLD.lock_version
      AND NEW.created_at IS OLD.created_at
    """
    task_authority = """
      EXISTS (
        SELECT 1
        FROM media_derivative_tasks task
        JOIN file_objects output ON output.id = task.output_file_id
        JOIN file_objects source ON source.id = NEW.original_file_id
        WHERE task.version_id = NEW.id
          AND task.project_ref_id = NEW.project_ref_id
          AND task.review_item_id = NEW.review_item_id
          AND task.kind = {kind}
          AND task.status = 'ready'
          AND task.output_file_id = {asset}
          AND output.owner_principal_id = source.owner_principal_id
          AND output.owner_principal_kind = source.owner_principal_kind
          AND output.mime_type = {mime}
      )
    """
    playback = task_authority.format(
        kind="'playback_faststart'",
        asset="NEW.playback_asset_id",
        mime="'video/mp4'",
    )
    thumbnail = task_authority.format(
        kind="'thumbnail'",
        asset="NEW.thumbnail_asset_id",
        mime="'image/jpeg'",
    )
    return f"""(
      {immutable}
      AND (
        (
          OLD.playback_asset_id IS NULL
          AND NEW.playback_asset_id IS NOT NULL
          AND NEW.thumbnail_asset_id IS OLD.thumbnail_asset_id
          AND {playback}
        )
        OR (
          OLD.thumbnail_asset_id IS NULL
          AND NEW.thumbnail_asset_id IS NOT NULL
          AND NEW.playback_asset_id IS OLD.playback_asset_id
          AND {thumbnail}
        )
      )
    )"""


def _replace_sqlite_review_version_update_trigger(*, allow_derivatives: bool) -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_review_versions_freeze_after_finalized_update")
    exception = _sqlite_derivative_exception() if allow_derivatives else "0"
    op.execute(
        f"""
        CREATE TRIGGER trg_review_versions_freeze_after_finalized_update
        BEFORE UPDATE ON review_versions
        WHEN EXISTS (
          SELECT 1 FROM review_items i
          WHERE i.id = OLD.review_item_id
            AND i.project_ref_id = OLD.project_ref_id
            AND i.workflow_status = 'finalized'
        )
        AND NOT {exception}
        BEGIN
          SELECT RAISE(ABORT, 'finalized review item is frozen');
        END
        """
    )


def _postgres_derivative_exception() -> str:
    task_authority = """
      EXISTS (
        SELECT 1
        FROM media_derivative_tasks task
        JOIN file_objects output ON output.id = task.output_file_id
        JOIN file_objects source ON source.id = NEW.original_file_id
        WHERE task.version_id = NEW.id
          AND task.project_ref_id = NEW.project_ref_id
          AND task.review_item_id = NEW.review_item_id
          AND task.kind = {kind}
          AND task.status = 'ready'
          AND task.output_file_id = {asset}
          AND output.owner_principal_id = source.owner_principal_id
          AND output.owner_principal_kind = source.owner_principal_kind
          AND output.mime_type = {mime}
      )
    """
    playback = task_authority.format(
        kind="'playback_faststart'",
        asset="NEW.playback_asset_id",
        mime="'video/mp4'",
    )
    thumbnail = task_authority.format(
        kind="'thumbnail'",
        asset="NEW.thumbnail_asset_id",
        mime="'image/jpeg'",
    )
    # Trigger records are table-specific. Keep review_versions-only field access
    # inside a branch that is never executed for finalizations, issue revisions,
    # or the other tables sharing this trigger function.
    return f"""
          IF TG_TABLE_NAME = 'review_versions' AND TG_OP = 'UPDATE' THEN
            allow_derivative_publication := (
              (to_jsonb(NEW) - 'playback_asset_id' - 'thumbnail_asset_id')
                  = (to_jsonb(OLD) - 'playback_asset_id' - 'thumbnail_asset_id')
              AND (
                (
                  OLD.playback_asset_id IS NULL
                  AND NEW.playback_asset_id IS NOT NULL
                  AND NEW.thumbnail_asset_id IS NOT DISTINCT FROM OLD.thumbnail_asset_id
                  AND {playback}
                )
                OR (
                  OLD.thumbnail_asset_id IS NULL
                  AND NEW.thumbnail_asset_id IS NOT NULL
                  AND NEW.playback_asset_id IS NOT DISTINCT FROM OLD.playback_asset_id
                  AND {thumbnail}
                )
              )
            );
          END IF;
    """


def _replace_postgresql_identity_freeze(*, allow_derivatives: bool) -> None:
    derivative_authorization = _postgres_derivative_exception() if allow_derivatives else ""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION fcr_check_identity_and_freeze() RETURNS trigger AS $$
        DECLARE
          ref_project_id text;
          ref_review_item_id text;
          allow_derivative_publication boolean := FALSE;
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
          IF EXISTS (
            SELECT 1 FROM review_items i
            WHERE i.id = ref_review_item_id
              AND i.project_ref_id = ref_project_id
              AND i.workflow_status = 'finalized'
          ) AND NOT allow_derivative_publication THEN
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


def upgrade() -> None:
    if _dialect() == "sqlite":
        _replace_sqlite_review_version_update_trigger(allow_derivatives=True)
    elif _dialect() == "postgresql":
        _replace_postgresql_identity_freeze(allow_derivatives=True)


def downgrade() -> None:
    if _dialect() == "sqlite":
        _replace_sqlite_review_version_update_trigger(allow_derivatives=False)
    elif _dialect() == "postgresql":
        _replace_postgresql_identity_freeze(allow_derivatives=False)
