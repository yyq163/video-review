"""harden durable deletion claims, artifacts, and immutable child removal

Revision ID: 20260824_0024
Revises: 20260824_0023
Create Date: 2026-08-24
"""

from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic import op


revision = "20260824_0024"
down_revision = "20260824_0023"
branch_labels = None
depends_on = None


ISSUE_CHILD_FOREIGN_KEYS = (
    (
        "issue_revisions",
        "fk_issue_revisions_issue_identity",
    ),
    (
        "annotation_sets",
        "fk_annotation_sets_issue_identity",
    ),
)
ISSUE_CHILD_COLUMNS = ["issue_id", "project_ref_id", "review_item_id", "version_id"]
ISSUE_IDENTITY_COLUMNS = ["id", "project_ref_id", "review_item_id", "version_id"]
INTENT_CLAIM_CHECK = "ck_review_deletion_intents_cleanup_claim"
ARTIFACT_SHA_CHECK = "ck_review_deletion_artifacts_sha256"
ARTIFACT_NAMESPACE_CHECK = "ck_review_deletion_artifacts_kind_namespace"
ARTIFACT_INTENT_FOREIGN_KEY = "fk_review_deletion_artifacts_intent"
SQLITE_BATCH_RECOVERY_COLUMNS = {
    "review_deletion_intents": {
        "id",
        "command_type",
        "operation_identity_hash",
        "subject_type",
        "project_ref_id_snapshot",
        "review_item_id_snapshot",
        "status",
        "attempts",
        "last_error_code",
        "completed_at",
        "created_at",
        "updated_at",
    },
    "review_deletion_artifacts": {
        "id",
        "deletion_intent_id",
        "artifact_kind",
        "relative_logical_target",
        "expected_device",
        "expected_inode",
        "expected_ctime_ns",
        "expected_size",
        "status",
        "attempts",
        "last_error_code",
        "quarantined_at",
        "deleted_at",
        "created_at",
        "updated_at",
    },
    "issue_revisions": {
        "id",
        "issue_id",
        "project_ref_id",
        "review_item_id",
        "version_id",
        "revision_no",
        "content",
        "annotation_set_id",
        "created_at",
    },
    "annotation_sets": {
        "id",
        "issue_id",
        "project_ref_id",
        "review_item_id",
        "version_id",
        "timestamp_ms",
        "frame_number",
        "canvas_width",
        "canvas_height",
        "video_width",
        "video_height",
        "shapes",
        "created_at",
    },
}


def _recreate_mode() -> str:
    return "always" if op.get_bind().dialect.name == "sqlite" else "auto"


def _drop_sqlite_rebuild_triggers() -> None:
    for trigger_name in (
        "trg_issue_revisions_no_delete",
        "trg_annotation_sets_no_delete",
        "trg_review_issues_current_revision_identity",
        "trg_review_deletion_intents_cleanup_claim_insert",
        "trg_review_deletion_intents_cleanup_claim_update",
        "trg_review_deletion_artifacts_sha_insert",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")


def _recover_sqlite_batch_residue() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names())
    known_temporary_tables = {
        f"_alembic_tmp_{table_name}"
        for table_name in SQLITE_BATCH_RECOVERY_COLUMNS
    }
    found_temporary_tables = {
        table_name
        for table_name in table_names
        if table_name.startswith("_alembic_tmp_")
    }
    unknown_temporary_tables = (
        found_temporary_tables - known_temporary_tables
    )
    if unknown_temporary_tables:
        raise RuntimeError(
            "unknown Alembic temporary table blocks deletion migration retry: "
            + ",".join(sorted(unknown_temporary_tables)),
        )
    if not found_temporary_tables:
        return
    recovery_actions: list[tuple[str, str, str]] = []
    for table_name, required_columns in SQLITE_BATCH_RECOVERY_COLUMNS.items():
        temporary_table = f"_alembic_tmp_{table_name}"
        if temporary_table not in found_temporary_tables:
            continue
        temporary_columns = {
            column["name"]
            for column in sa.inspect(bind).get_columns(temporary_table)
        }
        if not required_columns <= temporary_columns:
            raise RuntimeError(
                f"Alembic temporary table {temporary_table} has an unsafe shape",
            )
        if table_name in table_names:
            source_columns = {
                column["name"]
                for column in sa.inspect(bind).get_columns(table_name)
            }
            if not required_columns <= source_columns:
                raise RuntimeError(
                    f"migration source table {table_name} has an unsafe shape",
                )
            temp_only_rows = bind.scalar(
                sa.text(
                    f'SELECT count(*) FROM "{temporary_table}" AS temp '
                    f'LEFT JOIN "{table_name}" AS source ON source.id = temp.id '
                    "WHERE source.id IS NULL",
                ),
            )
            if int(temp_only_rows or 0) != 0:
                raise RuntimeError(
                    f"Alembic temporary table {temporary_table} contains "
                    "rows absent from its source",
                )
            recovery_actions.append(("drop", temporary_table, table_name))
        else:
            recovery_actions.append(("rename", temporary_table, table_name))
    _drop_sqlite_rebuild_triggers()
    for action, temporary_table, table_name in recovery_actions:
        if action == "drop":
            op.execute(f'DROP TABLE "{temporary_table}"')
        else:
            op.execute(
                f'ALTER TABLE "{temporary_table}" RENAME TO "{table_name}"',
            )
    remaining_temporary_tables = {
        table_name
        for table_name in sa.inspect(bind).get_table_names()
        if table_name.startswith("_alembic_tmp_")
    }
    if remaining_temporary_tables:
        raise RuntimeError(
            "Alembic temporary table recovery did not converge: "
            + ",".join(sorted(remaining_temporary_tables)),
        )


