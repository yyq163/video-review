"""forward-align main01 schema to explicit metadata

Revision ID: 20260621_0004
Revises: 20260621_0003
Create Date: 2026-06-21
"""
from __future__ import annotations

from typing import Any, Literal

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.sql.type_api import TypeEngine

revision = "20260621_0004"
down_revision = "20260621_0003"
branch_labels = None
depends_on = None

LEGACY_PRINCIPAL_ID = "migration-legacy-principal"
LEGACY_PRINCIPAL_KIND = "system"
LEGACY_PRINCIPAL_HASH = "0" * 64


def _dialect() -> str:
    return op.get_bind().dialect.name


def _now() -> sa.TextClause:
    return sa.text("CURRENT_TIMESTAMP")


def _table_names() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in inspect(op.get_bind()).get_indexes(table) if index["name"] is not None}


def _unique_names(table: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in inspect(op.get_bind()).get_unique_constraints(table)
        if constraint["name"] is not None
    }


def _check_names(table: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in inspect(op.get_bind()).get_check_constraints(table)
        if constraint["name"] is not None
    }


def _fk_matches(table: str, name: str, columns: list[str], referred_table: str) -> bool:
    for fk in inspect(op.get_bind()).get_foreign_keys(table):
        if fk.get("name") == name:
            return True
        if fk.get("constrained_columns") == columns and fk.get("referred_table") == referred_table:
            return True
    return False


def _batch_recreate() -> Literal["auto", "always", "never"]:
    return "always" if _dialect() == "sqlite" else "auto"


def _add_column(table: str, column: sa.Column) -> None:
    if column.name in _columns(table):
        return
    with op.batch_alter_table(table, recreate=_batch_recreate()) as batch_op:
        batch_op.add_column(column)


def _alter_not_nullable(table: str, column: str, existing_type: TypeEngine[Any]) -> None:
    nullable = next(item["nullable"] for item in inspect(op.get_bind()).get_columns(table) if item["name"] == column)
    if nullable is False:
        return
    with op.batch_alter_table(table, recreate=_batch_recreate()) as batch_op:
        batch_op.alter_column(column, existing_type=existing_type, nullable=False)


def _create_index(name: str, table: str, columns: list[str]) -> None:
    if name in _indexes(table):
        return
    op.create_index(name, table, columns)


def _create_unique(name: str, table: str, columns: list[str]) -> None:
    if name in _unique_names(table):
        return
    with op.batch_alter_table(table, recreate=_batch_recreate()) as batch_op:
        batch_op.create_unique_constraint(name, columns)


def _create_check(name: str, table: str, condition: str) -> None:
    if name in _check_names(table):
        return
    with op.batch_alter_table(table, recreate=_batch_recreate()) as batch_op:
        batch_op.create_check_constraint(name, condition)


def _create_fk(
    name: str,
    table: str,
    referent_table: str,
    local_cols: list[str],
    remote_cols: list[str],
    *,
    ondelete: str = "RESTRICT",
    deferrable: bool | None = None,
    initially: str | None = None,
) -> None:
    if _fk_matches(table, name, local_cols, referent_table):
        return
    with op.batch_alter_table(table, recreate=_batch_recreate()) as batch_op:
        batch_op.create_foreign_key(
            name,
            referent_table,
            local_cols,
            remote_cols,
            ondelete=ondelete,
            deferrable=deferrable,
            initially=initially,
        )


def _backfill_owner_columns() -> None:
    for table in ("file_objects", "upload_sessions"):
        if {"owner_principal_id", "owner_principal_kind"}.issubset(_columns(table)):
            op.execute(
                sa.text(
                    f"""
                    UPDATE {table}
                    SET owner_principal_id = COALESCE(owner_principal_id, :principal_id),
                        owner_principal_kind = COALESCE(owner_principal_kind, :principal_kind)
                    """
                ).bindparams(principal_id=LEGACY_PRINCIPAL_ID, principal_kind=LEGACY_PRINCIPAL_KIND)
            )


