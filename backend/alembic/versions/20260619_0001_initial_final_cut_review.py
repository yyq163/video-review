"""initial final cut review schema

Revision ID: 20260619_0001
Revises:
Create Date: 2026-06-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260619_0001"
down_revision = None
branch_labels = None
depends_on = None


def _dialect() -> str:
    return op.get_bind().dialect.name


def _now() -> sa.TextClause:
    return sa.text("CURRENT_TIMESTAMP")


def _sqlite_constraints(*constraints: sa.Constraint) -> list[sa.Constraint]:
    return list(constraints) if _dialect() == "sqlite" else []


def _create_unique_constraint(name: str, table: str, columns: list[str]) -> None:
    if _dialect() != "sqlite":
        op.create_unique_constraint(name, table, columns)


def _create_check_constraint(name: str, table: str, condition: str) -> None:
    if _dialect() != "sqlite":
        op.create_check_constraint(name, table, condition)


def _create_foreign_key(
    name: str,
    source_table: str,
    referent_table: str,
    local_cols: list[str],
    remote_cols: list[str],
    *,
    ondelete: str = "RESTRICT",
    deferrable: bool | None = None,
    initially: str | None = None,
) -> None:
    if _dialect() != "sqlite":
        op.create_foreign_key(name, source_table, referent_table, local_cols, remote_cols, ondelete=ondelete, deferrable=deferrable, initially=initially)


def upgrade() -> None:
    op.create_table(
        "project_refs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_code", sa.String(length=128), nullable=False),
        sa.Column("project_name", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("external_project_id", sa.String(length=128), nullable=True),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_project_refs"),
        *_sqlite_constraints(
            sa.UniqueConstraint("project_code", name="uq_project_refs_project_code"),
            sa.UniqueConstraint("source", "external_project_id", name="uq_project_refs_source_external_project_id"),
            sa.CheckConstraint("source in ('local','host')", name="ck_project_refs_source"),
            sa.CheckConstraint("lifecycle_status in ('active','archived')", name="ck_project_refs_lifecycle_status"),
            sa.CheckConstraint("lock_version >= 1", name="ck_project_refs_lock_version"),
        ),
    )
    _create_unique_constraint("uq_project_refs_project_code", "project_refs", ["project_code"])
    _create_unique_constraint(
        "uq_project_refs_source_external_project_id",
        "project_refs",
        ["source", "external_project_id"],
    )
    _create_check_constraint("ck_project_refs_source", "project_refs", "source in ('local','host')")
    _create_check_constraint(
        "ck_project_refs_lifecycle_status",
        "project_refs",
        "lifecycle_status in ('active','archived')",
    )
    _create_check_constraint("ck_project_refs_lock_version", "project_refs", "lock_version >= 1")
    op.create_index("ix_project_refs_lifecycle_status", "project_refs", ["lifecycle_status"])

    op.create_table(
        "file_objects",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("owner_principal_id", sa.String(length=128), nullable=False),
        sa.Column("owner_principal_kind", sa.String(length=32), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("fps_num", sa.Integer(), nullable=False),
        sa.Column("fps_den", sa.Integer(), nullable=False),
        sa.Column("media_probe_version", sa.String(length=64), nullable=False, server_default="local-probe-v1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_file_objects"),
        *_sqlite_constraints(
            sa.CheckConstraint("file_size > 0", name="ck_file_objects_file_size"),
            sa.CheckConstraint("duration_ms > 0", name="ck_file_objects_duration_ms"),
            sa.CheckConstraint("width > 0 and height > 0", name="ck_file_objects_dimensions"),
            sa.CheckConstraint("fps_num > 0 and fps_den > 0", name="ck_file_objects_fps"),
            sa.CheckConstraint("length(sha256) = 64", name="ck_file_objects_sha256_len"),
        ),
    )
    _create_check_constraint("ck_file_objects_file_size", "file_objects", "file_size > 0")
    _create_check_constraint("ck_file_objects_duration_ms", "file_objects", "duration_ms > 0")
    _create_check_constraint("ck_file_objects_dimensions", "file_objects", "width > 0 and height > 0")
    _create_check_constraint("ck_file_objects_fps", "file_objects", "fps_num > 0 and fps_den > 0")
    _create_check_constraint("ck_file_objects_sha256_len", "file_objects", "length(sha256) = 64")
    op.create_index("ix_file_objects_sha256", "file_objects", ["sha256"])
    op.create_index("ix_file_objects_owner", "file_objects", ["owner_principal_id"])

    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("declared_size", sa.BigInteger(), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="initiated"),
        sa.Column("temp_path", sa.String(length=1024), nullable=False),
        sa.Column("owner_principal_id", sa.String(length=128), nullable=False),
        sa.Column("owner_principal_kind", sa.String(length=32), nullable=False),
        sa.Column("received_parts", sa.JSON(), nullable=False),
        sa.Column("file_id", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("fps_num", sa.Integer(), nullable=False),
        sa.Column("fps_den", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_upload_sessions"),
        sa.ForeignKeyConstraint(["file_id"], ["file_objects.id"], name="fk_upload_sessions_file_id_file_objects", ondelete="RESTRICT"),
        *_sqlite_constraints(
            sa.CheckConstraint("declared_size > 0", name="ck_upload_sessions_declared_size"),
            sa.CheckConstraint("status in ('initiated','receiving','completed','aborted')", name="ck_upload_sessions_status"),
            sa.CheckConstraint("length(expected_sha256) = 64", name="ck_upload_sessions_sha256_len"),
        ),
    )
    _create_check_constraint("ck_upload_sessions_declared_size", "upload_sessions", "declared_size > 0")
    _create_check_constraint(
        "ck_upload_sessions_status",
        "upload_sessions",
        "status in ('initiated','receiving','completed','aborted')",
    )
    _create_check_constraint("ck_upload_sessions_sha256_len", "upload_sessions", "length(expected_sha256) = 64")
    op.create_index("ix_upload_sessions_status", "upload_sessions", ["status"])
    op.create_index("ix_upload_sessions_owner", "upload_sessions", ["owner_principal_id"])

    op.create_table(
        "review_items",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_ref_id", sa.String(length=64), nullable=False),
        sa.Column("item_code", sa.String(length=128), nullable=False),
        sa.Column("episode_no", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("workflow_status", sa.String(length=32), nullable=False, server_default="pending_review"),
        sa.Column("current_version_id", sa.String(length=64), nullable=True),
        sa.Column("current_version_is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("active_finalization_id", sa.String(length=64), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_review_items"),
        sa.ForeignKeyConstraint(["project_ref_id"], ["project_refs.id"], name="fk_review_items_project_ref_id_project_refs", ondelete="RESTRICT"),
        *_sqlite_constraints(
            sa.UniqueConstraint("project_ref_id", "item_code", name="uq_review_items_project_item_code"),
            sa.UniqueConstraint("id", "project_ref_id", name="uq_review_items_id_project"),
            sa.CheckConstraint(
                "workflow_status in ('pending_review','in_review','changes_requested','finalized')",
                name="ck_review_items_workflow_status",
            ),
            sa.CheckConstraint(
                "workflow_status != 'finalized' OR (active_finalization_id IS NOT NULL AND current_version_id IS NOT NULL)",
                name="ck_review_items_finalized_has_active_finalization",
            ),
            sa.CheckConstraint("current_version_id IS NULL OR current_version_is_current = 1", name="ck_review_items_current_version_true"),
            sa.CheckConstraint("lock_version >= 1", name="ck_review_items_lock_version"),
        ),
    )
    _create_unique_constraint("uq_review_items_project_item_code", "review_items", ["project_ref_id", "item_code"])
    _create_unique_constraint("uq_review_items_id_project", "review_items", ["id", "project_ref_id"])
    _create_check_constraint(
        "ck_review_items_workflow_status",
        "review_items",
        "workflow_status in ('pending_review','in_review','changes_requested','finalized')",
    )
    _create_check_constraint(
        "ck_review_items_finalized_has_active_finalization",
        "review_items",
        "workflow_status != 'finalized' OR (active_finalization_id IS NOT NULL AND current_version_id IS NOT NULL)",
    )
    _create_check_constraint(
        "ck_review_items_current_version_true",
        "review_items",
        "current_version_id IS NULL OR current_version_is_current IS TRUE",
    )
    _create_check_constraint("ck_review_items_lock_version", "review_items", "lock_version >= 1")
    op.create_index("ix_review_items_project_ref_id", "review_items", ["project_ref_id"])

    op.create_table(
        "review_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_ref_id", sa.String(length=64), nullable=False),
        sa.Column("review_item_id", sa.String(length=64), nullable=False),
        sa.Column("previous_version_id", sa.String(length=64), nullable=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("version_label", sa.String(length=32), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("original_file_id", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("fps_num", sa.Integer(), nullable=False),
        sa.Column("fps_den", sa.Integer(), nullable=False),
        sa.Column("media_probe_version", sa.String(length=64), nullable=False),
        sa.Column("playback_asset_id", sa.String(length=64), nullable=True),
        sa.Column("thumbnail_asset_id", sa.String(length=64), nullable=True),
        sa.Column("version_note", sa.Text(), nullable=True),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_review_versions"),
        sa.ForeignKeyConstraint(["project_ref_id"], ["project_refs.id"], name="fk_review_versions_project_ref_id_project_refs", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_item_id", "project_ref_id"], ["review_items.id", "review_items.project_ref_id"], name="fk_review_versions_item_project", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["original_file_id"], ["file_objects.id"], name="fk_review_versions_original_file_id_file_objects", ondelete="RESTRICT"),
        *_sqlite_constraints(
            sa.ForeignKeyConstraint(
                ["previous_version_id", "project_ref_id", "review_item_id"],
                ["review_versions.id", "review_versions.project_ref_id", "review_versions.review_item_id"],
                name="fk_review_versions_previous_identity",
                ondelete="RESTRICT",
            ),
            sa.UniqueConstraint("review_item_id", "version_no", name="uq_review_versions_item_version_no"),
            sa.UniqueConstraint("original_file_id", name="uq_review_versions_original_file_id"),
            sa.UniqueConstraint("id", "project_ref_id", "review_item_id", name="uq_review_versions_identity"),
            sa.UniqueConstraint("id", "project_ref_id", "review_item_id", "is_current", name="uq_review_versions_identity_current"),
            sa.UniqueConstraint("id", "project_ref_id", "review_item_id", "original_file_id", name="uq_review_versions_identity_file"),
            sa.CheckConstraint("version_no >= 1", name="ck_review_versions_version_no"),
            sa.CheckConstraint("file_size > 0", name="ck_review_versions_file_size"),
            sa.CheckConstraint("duration_ms > 0", name="ck_review_versions_duration_ms"),
            sa.CheckConstraint("width > 0 and height > 0", name="ck_review_versions_dimensions"),
            sa.CheckConstraint("fps_num > 0 and fps_den > 0", name="ck_review_versions_fps"),
            sa.CheckConstraint("lock_version >= 1", name="ck_review_versions_lock_version"),
        ),
    )
    _create_unique_constraint("uq_review_versions_item_version_no", "review_versions", ["review_item_id", "version_no"])
    _create_unique_constraint("uq_review_versions_original_file_id", "review_versions", ["original_file_id"])
    _create_unique_constraint(
        "uq_review_versions_identity",
        "review_versions",
        ["id", "project_ref_id", "review_item_id"],
    )
    _create_unique_constraint(
        "uq_review_versions_identity_current",
        "review_versions",
        ["id", "project_ref_id", "review_item_id", "is_current"],
    )
    _create_unique_constraint(
        "uq_review_versions_identity_file",
        "review_versions",
        ["id", "project_ref_id", "review_item_id", "original_file_id"],
    )
    _create_check_constraint("ck_review_versions_version_no", "review_versions", "version_no >= 1")
    _create_check_constraint("ck_review_versions_file_size", "review_versions", "file_size > 0")
    _create_check_constraint("ck_review_versions_duration_ms", "review_versions", "duration_ms > 0")
    _create_check_constraint("ck_review_versions_dimensions", "review_versions", "width > 0 and height > 0")
    _create_check_constraint("ck_review_versions_fps", "review_versions", "fps_num > 0 and fps_den > 0")
    _create_check_constraint("ck_review_versions_lock_version", "review_versions", "lock_version >= 1")
    _create_foreign_key(
        "fk_review_versions_previous_identity",
        "review_versions",
        "review_versions",
        ["previous_version_id", "project_ref_id", "review_item_id"],
        ["id", "project_ref_id", "review_item_id"],
    )
    op.create_index("ix_review_versions_project_item", "review_versions", ["project_ref_id", "review_item_id"])
    op.create_index(
        "uq_review_versions_current_true",
        "review_versions",
        ["review_item_id"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
        postgresql_where=sa.text("is_current IS TRUE"),
    )
    if _dialect() == "sqlite":
        with op.batch_alter_table("review_items", recreate="always") as batch_op:
            batch_op.create_foreign_key(
                "fk_review_items_current_version_identity",
                "review_versions",
                ["current_version_id", "project_ref_id", "id", "current_version_is_current"],
                ["id", "project_ref_id", "review_item_id", "is_current"],
                ondelete="RESTRICT",
            )
    else:
        op.create_foreign_key(
            "fk_review_items_current_version_identity",
            "review_items",
            "review_versions",
            ["current_version_id", "project_ref_id", "id", "current_version_is_current"],
            ["id", "project_ref_id", "review_item_id", "is_current"],
            ondelete="RESTRICT",
        )

    op.create_table(
        "review_issues",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_ref_id", sa.String(length=64), nullable=False),
        sa.Column("review_item_id", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("issue_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="unresolved"),
        sa.Column("current_revision_id", sa.String(length=64), nullable=False),
        sa.Column("timestamp_ms", sa.Integer(), nullable=False),
        sa.Column("frame_number", sa.Integer(), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_review_issues"),
        sa.ForeignKeyConstraint(["version_id", "project_ref_id", "review_item_id"], ["review_versions.id", "review_versions.project_ref_id", "review_versions.review_item_id"], name="fk_review_issues_version_identity", ondelete="RESTRICT"),
        *_sqlite_constraints(
            sa.UniqueConstraint("review_item_id", "issue_no", name="uq_review_issues_item_issue_no"),
            sa.UniqueConstraint("id", "project_ref_id", "review_item_id", "version_id", name="uq_review_issues_identity"),
            sa.CheckConstraint("status in ('unresolved','resolved')", name="ck_review_issues_status"),
            sa.CheckConstraint("timestamp_ms >= 0 and frame_number >= 0", name="ck_review_issues_time"),
            sa.CheckConstraint("lock_version >= 1", name="ck_review_issues_lock_version"),
        ),
    )
    _create_unique_constraint("uq_review_issues_item_issue_no", "review_issues", ["review_item_id", "issue_no"])
    _create_unique_constraint(
        "uq_review_issues_identity",
        "review_issues",
        ["id", "project_ref_id", "review_item_id", "version_id"],
    )
    _create_check_constraint("ck_review_issues_status", "review_issues", "status in ('unresolved','resolved')")
    _create_check_constraint("ck_review_issues_time", "review_issues", "timestamp_ms >= 0 and frame_number >= 0")
    _create_check_constraint("ck_review_issues_lock_version", "review_issues", "lock_version >= 1")
    op.create_index("ix_review_issues_version", "review_issues", ["project_ref_id", "review_item_id", "version_id"])

    op.create_table(
        "annotation_sets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_ref_id", sa.String(length=64), nullable=False),
        sa.Column("review_item_id", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("issue_id", sa.String(length=64), nullable=False),
        sa.Column("timestamp_ms", sa.Integer(), nullable=False),
        sa.Column("frame_number", sa.Integer(), nullable=False),
        sa.Column("canvas_width", sa.Integer(), nullable=False),
        sa.Column("canvas_height", sa.Integer(), nullable=False),
        sa.Column("video_width", sa.Integer(), nullable=False),
        sa.Column("video_height", sa.Integer(), nullable=False),
        sa.Column("shapes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_annotation_sets"),
        sa.ForeignKeyConstraint(["issue_id", "project_ref_id", "review_item_id", "version_id"], ["review_issues.id", "review_issues.project_ref_id", "review_issues.review_item_id", "review_issues.version_id"], name="fk_annotation_sets_issue_identity", ondelete="RESTRICT"),
        *_sqlite_constraints(
            sa.UniqueConstraint("id", "project_ref_id", "review_item_id", "version_id", "issue_id", name="uq_annotation_sets_identity"),
            sa.CheckConstraint("timestamp_ms >= 0 and frame_number >= 0", name="ck_annotation_sets_time"),
            sa.CheckConstraint("canvas_width > 0 and canvas_height > 0 and video_width > 0 and video_height > 0", name="ck_annotation_sets_dimensions"),
        ),
    )
    _create_unique_constraint(
        "uq_annotation_sets_identity",
        "annotation_sets",
        ["id", "project_ref_id", "review_item_id", "version_id", "issue_id"],
    )
    _create_check_constraint("ck_annotation_sets_time", "annotation_sets", "timestamp_ms >= 0 and frame_number >= 0")
    _create_check_constraint(
        "ck_annotation_sets_dimensions",
        "annotation_sets",
        "canvas_width > 0 and canvas_height > 0 and video_width > 0 and video_height > 0",
    )

    op.create_table(
        "issue_revisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_ref_id", sa.String(length=64), nullable=False),
        sa.Column("review_item_id", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("issue_id", sa.String(length=64), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("annotation_set_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_issue_revisions"),
        sa.ForeignKeyConstraint(["issue_id", "project_ref_id", "review_item_id", "version_id"], ["review_issues.id", "review_issues.project_ref_id", "review_issues.review_item_id", "review_issues.version_id"], name="fk_issue_revisions_issue_identity", ondelete="RESTRICT"),
        *_sqlite_constraints(
            sa.UniqueConstraint("issue_id", "revision_no", name="uq_issue_revisions_issue_revision_no"),
            sa.UniqueConstraint("id", "issue_id", name="uq_issue_revisions_id_issue"),
            sa.UniqueConstraint("id", "issue_id", "project_ref_id", "review_item_id", "version_id", name="uq_issue_revisions_current_identity"),
            sa.UniqueConstraint("id", "project_ref_id", "review_item_id", "version_id", "issue_id", name="uq_issue_revisions_identity"),
            sa.CheckConstraint("revision_no >= 1", name="ck_issue_revisions_revision_no"),
        ),
    )
    _create_unique_constraint("uq_issue_revisions_issue_revision_no", "issue_revisions", ["issue_id", "revision_no"])
    _create_unique_constraint("uq_issue_revisions_id_issue", "issue_revisions", ["id", "issue_id"])
    _create_unique_constraint(
        "uq_issue_revisions_current_identity",
        "issue_revisions",
        ["id", "issue_id", "project_ref_id", "review_item_id", "version_id"],
    )
    _create_unique_constraint(
        "uq_issue_revisions_identity",
        "issue_revisions",
        ["id", "project_ref_id", "review_item_id", "version_id", "issue_id"],
    )
    _create_check_constraint("ck_issue_revisions_revision_no", "issue_revisions", "revision_no >= 1")
    op.create_index("ix_issue_revisions_issue", "issue_revisions", ["issue_id"])
    if _dialect() == "sqlite":
        with op.batch_alter_table("review_issues", recreate="always") as batch_op:
            batch_op.create_foreign_key(
                "fk_review_issues_current_revision_identity",
                "issue_revisions",
                ["current_revision_id", "id", "project_ref_id", "review_item_id", "version_id"],
                ["id", "issue_id", "project_ref_id", "review_item_id", "version_id"],
                ondelete="RESTRICT",
                deferrable=True,
                initially="DEFERRED",
            )
    else:
        op.create_foreign_key(
            "fk_review_issues_current_revision_identity",
            "review_issues",
            "issue_revisions",
            ["current_revision_id", "id", "project_ref_id", "review_item_id", "version_id"],
            ["id", "issue_id", "project_ref_id", "review_item_id", "version_id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        )

    op.create_table(
        "thread_messages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_ref_id", sa.String(length=64), nullable=False),
        sa.Column("review_item_id", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("issue_id", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_thread_messages"),
        sa.ForeignKeyConstraint(["issue_id", "project_ref_id", "review_item_id", "version_id"], ["review_issues.id", "review_issues.project_ref_id", "review_issues.review_item_id", "review_issues.version_id"], name="fk_thread_messages_issue_identity", ondelete="RESTRICT"),
    )
    op.create_index("ix_thread_messages_issue", "thread_messages", ["project_ref_id", "review_item_id", "version_id", "issue_id"])

    op.create_table(
        "review_decisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_ref_id", sa.String(length=64), nullable=False),
        sa.Column("review_item_id", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("decision_type", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_review_decisions"),
        sa.ForeignKeyConstraint(["version_id", "project_ref_id", "review_item_id"], ["review_versions.id", "review_versions.project_ref_id", "review_versions.review_item_id"], name="fk_review_decisions_version_identity", ondelete="RESTRICT"),
        *_sqlite_constraints(
            sa.CheckConstraint("decision_type in ('changes_requested','finalized')", name="ck_review_decisions_decision_type"),
        ),
    )
    _create_check_constraint(
        "ck_review_decisions_decision_type",
        "review_decisions",
        "decision_type in ('changes_requested','finalized')",
    )
    op.create_index("ix_review_decisions_version", "review_decisions", ["project_ref_id", "review_item_id", "version_id"])

    op.create_table(
        "finalizations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_ref_id", sa.String(length=64), nullable=False),
        sa.Column("review_item_id", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("original_file_id", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("fps_num", sa.Integer(), nullable=False),
        sa.Column("fps_den", sa.Integer(), nullable=False),
        sa.Column("media_probe_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_finalizations"),
        sa.ForeignKeyConstraint(["version_id", "project_ref_id", "review_item_id", "original_file_id"], ["review_versions.id", "review_versions.project_ref_id", "review_versions.review_item_id", "review_versions.original_file_id"], name="fk_finalizations_version_file_identity", ondelete="RESTRICT"),
        *_sqlite_constraints(
            sa.UniqueConstraint("id", "project_ref_id", "review_item_id", name="uq_finalizations_identity"),
            sa.UniqueConstraint("id", "project_ref_id", "review_item_id", "version_id", name="uq_finalizations_current_version_identity"),
            sa.CheckConstraint("status = 'active'", name="ck_finalization_active_only"),
            sa.CheckConstraint("version_no >= 1", name="ck_finalizations_version_no"),
            sa.CheckConstraint("file_size > 0", name="ck_finalizations_file_size"),
        ),
    )
    _create_unique_constraint("uq_finalizations_identity", "finalizations", ["id", "project_ref_id", "review_item_id"])
    _create_unique_constraint(
        "uq_finalizations_current_version_identity",
        "finalizations",
        ["id", "project_ref_id", "review_item_id", "version_id"],
    )
    _create_check_constraint("ck_finalization_active_only", "finalizations", "status = 'active'")
    _create_check_constraint("ck_finalizations_version_no", "finalizations", "version_no >= 1")
    _create_check_constraint("ck_finalizations_file_size", "finalizations", "file_size > 0")
    op.create_index(
        "uq_finalizations_active_review_item",
        "finalizations",
        ["review_item_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index("ix_finalizations_project_version", "finalizations", ["project_ref_id", "review_item_id", "version_id"])
    if _dialect() == "sqlite":
        with op.batch_alter_table("review_items", recreate="always") as batch_op:
            batch_op.create_foreign_key(
                "fk_review_items_active_finalization_current_version",
                "finalizations",
                ["active_finalization_id", "project_ref_id", "id", "current_version_id"],
                ["id", "project_ref_id", "review_item_id", "version_id"],
                ondelete="RESTRICT",
            )
    else:
        op.create_foreign_key(
            "fk_review_items_active_finalization_current_version",
            "review_items",
            "finalizations",
            ["active_finalization_id", "project_ref_id", "id", "current_version_id"],
            ["id", "project_ref_id", "review_item_id", "version_id"],
            ondelete="RESTRICT",
        )

    op.create_table(
        "package_snapshots",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_ref_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("package_filename", sa.String(length=512), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("failure_details", sa.JSON(), nullable=True),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_package_snapshots"),
        sa.ForeignKeyConstraint(["project_ref_id"], ["project_refs.id"], name="fk_package_snapshots_project_ref_id_project_refs", ondelete="RESTRICT"),
        *_sqlite_constraints(
            sa.UniqueConstraint("id", "project_ref_id", name="uq_package_snapshots_id_project"),
            sa.CheckConstraint("status in ('preparing','ready','failed','expired')", name="ck_package_snapshots_status"),
        ),
    )
    _create_unique_constraint("uq_package_snapshots_id_project", "package_snapshots", ["id", "project_ref_id"])
    _create_check_constraint("ck_package_snapshots_status", "package_snapshots", "status in ('preparing','ready','failed','expired')")
    op.create_index("ix_package_snapshots_project", "package_snapshots", ["project_ref_id"])

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=64), nullable=False),
        sa.Column("aggregate_version", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("project_ref_id", sa.String(length=64), nullable=False),
        sa.Column("review_item_id", sa.String(length=64), nullable=True),
        sa.Column("version_id", sa.String(length=64), nullable=True),
        sa.Column("issue_id", sa.String(length=64), nullable=True),
        sa.Column("finalization_id", sa.String(length=64), nullable=True),
        sa.Column("package_id", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("causation_id", sa.String(length=64), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        sa.ForeignKeyConstraint(["project_ref_id"], ["project_refs.id"], name="fk_outbox_events_project_ref_id_project_refs", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_item_id", "project_ref_id"], ["review_items.id", "review_items.project_ref_id"], name="fk_outbox_events_review_item_identity", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["version_id", "project_ref_id", "review_item_id"], ["review_versions.id", "review_versions.project_ref_id", "review_versions.review_item_id"], name="fk_outbox_events_version_identity", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["issue_id", "project_ref_id", "review_item_id", "version_id"], ["review_issues.id", "review_issues.project_ref_id", "review_issues.review_item_id", "review_issues.version_id"], name="fk_outbox_events_issue_identity", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["finalization_id", "project_ref_id", "review_item_id"], ["finalizations.id", "finalizations.project_ref_id", "finalizations.review_item_id"], name="fk_outbox_events_finalization_identity", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["package_id", "project_ref_id"], ["package_snapshots.id", "package_snapshots.project_ref_id"], name="fk_outbox_events_package_identity", ondelete="RESTRICT"),
        *_sqlite_constraints(
            sa.UniqueConstraint("event_id", name="uq_outbox_event_id"),
            sa.UniqueConstraint("sequence", name="uq_outbox_events_sequence"),
            sa.CheckConstraint("event_version >= 1", name="ck_outbox_events_event_version"),
            sa.CheckConstraint("aggregate_version >= 1", name="ck_outbox_events_aggregate_version"),
            sa.CheckConstraint("sequence >= 1", name="ck_outbox_events_sequence"),
            sa.CheckConstraint("status in ('pending','publishing','failed','dispatched')", name="ck_outbox_events_status"),
            sa.CheckConstraint("attempts >= 0", name="ck_outbox_events_attempts"),
        ),
    )
    _create_unique_constraint("uq_outbox_event_id", "outbox_events", ["event_id"])
    _create_unique_constraint("uq_outbox_events_sequence", "outbox_events", ["sequence"])
    _create_check_constraint("ck_outbox_events_event_version", "outbox_events", "event_version >= 1")
    _create_check_constraint("ck_outbox_events_aggregate_version", "outbox_events", "aggregate_version >= 1")
    _create_check_constraint("ck_outbox_events_sequence", "outbox_events", "sequence >= 1")
    _create_check_constraint(
        "ck_outbox_events_status",
        "outbox_events",
        "status in ('pending','publishing','failed','dispatched')",
    )
    _create_check_constraint("ck_outbox_events_attempts", "outbox_events", "attempts >= 0")
    op.create_index("ix_outbox_events_status_id", "outbox_events", ["status", "id"])

    op.create_table(
        "idempotency_records",
        sa.Column("key", sa.String(length=256), nullable=False),
        sa.Column("command_type", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("key", name="pk_idempotency_records"),
        *_sqlite_constraints(
            sa.CheckConstraint("length(request_hash) = 64", name="ck_idempotency_records_hash_len"),
            sa.CheckConstraint("status_code in (102, 200)", name="ck_idempotency_records_status_code"),
        ),
    )
    _create_check_constraint("ck_idempotency_records_hash_len", "idempotency_records", "length(request_hash) = 64")
    _create_check_constraint("ck_idempotency_records_status_code", "idempotency_records", "status_code in (102, 200)")
    op.create_index("ix_idempotency_records_command_type", "idempotency_records", ["command_type"])

    op.create_table(
        "operation_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("entry_source", sa.String(length=32), nullable=False),
        sa.Column("capability", sa.String(length=128), nullable=True),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_operation_logs"),
        *_sqlite_constraints(
            sa.CheckConstraint("entry_source in ('edit','review','embedded','unspecified')", name="ck_operation_logs_entry_source"),
            sa.CheckConstraint("result in ('ok','error')", name="ck_operation_logs_result"),
        ),
    )
    _create_check_constraint(
        "ck_operation_logs_entry_source",
        "operation_logs",
        "entry_source in ('edit','review','embedded','unspecified')",
    )
    _create_check_constraint("ck_operation_logs_result", "operation_logs", "result in ('ok','error')")
    op.create_index("ix_operation_logs_request_id", "operation_logs", ["request_id"])

    op.create_table(
        "outbox_consumer_receipts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("consumer_name", sa.String(length=128), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_consumer_receipts"),
        sa.ForeignKeyConstraint(["event_id"], ["outbox_events.event_id"], name="fk_outbox_consumer_receipts_event_id", ondelete="RESTRICT"),
        *_sqlite_constraints(
            sa.UniqueConstraint("event_id", "consumer_name", name="uq_outbox_consumer_event_consumer"),
        ),
    )
    _create_unique_constraint("uq_outbox_consumer_event_consumer", "outbox_consumer_receipts", ["event_id", "consumer_name"])

    _create_governance_triggers()


def _create_governance_triggers() -> None:
    dialect = _dialect()
    if dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_issue_revisions_no_update
            BEFORE UPDATE ON issue_revisions
            BEGIN
              SELECT RAISE(ABORT, 'issue_revisions are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_issue_revisions_no_delete
            BEFORE DELETE ON issue_revisions
            BEGIN
              SELECT RAISE(ABORT, 'issue_revisions are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_annotation_sets_no_update
            BEFORE UPDATE ON annotation_sets
            BEGIN
              SELECT RAISE(ABORT, 'annotation_sets are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_annotation_sets_no_delete
            BEFORE DELETE ON annotation_sets
            BEGIN
              SELECT RAISE(ABORT, 'annotation_sets are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_issue_revisions_annotation_identity
            BEFORE INSERT ON issue_revisions
            WHEN NEW.annotation_set_id IS NOT NULL
             AND NOT EXISTS (
               SELECT 1 FROM annotation_sets a
               WHERE a.id = NEW.annotation_set_id
                 AND a.project_ref_id = NEW.project_ref_id
                 AND a.review_item_id = NEW.review_item_id
                 AND a.version_id = NEW.version_id
                 AND a.issue_id = NEW.issue_id
             )
            BEGIN
              SELECT RAISE(ABORT, 'issue revision annotation identity mismatch');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_review_issues_current_revision_identity
            BEFORE UPDATE OF current_revision_id ON review_issues
            WHEN NEW.current_revision_id IS NOT NULL
             AND NOT EXISTS (
               SELECT 1 FROM issue_revisions r
               WHERE r.id = NEW.current_revision_id
                 AND r.issue_id = NEW.id
                 AND r.project_ref_id = NEW.project_ref_id
                 AND r.review_item_id = NEW.review_item_id
                 AND r.version_id = NEW.version_id
             )
            BEGIN
              SELECT RAISE(ABORT, 'current revision identity mismatch');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_review_items_current_version_identity
            BEFORE UPDATE OF current_version_id ON review_items
            WHEN NEW.current_version_id IS NOT NULL
             AND NOT EXISTS (
               SELECT 1 FROM review_versions v
               WHERE v.id = NEW.current_version_id
                 AND v.project_ref_id = NEW.project_ref_id
                 AND v.review_item_id = NEW.id
                 AND v.is_current = 1
             )
            BEGIN
              SELECT RAISE(ABORT, 'current version identity mismatch');
            END
            """
        )
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
        op.execute(
            """
            CREATE TRIGGER trg_review_items_no_delete_after_finalized
            BEFORE DELETE ON review_items
            WHEN OLD.workflow_status = 'finalized'
            BEGIN
              SELECT RAISE(ABORT, 'finalized review item is frozen');
            END
            """
        )
        for table in ("review_versions", "review_issues", "thread_messages", "review_decisions", "issue_revisions", "annotation_sets", "finalizations"):
            for event in ("INSERT", "UPDATE"):
                op.execute(
                    f"""
                    CREATE TRIGGER trg_{table}_freeze_after_finalized_{event.lower()}
                    BEFORE {event} ON {table}
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
                f"""
                CREATE TRIGGER trg_{table}_freeze_after_finalized_delete
                BEFORE DELETE ON {table}
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
        return

    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION fcr_raise() RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION '%', TG_ARGV[0];
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_issue_revisions_no_update
            BEFORE UPDATE ON issue_revisions
            FOR EACH ROW EXECUTE FUNCTION fcr_raise('issue_revisions are immutable')
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_issue_revisions_no_delete
            BEFORE DELETE ON issue_revisions
            FOR EACH ROW EXECUTE FUNCTION fcr_raise('issue_revisions are immutable')
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_annotation_sets_no_update
            BEFORE UPDATE ON annotation_sets
            FOR EACH ROW EXECUTE FUNCTION fcr_raise('annotation_sets are immutable')
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_annotation_sets_no_delete
            BEFORE DELETE ON annotation_sets
            FOR EACH ROW EXECUTE FUNCTION fcr_raise('annotation_sets are immutable')
            """
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION fcr_check_identity_and_freeze() RETURNS trigger AS $$
            DECLARE
              ref_project_id text;
              ref_review_item_id text;
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
              IF EXISTS (
                SELECT 1 FROM review_items i
                WHERE i.id = ref_review_item_id
                  AND i.project_ref_id = ref_project_id
                  AND i.workflow_status = 'finalized'
              ) THEN
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
        op.execute(
            """
            CREATE OR REPLACE FUNCTION fcr_check_review_issue_current_revision() RETURNS trigger AS $$
            BEGIN
              IF NEW.current_revision_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM issue_revisions r
                WHERE r.id = NEW.current_revision_id
                  AND r.issue_id = NEW.id
                  AND r.project_ref_id = NEW.project_ref_id
                  AND r.review_item_id = NEW.review_item_id
                  AND r.version_id = NEW.version_id
              ) THEN
                RAISE EXCEPTION 'current revision identity mismatch';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_review_issues_current_revision_identity
            BEFORE UPDATE OF current_revision_id ON review_issues
            FOR EACH ROW EXECUTE FUNCTION fcr_check_review_issue_current_revision()
            """
        )
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
        op.execute(
            """
            CREATE TRIGGER trg_review_items_refs_and_freeze
            BEFORE UPDATE ON review_items
            FOR EACH ROW EXECUTE FUNCTION fcr_check_review_item_refs_and_freeze()
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_review_items_no_delete_after_finalized
            BEFORE DELETE ON review_items
            FOR EACH ROW WHEN (OLD.workflow_status = 'finalized')
            EXECUTE FUNCTION fcr_raise('finalized review item is frozen')
            """
        )
        for table in ("issue_revisions", "review_versions", "review_issues", "thread_messages", "review_decisions", "annotation_sets", "finalizations"):
            for event in ("INSERT", "UPDATE", "DELETE"):
                op.execute(
                    f"""
                    CREATE TRIGGER trg_{table}_identity_freeze_{event.lower()}
                    BEFORE {event} ON {table}
                    FOR EACH ROW EXECUTE FUNCTION fcr_check_identity_and_freeze()
                    """
                )


def downgrade() -> None:
    dialect = _dialect()
    if dialect == "postgresql":
        for table in ("issue_revisions", "review_versions", "review_issues", "thread_messages", "review_decisions", "annotation_sets", "finalizations"):
            for event in ("insert", "update", "delete"):
                op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_identity_freeze_{event} ON {table}")
        op.execute("DROP TRIGGER IF EXISTS trg_review_items_no_delete_after_finalized ON review_items")
        op.execute("DROP TRIGGER IF EXISTS trg_review_items_refs_and_freeze ON review_items")
        op.execute("DROP TRIGGER IF EXISTS trg_review_issues_current_revision_identity ON review_issues")
        op.execute("DROP TRIGGER IF EXISTS trg_annotation_sets_no_delete ON annotation_sets")
        op.execute("DROP TRIGGER IF EXISTS trg_annotation_sets_no_update ON annotation_sets")
        op.execute("DROP TRIGGER IF EXISTS trg_issue_revisions_no_delete ON issue_revisions")
        op.execute("DROP TRIGGER IF EXISTS trg_issue_revisions_no_update ON issue_revisions")
        op.execute("DROP FUNCTION IF EXISTS fcr_check_review_item_refs_and_freeze()")
        op.execute("DROP FUNCTION IF EXISTS fcr_check_review_issue_current_revision()")
        op.execute("DROP FUNCTION IF EXISTS fcr_check_identity_and_freeze()")
        op.execute("DROP FUNCTION IF EXISTS fcr_raise()")
        for table in (
            "outbox_consumer_receipts",
            "operation_logs",
            "idempotency_records",
            "outbox_events",
            "package_snapshots",
            "finalizations",
            "review_decisions",
            "thread_messages",
            "issue_revisions",
            "annotation_sets",
            "review_issues",
            "review_versions",
            "review_items",
            "upload_sessions",
            "file_objects",
            "project_refs",
        ):
            op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        return
    elif dialect == "sqlite":
        for trigger in (
            "trg_issue_revisions_no_update",
            "trg_issue_revisions_no_delete",
            "trg_annotation_sets_no_update",
            "trg_annotation_sets_no_delete",
            "trg_issue_revisions_annotation_identity",
            "trg_review_issues_current_revision_identity",
            "trg_review_items_current_version_identity",
            "trg_review_items_finalization_identity",
            "trg_review_items_freeze_after_finalized",
            "trg_review_items_no_delete_after_finalized",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        for table in ("review_versions", "review_issues", "thread_messages", "review_decisions", "issue_revisions", "annotation_sets", "finalizations"):
            for event in ("insert", "update", "delete"):
                op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_freeze_after_finalized_{event}")

    existing = set(inspect(op.get_bind()).get_table_names())
    for table in (
        "outbox_consumer_receipts",
        "operation_logs",
        "idempotency_records",
        "outbox_events",
        "package_snapshots",
        "finalizations",
        "review_decisions",
        "thread_messages",
        "issue_revisions",
        "annotation_sets",
        "review_issues",
        "review_versions",
        "review_items",
        "upload_sessions",
        "file_objects",
        "project_refs",
    ):
        if table in existing:
            op.drop_table(table)