def _replace_issue_child_foreign_keys(ondelete: str) -> None:
    for table_name, constraint_name in ISSUE_CHILD_FOREIGN_KEYS:
        foreign_key = next(
            (
                candidate
                for candidate in sa.inspect(op.get_bind()).get_foreign_keys(
                    table_name,
                )
                if candidate.get("name") == constraint_name
            ),
            None,
        )
        current_ondelete = (
            str((foreign_key.get("options") or {}).get("ondelete", "")).upper()
            if foreign_key is not None
            else ""
        )
        if current_ondelete == ondelete.upper():
            continue
        with op.batch_alter_table(
            table_name,
            recreate=_recreate_mode(),
        ) as batch_op:
            if foreign_key is not None:
                batch_op.drop_constraint(constraint_name, type_="foreignkey")
            batch_op.create_foreign_key(
                constraint_name,
                "review_issues",
                ISSUE_CHILD_COLUMNS,
                ISSUE_IDENTITY_COLUMNS,
                ondelete=ondelete,
            )


def _restore_governance_triggers() -> None:
    migration = importlib.import_module(
        "backend.alembic.versions.20260621_0004_main01_forward_schema_alignment",
    )
    migration._create_governance_triggers()  # type: ignore[attr-defined]


def _drop_sqlite_child_rebuild_dependencies() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_review_issues_current_revision_identity")


