"""persist thumbnail variants and allow task-backed representative-frame switching

Revision ID: 20260804_0022
Revises: 20260803_0021
Create Date: 2026-08-04
"""

from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic import op


revision = "20260804_0022"
down_revision = "20260803_0021"
branch_labels = None
depends_on = None


def _previous() -> object:
    return importlib.import_module(
        "backend.alembic.versions.20260803_0021_finalized_derivative_publication"
    )


def _sqlite_derivative_exception() -> str:
    previous = _previous()
    original = previous._sqlite_derivative_exception()  # type: ignore[attr-defined]
    return original.replace(
        "OLD.thumbnail_asset_id IS NULL\n          AND NEW.thumbnail_asset_id IS NOT NULL",
        "NEW.thumbnail_asset_id IS NOT OLD.thumbnail_asset_id\n          AND NEW.thumbnail_asset_id IS NOT NULL",
    )


def _postgres_derivative_exception() -> str:
    previous = _previous()
    original = previous._postgres_derivative_exception()  # type: ignore[attr-defined]
    return original.replace(
        "OLD.thumbnail_asset_id IS NULL\n                  AND NEW.thumbnail_asset_id IS NOT NULL",
        "NEW.thumbnail_asset_id IS DISTINCT FROM OLD.thumbnail_asset_id\n                  AND NEW.thumbnail_asset_id IS NOT NULL",
    )


def _install_trigger_policy(*, allow_replacement: bool) -> None:
    previous = _previous()
    dialect = op.get_bind().dialect.name
    if not allow_replacement:
        if dialect == "sqlite":
            previous._replace_sqlite_review_version_update_trigger(  # type: ignore[attr-defined]
                allow_derivatives=True
            )
        elif dialect == "postgresql":
            previous._replace_postgresql_identity_freeze(  # type: ignore[attr-defined]
                allow_derivatives=True
            )
        return

    attribute = (
        "_sqlite_derivative_exception"
        if dialect == "sqlite"
        else "_postgres_derivative_exception"
    )
    replacement_sql = (
        _sqlite_derivative_exception()
        if dialect == "sqlite"
        else _postgres_derivative_exception()
    )

    def replacement() -> str:
        return replacement_sql

    original = getattr(previous, attribute)
    setattr(previous, attribute, replacement)
    try:
        if dialect == "sqlite":
            previous._replace_sqlite_review_version_update_trigger(  # type: ignore[attr-defined]
                allow_derivatives=True
            )
        elif dialect == "postgresql":
            previous._replace_postgresql_identity_freeze(  # type: ignore[attr-defined]
                allow_derivatives=True
            )
    finally:
        setattr(previous, attribute, original)


def _normalize_thumbnail_selection_for_downgrade() -> None:
    """Restore the frame-zero selection expected by pre-0022 workers.

    The 3s file rows and blobs are deliberately retained as unreferenced
    rollback data.  A schema migration cannot safely unlink storage files;
    normal controlled cleanup can reclaim them after the downgrade is accepted.
    """
    connection = op.get_bind()
    dialect = connection.dialect.name
    if dialect == "sqlite":
        zero_id = "json_extract(task.result_details, '$.variants.\"0\"')"
        fallback_id = "json_extract(task.result_details, '$.variants.\"3000\"')"
        differs = "IS NOT"
    elif dialect == "postgresql":
        zero_id = "task.result_details -> 'variants' ->> '0'"
        fallback_id = "task.result_details -> 'variants' ->> '3000'"
        differs = "IS DISTINCT FROM"
    else:
        return

    missing_frame_zero = connection.scalar(
        sa.text(
            f"""
            SELECT COUNT(*)
            FROM media_derivative_tasks AS task
            JOIN review_versions AS version
              ON version.id = task.version_id
             AND version.project_ref_id = task.project_ref_id
             AND version.review_item_id = task.review_item_id
            WHERE task.kind = 'thumbnail'
              AND {fallback_id} IS NOT NULL
              AND (
                task.output_file_id = {fallback_id}
                OR version.thumbnail_asset_id = {fallback_id}
              )
              AND (
                {zero_id} IS NULL
                OR {zero_id} = ''
                OR NOT EXISTS (
                  SELECT 1 FROM file_objects AS output
                  WHERE output.id = {zero_id}
                    AND output.mime_type = 'image/jpeg'
                )
              )
            """
        )
    )
    if missing_frame_zero:
        raise RuntimeError(
            "cannot downgrade thumbnail fallback selection without a valid frame-zero variant"
        )

    op.execute(
        sa.text(
            f"""
            UPDATE media_derivative_tasks AS task
            SET status = 'ready',
                output_file_id = {zero_id},
                next_attempt_at = NULL,
                lease_id = NULL,
                lease_expires_at = NULL,
                error_code = NULL,
                failure_details = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE task.kind = 'thumbnail'
              AND {zero_id} IS NOT NULL
              AND {zero_id} != ''
              AND EXISTS (
                SELECT 1 FROM file_objects AS output
                WHERE output.id = {zero_id}
                  AND output.mime_type = 'image/jpeg'
              )
            """
        )
    )
    if dialect == "sqlite":
        op.execute(
            sa.text(
                f"""
                UPDATE review_versions
                SET thumbnail_asset_id = (
                  SELECT task.output_file_id
                  FROM media_derivative_tasks AS task
                  WHERE task.version_id = review_versions.id
                    AND task.project_ref_id = review_versions.project_ref_id
                    AND task.review_item_id = review_versions.review_item_id
                    AND task.kind = 'thumbnail'
                    AND task.status = 'ready'
                  LIMIT 1
                )
                WHERE EXISTS (
                  SELECT 1
                  FROM media_derivative_tasks AS task
                  WHERE task.version_id = review_versions.id
                    AND task.project_ref_id = review_versions.project_ref_id
                    AND task.review_item_id = review_versions.review_item_id
                    AND task.kind = 'thumbnail'
                    AND task.status = 'ready'
                    AND review_versions.thumbnail_asset_id {differs} task.output_file_id
                )
                """
            )
        )
    else:
        op.execute(
            sa.text(
                f"""
                UPDATE review_versions AS version
                SET thumbnail_asset_id = task.output_file_id
                FROM media_derivative_tasks AS task
                WHERE task.version_id = version.id
                  AND task.project_ref_id = version.project_ref_id
                  AND task.review_item_id = version.review_item_id
                  AND task.kind = 'thumbnail'
                  AND task.status = 'ready'
                  AND version.thumbnail_asset_id {differs} task.output_file_id
                """
            )
        )


def upgrade() -> None:
    op.add_column(
        "media_derivative_tasks",
        sa.Column("result_details", sa.JSON(), nullable=True),
    )
    _install_trigger_policy(allow_replacement=True)


def downgrade() -> None:
    _normalize_thumbnail_selection_for_downgrade()
    _install_trigger_policy(allow_replacement=False)
    op.drop_column("media_derivative_tasks", "result_details")
