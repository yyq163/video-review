"""add shared frame-rate, editor, and durable deletion contract foundations

Revision ID: 20260824_0023
Revises: 20260804_0022
Create Date: 2026-08-24
"""

from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic import op


revision = "20260824_0023"
down_revision = "20260804_0022"
branch_labels = None
depends_on = None


def _previous() -> object:
    return importlib.import_module(
        "backend.alembic.versions.20260804_0022_thumbnail_similarity_fallback"
    )


def _create_sqlite_contract_checks() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    for event in ("INSERT", "UPDATE"):
        op.execute(
            f"""
            CREATE TRIGGER trg_project_refs_fps_pair_{event.lower()}
            BEFORE {event} ON project_refs
            WHEN NOT (
              (NEW.fps_num IS NULL AND NEW.fps_den IS NULL)
              OR (
                NEW.fps_num IS NOT NULL AND NEW.fps_den IS NOT NULL
                AND NEW.fps_num > 0 AND NEW.fps_den > 0
              )
            )
            BEGIN
              SELECT RAISE(ABORT, 'project frame rate must be null together or positive together');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_review_versions_responsible_editor_pair_{event.lower()}
            BEFORE {event} ON review_versions
            WHEN NOT (
              (NEW.responsible_editor_id IS NULL AND NEW.responsible_editor_name_snapshot IS NULL)
              OR (
                NEW.responsible_editor_id IS NOT NULL
                AND NEW.responsible_editor_name_snapshot IS NOT NULL
                AND length(trim(NEW.responsible_editor_name_snapshot)) BETWEEN 1 AND 255
              )
            )
            BEGIN
              SELECT RAISE(ABORT, 'responsible editor identity and snapshot must be present together');
            END
            """
        )