def _install_parent_bound_immutable_delete_triggers() -> None:
    dialect = op.get_bind().dialect.name
    tables = ("issue_revisions", "annotation_sets")
    if dialect == "sqlite":
        for table_name in tables:
            trigger_name = f"trg_{table_name}_no_delete"
            op.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
            op.execute(
                f"""
                CREATE TRIGGER {trigger_name}
                BEFORE DELETE ON {table_name}
                WHEN EXISTS (
                  SELECT 1
                  FROM review_issues issue
                  WHERE issue.id = OLD.issue_id
                    AND issue.project_ref_id = OLD.project_ref_id
                    AND issue.review_item_id = OLD.review_item_id
                    AND issue.version_id = OLD.version_id
                )
                BEGIN
                  SELECT RAISE(ABORT, '{table_name} are immutable outside parent deletion');
                END
                """,
            )
        return
    if dialect != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fcr_allow_issue_child_parent_delete()
        RETURNS trigger AS $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM review_issues issue
            WHERE issue.id = OLD.issue_id
              AND issue.project_ref_id = OLD.project_ref_id
              AND issue.review_item_id = OLD.review_item_id
              AND issue.version_id = OLD.version_id
          ) THEN
            RAISE EXCEPTION '% are immutable outside parent deletion', TG_TABLE_NAME;
          END IF;
          RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
        """,
    )
    for table_name in tables:
        trigger_name = f"trg_{table_name}_no_delete"
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}")
        op.execute(
            f"CREATE TRIGGER {trigger_name} BEFORE DELETE ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION fcr_allow_issue_child_parent_delete()",
        )
    op.execute("DROP FUNCTION IF EXISTS fcr_allow_durable_business_delete()")


def _restore_unconditional_immutable_delete_triggers() -> None:
    dialect = op.get_bind().dialect.name
    tables = ("issue_revisions", "annotation_sets")
    for table_name in tables:
        trigger_name = f"trg_{table_name}_no_delete"
        if dialect == "sqlite":
            op.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        elif dialect == "postgresql":
            op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}")
    if dialect == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS fcr_allow_issue_child_parent_delete()")
    _restore_governance_triggers()


def _install_artifact_insert_sha_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_review_deletion_artifacts_sha_insert",
        )
        op.execute(
            """
            CREATE TRIGGER trg_review_deletion_artifacts_sha_insert
            BEFORE INSERT ON review_deletion_artifacts
            WHEN NEW.expected_sha256 IS NULL OR length(NEW.expected_sha256) != 64
            BEGIN
              SELECT RAISE(ABORT, 'new deletion artifacts require sha256');
            END
            """,
        )
        return
    if dialect != "postgresql":
        return
    op.execute(
        "DROP TRIGGER IF EXISTS trg_review_deletion_artifacts_sha_insert "
        "ON review_deletion_artifacts",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fcr_require_deletion_artifact_sha_insert()
        RETURNS trigger AS $$
        BEGIN
          IF NEW.expected_sha256 IS NULL OR length(NEW.expected_sha256) != 64 THEN
            RAISE EXCEPTION 'new deletion artifacts require sha256';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
    )
    op.execute(
        """
        CREATE TRIGGER trg_review_deletion_artifacts_sha_insert
        BEFORE INSERT ON review_deletion_artifacts
        FOR EACH ROW EXECUTE FUNCTION fcr_require_deletion_artifact_sha_insert()
        """,
    )


def _drop_artifact_insert_sha_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_review_deletion_artifacts_sha_insert")
    elif dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_review_deletion_artifacts_sha_insert "
            "ON review_deletion_artifacts",
        )
        op.execute("DROP FUNCTION IF EXISTS fcr_require_deletion_artifact_sha_insert()")


def _install_intent_claim_guard() -> None:
    check_expression = (
        "(status = 'cleaning' AND cleanup_claim_id IS NOT NULL "
        "AND cleanup_claim_expires_at IS NOT NULL) "
        "OR (status != 'cleaning' AND cleanup_claim_id IS NULL "
        "AND cleanup_claim_expires_at IS NULL)"
    )
    if op.get_bind().dialect.name == "sqlite":
        trigger_expression = (
            "(NEW.status = 'cleaning' AND NEW.cleanup_claim_id IS NOT NULL "
            "AND NEW.cleanup_claim_expires_at IS NOT NULL) "
            "OR (NEW.status != 'cleaning' AND NEW.cleanup_claim_id IS NULL "
            "AND NEW.cleanup_claim_expires_at IS NULL)"
        )
        for event in ("INSERT", "UPDATE"):
            op.execute(
                "DROP TRIGGER IF EXISTS "
                f"trg_review_deletion_intents_cleanup_claim_{event.lower()}",
            )
            op.execute(
                f"""
                CREATE TRIGGER trg_review_deletion_intents_cleanup_claim_{event.lower()}
                BEFORE {event} ON review_deletion_intents
                WHEN NOT ({trigger_expression})
                BEGIN
                  SELECT RAISE(ABORT, 'deletion intent cleanup claim is invalid');
                END
                """,
            )
        return
    op.create_check_constraint(
        INTENT_CLAIM_CHECK,
        "review_deletion_intents",
        check_expression,
    )


def _drop_intent_claim_guard() -> None:
    if op.get_bind().dialect.name == "sqlite":
        for event in ("insert", "update"):
            op.execute(
                "DROP TRIGGER IF EXISTS "
                f"trg_review_deletion_intents_cleanup_claim_{event}",
            )
        return
    op.drop_constraint(
        "ck_review_deletion_intents_cleanup_claim",
        "review_deletion_intents",
        type_="check",
    )


def _drop_sqlite_legacy_intent_claim_check(
    intent_checks: set[str | None],
) -> bool:
    if (
        op.get_bind().dialect.name != "sqlite"
        or INTENT_CLAIM_CHECK not in intent_checks
    ):
        return False
    artifact_foreign_keys = sa.inspect(op.get_bind()).get_foreign_keys(
        "review_deletion_artifacts",
    )
    intent_foreign_key = next(
        (
            foreign_key
            for foreign_key in artifact_foreign_keys
            if foreign_key.get("name") == ARTIFACT_INTENT_FOREIGN_KEY
        ),
        None,
    )
    with op.batch_alter_table(
        "review_deletion_artifacts",
        recreate="always",
    ) as batch_op:
        if intent_foreign_key is not None:
            batch_op.drop_constraint(
                ARTIFACT_INTENT_FOREIGN_KEY,
                type_="foreignkey",
            )
    for table_name in ("issue_revisions", "annotation_sets"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_no_delete")
    with op.batch_alter_table(
        "review_deletion_intents",
        recreate="always",
    ) as batch_op:
        batch_op.drop_constraint(INTENT_CLAIM_CHECK, type_="check")
    return True


def upgrade() -> None:
    _recover_sqlite_batch_residue()
    intent_columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(
            "review_deletion_intents",
        )
    }
    if "cleanup_claim_id" not in intent_columns:
        op.add_column(
            "review_deletion_intents",
            sa.Column("cleanup_claim_id", sa.String(length=64), nullable=True),
        )
    if "cleanup_claim_expires_at" not in intent_columns:
        op.add_column(
            "review_deletion_intents",
            sa.Column(
                "cleanup_claim_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
    intent_checks = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(
            "review_deletion_intents",
        )
    }
    detached_legacy_artifact_intent_foreign_key = (
        _drop_sqlite_legacy_intent_claim_check(intent_checks)
    )
    if (
        INTENT_CLAIM_CHECK in intent_checks
        and op.get_bind().dialect.name != "sqlite"
    ):
        with op.batch_alter_table(
            "review_deletion_intents",
            recreate=_recreate_mode(),
        ) as batch_op:
            batch_op.drop_constraint(INTENT_CLAIM_CHECK, type_="check")
    _install_intent_claim_guard()

    artifact_inspector = sa.inspect(op.get_bind())
    artifact_columns = {
        column["name"]
        for column in artifact_inspector.get_columns(
            "review_deletion_artifacts",
        )
    }
    artifact_checks = {
        constraint["name"]
        for constraint in artifact_inspector.get_check_constraints(
            "review_deletion_artifacts",
        )
    }
    artifact_intent_foreign_key = next(
        (
            foreign_key
            for foreign_key in artifact_inspector.get_foreign_keys(
                "review_deletion_artifacts",
            )
            if foreign_key.get("name") == ARTIFACT_INTENT_FOREIGN_KEY
        ),
        None,
    )
    artifact_intent_ondelete = (
        str(
            (artifact_intent_foreign_key.get("options") or {}).get(
                "ondelete",
                "",
            ),
        ).upper()
        if artifact_intent_foreign_key is not None
        else ""
    )
    normalize_sqlite_artifact_intent_foreign_key = (
        op.get_bind().dialect.name == "sqlite"
        and (
            detached_legacy_artifact_intent_foreign_key
            or artifact_intent_foreign_key is None
            or artifact_intent_ondelete != "RESTRICT"
        )
    )
    with op.batch_alter_table(
        "review_deletion_artifacts",
        recreate=_recreate_mode(),
    ) as batch_op:
        if "expected_sha256" not in artifact_columns:
            batch_op.add_column(
                sa.Column("expected_sha256", sa.String(length=64), nullable=True),
            )
        else:
            batch_op.alter_column(
                "expected_sha256",
                existing_type=sa.String(length=64),
                nullable=True,
            )
        if ARTIFACT_SHA_CHECK in artifact_checks:
            batch_op.drop_constraint(ARTIFACT_SHA_CHECK, type_="check")
        if ARTIFACT_NAMESPACE_CHECK in artifact_checks:
            batch_op.drop_constraint(ARTIFACT_NAMESPACE_CHECK, type_="check")
        if (
            normalize_sqlite_artifact_intent_foreign_key
            and artifact_intent_foreign_key is not None
        ):
            batch_op.drop_constraint(
                ARTIFACT_INTENT_FOREIGN_KEY,
                type_="foreignkey",
            )
        batch_op.create_check_constraint(
            ARTIFACT_SHA_CHECK,
            "expected_sha256 IS NULL OR length(expected_sha256) = 64",
        )
        batch_op.create_check_constraint(
            ARTIFACT_NAMESPACE_CHECK,
            "(artifact_kind = 'project_package' "
            "AND relative_logical_target LIKE 'packages/%') "
            "OR (artifact_kind IN "
            "('original_media','playback_derivative','thumbnail') "
            "AND relative_logical_target LIKE 'files/%')",
        )
        if normalize_sqlite_artifact_intent_foreign_key:
            batch_op.create_foreign_key(
                ARTIFACT_INTENT_FOREIGN_KEY,
                "review_deletion_intents",
                ["deletion_intent_id"],
                ["id"],
                ondelete="RESTRICT",
            )

    _drop_sqlite_child_rebuild_dependencies()
    _replace_issue_child_foreign_keys("CASCADE")
    _restore_governance_triggers()
    _install_parent_bound_immutable_delete_triggers()
    _install_artifact_insert_sha_guard()


def downgrade() -> None:
    _drop_artifact_insert_sha_guard()
    _restore_unconditional_immutable_delete_triggers()
    _drop_sqlite_child_rebuild_dependencies()
    _replace_issue_child_foreign_keys("RESTRICT")
    _restore_governance_triggers()

    with op.batch_alter_table(
        "review_deletion_artifacts",
        recreate=_recreate_mode(),
    ) as batch_op:
        batch_op.drop_constraint(
            "ck_review_deletion_artifacts_kind_namespace",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_review_deletion_artifacts_sha256",
            type_="check",
        )
        batch_op.drop_column("expected_sha256")

    _drop_intent_claim_guard()
    op.drop_column("review_deletion_intents", "cleanup_claim_expires_at")
    op.drop_column("review_deletion_intents", "cleanup_claim_id")