def _drop_owner_server_defaults() -> None:
    for table in ("file_objects", "upload_sessions"):
        if not {"owner_principal_id", "owner_principal_kind"}.issubset(_columns(table)):
            continue
        with op.batch_alter_table(table, recreate=_batch_recreate()) as batch_op:
            batch_op.alter_column("owner_principal_id", existing_type=sa.String(length=128), server_default=None)
            batch_op.alter_column("owner_principal_kind", existing_type=sa.String(length=32), server_default=None)


def _drop_principal_hash_server_default() -> None:
    if "idempotency_records" not in _table_names() or "principal_hash" not in _columns("idempotency_records"):
        return
    with op.batch_alter_table("idempotency_records", recreate=_batch_recreate()) as batch_op:
        batch_op.alter_column("principal_hash", existing_type=sa.String(length=64), server_default=None)


def _restore_principal_hash_server_default() -> None:
    if "idempotency_records" not in _table_names() or "principal_hash" not in _columns("idempotency_records"):
        return
    with op.batch_alter_table("idempotency_records", recreate=_batch_recreate()) as batch_op:
        batch_op.alter_column("principal_hash", existing_type=sa.String(length=64), server_default=LEGACY_PRINCIPAL_HASH)


def _backfill_current_revision_ids() -> None:
    if "review_issues" not in _table_names() or "issue_revisions" not in _table_names():
        return
    bind = op.get_bind()
    missing = bind.execute(
        sa.text(
            """
            SELECT i.id, i.project_ref_id, i.review_item_id, i.version_id, r.id AS revision_id
            FROM review_issues i
            LEFT JOIN issue_revisions r
              ON r.issue_id = i.id
             AND r.project_ref_id = i.project_ref_id
             AND r.review_item_id = i.review_item_id
             AND r.version_id = i.version_id
            WHERE i.current_revision_id IS NULL
            ORDER BY i.id, r.revision_no DESC
            """
        )
    ).mappings().all()
    seen: set[str] = set()
    for row in missing:
        issue_id = str(row["id"])
        if issue_id in seen or row["revision_id"] is None:
            continue
        seen.add(issue_id)
        bind.execute(
            sa.text("UPDATE review_issues SET current_revision_id = :revision_id WHERE id = :issue_id").bindparams(
                revision_id=row["revision_id"],
                issue_id=issue_id,
            )
        )
    unresolved = bind.execute(sa.text("SELECT COUNT(*) FROM review_issues WHERE current_revision_id IS NULL")).scalar_one()
    if unresolved:
        raise RuntimeError("Cannot make review_issues.current_revision_id non-null: issues without revisions remain")


def _add_columns() -> None:
    _add_column(
        "file_objects",
        sa.Column("owner_principal_id", sa.String(length=128), nullable=False, server_default=LEGACY_PRINCIPAL_ID),
    )
    _add_column(
        "file_objects",
        sa.Column("owner_principal_kind", sa.String(length=32), nullable=False, server_default=LEGACY_PRINCIPAL_KIND),
    )
    _add_column(
        "upload_sessions",
        sa.Column("owner_principal_id", sa.String(length=128), nullable=False, server_default=LEGACY_PRINCIPAL_ID),
    )
    _add_column(
        "upload_sessions",
        sa.Column("owner_principal_kind", sa.String(length=32), nullable=False, server_default=LEGACY_PRINCIPAL_KIND),
    )
    _add_column("package_snapshots", sa.Column("total_bytes", sa.BigInteger(), nullable=False, server_default="0"))
    _add_column("package_snapshots", sa.Column("failure_details", sa.JSON(), nullable=True))
    _add_column("package_snapshots", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()))
    _add_column("review_items", sa.Column("current_version_is_current", sa.Boolean(), nullable=False, server_default=sa.true()))
    _backfill_owner_columns()
    _drop_owner_server_defaults()
    _drop_principal_hash_server_default()
    _backfill_current_revision_ids()
    _alter_not_nullable("review_issues", "current_revision_id", sa.String(length=64))