def _drop_sqlite_contract_checks() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    for event in ("insert", "update"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_project_refs_fps_pair_{event}")
        op.execute(f"DROP TRIGGER IF EXISTS trg_review_versions_responsible_editor_pair_{event}")


def _install_sqlite_review_version_freeze() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    previous = _previous()
    original = previous._sqlite_derivative_exception  # type: ignore[attr-defined]
    original_sql = original()
    replacement_sql = original_sql.replace(
        "AND NEW.media_probe_version IS OLD.media_probe_version\n"
        "      AND NEW.version_note IS OLD.version_note",
        "AND NEW.media_probe_version IS OLD.media_probe_version\n"
        "      AND NEW.responsible_editor_id IS OLD.responsible_editor_id\n"
        "      AND NEW.responsible_editor_name_snapshot IS OLD.responsible_editor_name_snapshot\n"
        "      AND NEW.version_note IS OLD.version_note",
    )
    if replacement_sql == original_sql:
        raise RuntimeError("unable to extend review-version freeze trigger for editor snapshots")

    def replacement() -> str:
        return replacement_sql

    previous._sqlite_derivative_exception = replacement  # type: ignore[attr-defined]
    try:
        previous._install_trigger_policy(allow_replacement=True)  # type: ignore[attr-defined]
    finally:
        previous._sqlite_derivative_exception = original  # type: ignore[attr-defined]


def _restore_previous_sqlite_review_version_freeze() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _previous()._install_trigger_policy(allow_replacement=True)  # type: ignore[attr-defined]


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.add_column("project_refs", sa.Column("fps_num", sa.Integer(), nullable=True))
    op.add_column("project_refs", sa.Column("fps_den", sa.Integer(), nullable=True))
    if dialect != "sqlite":
        op.create_check_constraint(
            "ck_project_refs_fps_pair",
            "project_refs",
            "(fps_num IS NULL AND fps_den IS NULL) "
            "OR (fps_num IS NOT NULL AND fps_den IS NOT NULL AND fps_num > 0 AND fps_den > 0)",
        )

    op.create_table(
        "responsible_editors",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(display_name)) BETWEEN 1 AND 255",
            name="ck_responsible_editors_display_name",
        ),
        sa.CheckConstraint("lock_version >= 1", name="ck_responsible_editors_lock_version"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_responsible_editors_display_name",
        "responsible_editors",
        ["display_name"],
        unique=False,
    )

    op.add_column(
        "review_versions",
        sa.Column("responsible_editor_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "review_versions",
        sa.Column("responsible_editor_name_snapshot", sa.String(length=255), nullable=True),
    )
    if dialect != "sqlite":
        op.create_check_constraint(
            "ck_review_versions_responsible_editor_snapshot_pair",
            "review_versions",
            "(responsible_editor_id IS NULL AND responsible_editor_name_snapshot IS NULL) "
            "OR (responsible_editor_id IS NOT NULL AND responsible_editor_name_snapshot IS NOT NULL "
            "AND length(trim(responsible_editor_name_snapshot)) BETWEEN 1 AND 255)",
        )
    op.create_index(
        "ix_review_versions_responsible_editor",
        "review_versions",
        ["responsible_editor_id"],
        unique=False,
    )
    _create_sqlite_contract_checks()
    _install_sqlite_review_version_freeze()

    op.create_table(
        "review_deletion_intents",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("command_type", sa.String(length=64), nullable=False),
        sa.Column("operation_identity_hash", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("project_ref_id_snapshot", sa.String(length=64), nullable=False),
        sa.Column("review_item_id_snapshot", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "command_type in ('DeleteProject','DeleteReviewItem','DeleteReviewItemsBatch')",
            name="ck_review_deletion_intents_command_type",
        ),
        sa.CheckConstraint(
            "subject_type in ('project','review_item')",
            name="ck_review_deletion_intents_subject_type",
        ),
        sa.CheckConstraint(
            "(command_type = 'DeleteProject' AND subject_type = 'project') "
            "OR (command_type IN ('DeleteReviewItem','DeleteReviewItemsBatch') "
            "AND subject_type = 'review_item')",
            name="ck_review_deletion_intents_command_subject",
        ),
        sa.CheckConstraint(
            "length(trim(project_ref_id_snapshot)) BETWEEN 1 AND 64",
            name="ck_review_deletion_intents_project_snapshot",
        ),
        sa.CheckConstraint(
            "(subject_type = 'project' AND review_item_id_snapshot IS NULL) "
            "OR (subject_type = 'review_item' AND review_item_id_snapshot IS NOT NULL "
            "AND length(trim(review_item_id_snapshot)) BETWEEN 1 AND 64)",
            name="ck_review_deletion_intents_subject_snapshot_shape",
        ),
        sa.CheckConstraint(
            "status in ('pending','cleaning','failed','complete')",
            name="ck_review_deletion_intents_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_review_deletion_intents_attempts"),
        sa.CheckConstraint(
            "length(operation_identity_hash) = 64",
            name="ck_review_deletion_intents_identity_hash",
        ),
        sa.CheckConstraint(
            "(status = 'complete' AND completed_at IS NOT NULL) "
            "OR (status != 'complete' AND completed_at IS NULL)",
            name="ck_review_deletion_intents_completed_at",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_identity_hash",
            name="uq_review_deletion_intents_operation_identity",
        ),
    )
    op.create_index(
        "ix_review_deletion_intents_cleanup",
        "review_deletion_intents",
        ["status", "updated_at", "id"],
        unique=False,
    )

    op.create_table(
        "review_deletion_artifacts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("deletion_intent_id", sa.String(length=64), nullable=False),
        sa.Column("artifact_kind", sa.String(length=32), nullable=False),
        sa.Column("relative_logical_target", sa.String(length=1024), nullable=False),
        sa.Column("expected_device", sa.BigInteger(), nullable=False),
        sa.Column("expected_inode", sa.BigInteger(), nullable=False),
        sa.Column("expected_ctime_ns", sa.BigInteger(), nullable=False),
        sa.Column("expected_size", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "artifact_kind in ('original_media','playback_derivative','thumbnail','project_package')",
            name="ck_review_deletion_artifacts_kind",
        ),
        sa.CheckConstraint(
            "status in ('pending','quarantined','failed','deleted')",
            name="ck_review_deletion_artifacts_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_review_deletion_artifacts_attempts"),
        sa.CheckConstraint(
            "length(relative_logical_target) > 0 "
            "AND relative_logical_target = trim(relative_logical_target) "
            "AND relative_logical_target LIKE '%/%' "
            "AND substr(relative_logical_target, 1, 1) != '/' "
            "AND relative_logical_target NOT LIKE '%/' "
            "AND relative_logical_target NOT LIKE '%//%' "
            "AND relative_logical_target != '..' "
            "AND relative_logical_target != '.' "
            "AND relative_logical_target NOT LIKE './%' "
            "AND relative_logical_target NOT LIKE '../%' "
            "AND relative_logical_target NOT LIKE '%/./%' "
            "AND relative_logical_target NOT LIKE '%/../%' "
            "AND relative_logical_target NOT LIKE '%/.' "
            "AND relative_logical_target NOT LIKE '%/..' "
            "AND relative_logical_target NOT LIKE '%:%' "
            "AND replace(relative_logical_target, '\\', '') = relative_logical_target",
            name="ck_review_deletion_artifacts_relative_target",
        ),
        sa.CheckConstraint(
            "expected_device >= 0 AND expected_inode >= 0 "
            "AND expected_ctime_ns >= 0 AND expected_size > 0",
            name="ck_review_deletion_artifacts_strong_identity",
        ),
        sa.ForeignKeyConstraint(
            ["deletion_intent_id"],
            ["review_deletion_intents.id"],
            name="fk_review_deletion_artifacts_intent",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deletion_intent_id",
            "relative_logical_target",
            name="uq_review_deletion_artifacts_intent_target",
        ),
    )
    op.create_index(
        "ix_review_deletion_artifacts_cleanup",
        "review_deletion_artifacts",
        ["status", "updated_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_review_deletion_artifacts_intent",
        "review_deletion_artifacts",
        ["deletion_intent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_review_deletion_artifacts_intent", table_name="review_deletion_artifacts")
    op.drop_index("ix_review_deletion_artifacts_cleanup", table_name="review_deletion_artifacts")
    op.drop_table("review_deletion_artifacts")
    op.drop_index("ix_review_deletion_intents_cleanup", table_name="review_deletion_intents")
    op.drop_table("review_deletion_intents")

    op.drop_index("ix_review_versions_responsible_editor", table_name="review_versions")
    dialect = op.get_bind().dialect.name
    _drop_sqlite_contract_checks()
    _restore_previous_sqlite_review_version_freeze()
    if dialect != "sqlite":
        op.drop_constraint(
            "ck_review_versions_responsible_editor_snapshot_pair",
            "review_versions",
            type_="check",
        )
    op.drop_column("review_versions", "responsible_editor_name_snapshot")
    op.drop_column("review_versions", "responsible_editor_id")

    op.drop_index("ix_responsible_editors_display_name", table_name="responsible_editors")
    op.drop_table("responsible_editors")

    if dialect != "sqlite":
        op.drop_constraint("ck_project_refs_fps_pair", "project_refs", type_="check")
    op.drop_column("project_refs", "fps_den")
    op.drop_column("project_refs", "fps_num")