def _add_checks() -> None:
    checks = {
        "project_refs": [
            ("ck_project_refs_source", "source in ('local','host')"),
            ("ck_project_refs_lifecycle_status", "lifecycle_status in ('active','archived')"),
            ("ck_project_refs_lock_version", "lock_version >= 1"),
        ],
        "file_objects": [
            ("ck_file_objects_file_size", "file_size > 0"),
            ("ck_file_objects_duration_ms", "duration_ms > 0"),
            ("ck_file_objects_dimensions", "width > 0 and height > 0"),
            ("ck_file_objects_fps", "fps_num > 0 and fps_den > 0"),
            ("ck_file_objects_sha256_len", "length(sha256) = 64"),
        ],
        "upload_sessions": [
            ("ck_upload_sessions_declared_size", "declared_size > 0"),
            ("ck_upload_sessions_status", "status in ('initiated','receiving','completed','aborted')"),
            ("ck_upload_sessions_sha256_len", "length(expected_sha256) = 64"),
        ],
        "review_items": [
            ("ck_review_items_workflow_status", "workflow_status in ('pending_review','in_review','changes_requested','finalized')"),
            (
                "ck_review_items_finalized_has_active_finalization",
                "workflow_status != 'finalized' OR (active_finalization_id IS NOT NULL AND current_version_id IS NOT NULL)",
            ),
            (
                "ck_review_items_current_version_true",
                "current_version_id IS NULL OR current_version_is_current = 1"
                if _dialect() == "sqlite"
                else "current_version_id IS NULL OR current_version_is_current IS TRUE",
            ),
            ("ck_review_items_lock_version", "lock_version >= 1"),
        ],
        "review_versions": [
            ("ck_review_versions_version_no", "version_no >= 1"),
            ("ck_review_versions_file_size", "file_size > 0"),
            ("ck_review_versions_duration_ms", "duration_ms > 0"),
            ("ck_review_versions_dimensions", "width > 0 and height > 0"),
            ("ck_review_versions_fps", "fps_num > 0 and fps_den > 0"),
            ("ck_review_versions_lock_version", "lock_version >= 1"),
        ],
        "review_issues": [
            ("ck_review_issues_status", "status in ('unresolved','resolved')"),
            ("ck_review_issues_time", "timestamp_ms >= 0 and frame_number >= 0"),
            ("ck_review_issues_lock_version", "lock_version >= 1"),
        ],
        "annotation_sets": [
            ("ck_annotation_sets_time", "timestamp_ms >= 0 and frame_number >= 0"),
            ("ck_annotation_sets_dimensions", "canvas_width > 0 and canvas_height > 0 and video_width > 0 and video_height > 0"),
        ],
        "issue_revisions": [("ck_issue_revisions_revision_no", "revision_no >= 1")],
        "review_decisions": [("ck_review_decisions_decision_type", "decision_type in ('changes_requested','finalized')")],
        "finalizations": [
            ("ck_finalizations_version_no", "version_no >= 1"),
            ("ck_finalizations_file_size", "file_size > 0"),
        ],
        "package_snapshots": [("ck_package_snapshots_status", "status in ('preparing','ready','failed','expired')")],
        "outbox_events": [
            ("ck_outbox_events_event_version", "event_version >= 1"),
            ("ck_outbox_events_aggregate_version", "aggregate_version >= 1"),
            ("ck_outbox_events_sequence", "sequence >= 1"),
            ("ck_outbox_events_status", "status in ('pending','publishing','failed','dispatched')"),
            ("ck_outbox_events_attempts", "attempts >= 0"),
        ],
        "idempotency_records": [
            ("ck_idempotency_records_hash_len", "length(request_hash) = 64"),
            ("ck_idempotency_records_status_code", "status_code in (102, 200)"),
        ],
        "operation_logs": [
            ("ck_operation_logs_entry_source", "entry_source in ('edit','review','embedded','unspecified')"),
            ("ck_operation_logs_result", "result in ('ok','error')"),
        ],
    }
    for table, table_checks in checks.items():
        for name, condition in table_checks:
            _create_check(name, table, condition)


def _add_uniques() -> None:
    _create_unique("uq_review_versions_identity_current", "review_versions", ["id", "project_ref_id", "review_item_id", "is_current"])
    _create_unique(
        "uq_issue_revisions_current_identity",
        "issue_revisions",
        ["id", "issue_id", "project_ref_id", "review_item_id", "version_id"],
    )
    _create_unique(
        "uq_issue_revisions_identity",
        "issue_revisions",
        ["id", "project_ref_id", "review_item_id", "version_id", "issue_id"],
    )
    _create_unique(
        "uq_finalizations_current_version_identity",
        "finalizations",
        ["id", "project_ref_id", "review_item_id", "version_id"],
    )
    _create_unique("uq_package_snapshots_id_project", "package_snapshots", ["id", "project_ref_id"])
    _create_unique("uq_outbox_events_sequence", "outbox_events", ["sequence"])


def _add_indexes() -> None:
    _create_index("ix_file_objects_owner", "file_objects", ["owner_principal_id"])
    _create_index("ix_file_objects_sha256", "file_objects", ["sha256"])
    _create_index("ix_upload_sessions_owner", "upload_sessions", ["owner_principal_id"])
    _create_index("ix_upload_sessions_status", "upload_sessions", ["status"])
    _create_index("ix_project_refs_lifecycle_status", "project_refs", ["lifecycle_status"])
    _create_index("ix_review_items_project_ref_id", "review_items", ["project_ref_id"])
    _create_index("ix_review_versions_project_item", "review_versions", ["project_ref_id", "review_item_id"])
    _create_index("ix_review_issues_version", "review_issues", ["project_ref_id", "review_item_id", "version_id"])
    _create_index("ix_issue_revisions_issue", "issue_revisions", ["issue_id"])
    _create_index("ix_thread_messages_issue", "thread_messages", ["project_ref_id", "review_item_id", "version_id", "issue_id"])
    _create_index("ix_review_decisions_version", "review_decisions", ["project_ref_id", "review_item_id", "version_id"])
    _create_index("ix_finalizations_project_version", "finalizations", ["project_ref_id", "review_item_id", "version_id"])
    _create_index("ix_package_snapshots_project", "package_snapshots", ["project_ref_id"])
    _create_index("ix_outbox_events_status_id", "outbox_events", ["status", "id"])
    _create_index("ix_idempotency_records_command_type", "idempotency_records", ["command_type"])
    _create_index("ix_operation_logs_request_id", "operation_logs", ["request_id"])


def _add_fks() -> None:
    _create_fk(
        "fk_review_items_current_version_identity",
        "review_items",
        "review_versions",
        ["current_version_id", "project_ref_id", "id", "current_version_is_current"],
        ["id", "project_ref_id", "review_item_id", "is_current"],
    )
    _create_fk(
        "fk_review_items_active_finalization_current_version",
        "review_items",
        "finalizations",
        ["active_finalization_id", "project_ref_id", "id", "current_version_id"],
        ["id", "project_ref_id", "review_item_id", "version_id"],
    )
    _create_fk(
        "fk_review_issues_current_revision_identity",
        "review_issues",
        "issue_revisions",
        ["current_revision_id", "id", "project_ref_id", "review_item_id", "version_id"],
        ["id", "issue_id", "project_ref_id", "review_item_id", "version_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    _create_fk(
        "fk_review_decisions_version_identity",
        "review_decisions",
        "review_versions",
        ["version_id", "project_ref_id", "review_item_id"],
        ["id", "project_ref_id", "review_item_id"],
    )
    _create_fk(
        "fk_outbox_consumer_receipts_event_id",
        "outbox_consumer_receipts",
        "outbox_events",
        ["event_id"],
        ["event_id"],
    )
    for name, local_cols, remote_table, remote_cols in (
        ("fk_outbox_events_project_ref_id_project_refs", ["project_ref_id"], "project_refs", ["id"]),
        ("fk_outbox_events_review_item_identity", ["review_item_id", "project_ref_id"], "review_items", ["id", "project_ref_id"]),
        (
            "fk_outbox_events_version_identity",
            ["version_id", "project_ref_id", "review_item_id"],
            "review_versions",
            ["id", "project_ref_id", "review_item_id"],
        ),
        (
            "fk_outbox_events_issue_identity",
            ["issue_id", "project_ref_id", "review_item_id", "version_id"],
            "review_issues",
            ["id", "project_ref_id", "review_item_id", "version_id"],
        ),
        (
            "fk_outbox_events_finalization_identity",
            ["finalization_id", "project_ref_id", "review_item_id"],
            "finalizations",
            ["id", "project_ref_id", "review_item_id"],
        ),
        ("fk_outbox_events_package_identity", ["package_id", "project_ref_id"], "package_snapshots", ["id", "project_ref_id"]),
    ):
        _create_fk(name, "outbox_events", remote_table, local_cols, remote_cols)


def _create_governance_triggers() -> None:
    if _dialect() == "sqlite":
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_issue_revisions_no_update
            BEFORE UPDATE ON issue_revisions
            BEGIN
              SELECT RAISE(ABORT, 'issue_revisions are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_issue_revisions_no_delete
            BEFORE DELETE ON issue_revisions
            BEGIN
              SELECT RAISE(ABORT, 'issue_revisions are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_annotation_sets_no_update
            BEFORE UPDATE ON annotation_sets
            BEGIN
              SELECT RAISE(ABORT, 'annotation_sets are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_annotation_sets_no_delete
            BEFORE DELETE ON annotation_sets
            BEGIN
              SELECT RAISE(ABORT, 'annotation_sets are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_issue_revisions_annotation_identity
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
            CREATE TRIGGER IF NOT EXISTS trg_review_issues_current_revision_identity
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
            CREATE TRIGGER IF NOT EXISTS trg_review_items_current_version_identity
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
            CREATE TRIGGER IF NOT EXISTS trg_review_items_finalization_identity
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
            CREATE TRIGGER IF NOT EXISTS trg_review_items_freeze_after_finalized
            BEFORE UPDATE ON review_items
            WHEN OLD.workflow_status = 'finalized'
            BEGIN
              SELECT RAISE(ABORT, 'finalized review item is frozen');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_review_items_no_delete_after_finalized
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
                    CREATE TRIGGER IF NOT EXISTS trg_{table}_freeze_after_finalized_{event.lower()}
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
                CREATE TRIGGER IF NOT EXISTS trg_{table}_freeze_after_finalized_delete
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
    if _dialect() != "postgresql":
        return
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
    for trigger, table, function in (
        ("trg_issue_revisions_no_update", "issue_revisions", "fcr_raise('issue_revisions are immutable')"),
        ("trg_issue_revisions_no_delete", "issue_revisions", "fcr_raise('issue_revisions are immutable')"),
        ("trg_annotation_sets_no_update", "annotation_sets", "fcr_raise('annotation_sets are immutable')"),
        ("trg_annotation_sets_no_delete", "annotation_sets", "fcr_raise('annotation_sets are immutable')"),
        ("trg_review_issues_current_revision_identity", "review_issues", "fcr_check_review_issue_current_revision()"),
        ("trg_review_items_refs_and_freeze", "review_items", "fcr_check_review_item_refs_and_freeze()"),
        ("trg_review_items_no_delete_after_finalized", "review_items", "fcr_raise('finalized review item is frozen')"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        if trigger == "trg_review_items_no_delete_after_finalized":
            op.execute(f"CREATE TRIGGER {trigger} BEFORE DELETE ON {table} FOR EACH ROW WHEN (OLD.workflow_status = 'finalized') EXECUTE FUNCTION {function}")
        else:
            event = "UPDATE" if trigger.endswith("_update") or trigger.endswith("_freeze") else "DELETE"
            if trigger == "trg_review_issues_current_revision_identity":
                op.execute(f"CREATE TRIGGER {trigger} BEFORE UPDATE OF current_revision_id ON {table} FOR EACH ROW EXECUTE FUNCTION {function}")
            elif trigger == "trg_review_items_refs_and_freeze":
                op.execute(f"CREATE TRIGGER {trigger} BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION {function}")
            else:
                op.execute(f"CREATE TRIGGER {trigger} BEFORE {event} ON {table} FOR EACH ROW EXECUTE FUNCTION {function}")
    for table in ("issue_revisions", "review_versions", "review_issues", "thread_messages", "review_decisions", "annotation_sets", "finalizations"):
        for event in ("insert", "update", "delete"):
            trigger = f"trg_{table}_identity_freeze_{event}"
            op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
            op.execute(f"CREATE TRIGGER {trigger} BEFORE {event.upper()} ON {table} FOR EACH ROW EXECUTE FUNCTION fcr_check_identity_and_freeze()")


def upgrade() -> None:
    _add_columns()
    _add_checks()
    _add_uniques()
    _add_indexes()
    _add_fks()
    _create_governance_triggers()


def downgrade() -> None:
    _restore_principal_hash_server_default()
