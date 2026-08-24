"""harden durable deletion claims, artifacts, and immutable child removal

Revision ID: 20260824_0024
Revises: 20260824_0023
Create Date: 2026-08-24
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import re
from typing import Any

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
SQLITE_RECOVERY_MANIFEST_TABLE = "fcr_0024_recovery_manifest"
SQLITE_RECOVERY_MANIFEST_VERSION = 1
SQLITE_RECOVERY_STAGES = {
    "artifact_detach_intent_fk": "review_deletion_artifacts",
    "intent_drop_claim_check": "review_deletion_intents",
    "artifact_normalize": "review_deletion_artifacts",
    "issue_revisions_cascade": "issue_revisions",
    "annotation_sets_cascade": "annotation_sets",
}


def _recreate_mode() -> str:
    return "always" if op.get_bind().dialect.name == "sqlite" else "auto"


def _normalized_sql(value: object) -> str:
    return " ".join(str(value or "").lower().split())


def _sqlite_schema_inventory(table_name: str) -> dict[str, Any]:
    inspector = sa.inspect(op.get_bind())
    return {
        "columns": [
            {
                "name": column["name"],
                "type": str(column["type"]).lower(),
                "nullable": bool(column["nullable"]),
                "default": _normalized_sql(column.get("default")) or None,
                "primary_key": int(column.get("primary_key") or 0),
            }
            for column in inspector.get_columns(table_name)
        ],
        "primary_key": {
            "name": inspector.get_pk_constraint(table_name).get("name"),
            "columns": list(
                inspector.get_pk_constraint(table_name).get(
                    "constrained_columns",
                )
                or []
            ),
        },
        "unique_constraints": sorted(
            (
                {
                    "name": constraint.get("name"),
                    "columns": list(constraint.get("column_names") or []),
                }
                for constraint in inspector.get_unique_constraints(table_name)
            ),
            key=lambda value: (str(value["name"]), value["columns"]),
        ),
        "checks": sorted(
            (
                {
                    "name": constraint.get("name"),
                    "sql": _normalized_sql(constraint.get("sqltext")),
                }
                for constraint in inspector.get_check_constraints(table_name)
            ),
            key=lambda value: (str(value["name"]), value["sql"]),
        ),
        "foreign_keys": sorted(
            (
                {
                    "name": foreign_key.get("name"),
                    "columns": list(
                        foreign_key.get("constrained_columns") or [],
                    ),
                    "referred_table": foreign_key.get("referred_table"),
                    "referred_columns": list(
                        foreign_key.get("referred_columns") or [],
                    ),
                    "ondelete": str(
                        (foreign_key.get("options") or {}).get(
                            "ondelete",
                            "",
                        ),
                    ).upper(),
                }
                for foreign_key in inspector.get_foreign_keys(table_name)
            ),
            key=lambda value: (str(value["name"]), value["columns"]),
        ),
        "indexes": sorted(
            (
                {
                    "name": index.get("name"),
                    "columns": list(index.get("column_names") or []),
                    "unique": bool(index.get("unique")),
                    "where": _normalized_sql(
                        (index.get("dialect_options") or {}).get(
                            "sqlite_where",
                        ),
                    )
                    or None,
                }
                for index in inspector.get_indexes(table_name)
            ),
            key=lambda value: (str(value["name"]), value["columns"]),
        ),
    }


def _inventory_sha256(inventory: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            inventory,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode(),
    ).hexdigest()


def _replace_named_inventory_entry(
    entries: list[dict[str, Any]],
    name: str,
    replacement: dict[str, Any] | None,
) -> None:
    entries[:] = [entry for entry in entries if entry.get("name") != name]
    if replacement is not None:
        entries.append(replacement)
    entries.sort(key=lambda value: (str(value["name"]), value.get("columns", [])))


def _sqlite_target_inventory(
    source: dict[str, Any],
    stage: str,
) -> dict[str, Any]:
    target = copy.deepcopy(source)
    if stage == "artifact_detach_intent_fk":
        _replace_named_inventory_entry(
            target["foreign_keys"],
            ARTIFACT_INTENT_FOREIGN_KEY,
            None,
        )
    elif stage == "intent_drop_claim_check":
        _replace_named_inventory_entry(
            target["checks"],
            INTENT_CLAIM_CHECK,
            None,
        )
    elif stage == "artifact_normalize":
        expected_sha_column = next(
            (
                column
                for column in target["columns"]
                if column["name"] == "expected_sha256"
            ),
            None,
        )
        if expected_sha_column is None:
            target["columns"].append(
                {
                    "name": "expected_sha256",
                    "type": "varchar(64)",
                    "nullable": True,
                    "default": None,
                    "primary_key": 0,
                },
            )
        else:
            expected_sha_column["nullable"] = True
        _replace_named_inventory_entry(
            target["checks"],
            ARTIFACT_SHA_CHECK,
            {
                "name": ARTIFACT_SHA_CHECK,
                "sql": _normalized_sql(
                    "expected_sha256 IS NULL OR length(expected_sha256) = 64",
                ),
            },
        )
        _replace_named_inventory_entry(
            target["checks"],
            ARTIFACT_NAMESPACE_CHECK,
            {
                "name": ARTIFACT_NAMESPACE_CHECK,
                "sql": _normalized_sql(
                    "(artifact_kind = 'project_package' "
                    "AND relative_logical_target LIKE 'packages/%') "
                    "OR (artifact_kind IN "
                    "('original_media','playback_derivative','thumbnail') "
                    "AND relative_logical_target LIKE 'files/%')",
                ),
            },
        )
        _replace_named_inventory_entry(
            target["foreign_keys"],
            ARTIFACT_INTENT_FOREIGN_KEY,
            {
                "name": ARTIFACT_INTENT_FOREIGN_KEY,
                "columns": ["deletion_intent_id"],
                "referred_table": "review_deletion_intents",
                "referred_columns": ["id"],
                "ondelete": "RESTRICT",
            },
        )
    elif stage in {"issue_revisions_cascade", "annotation_sets_cascade"}:
        table_name = SQLITE_RECOVERY_STAGES[stage]
        constraint_name = f"fk_{table_name}_issue_identity"
        _replace_named_inventory_entry(
            target["foreign_keys"],
            constraint_name,
            {
                "name": constraint_name,
                "columns": ISSUE_CHILD_COLUMNS,
                "referred_table": "review_issues",
                "referred_columns": ISSUE_IDENTITY_COLUMNS,
                "ondelete": "CASCADE",
            },
        )
    else:
        raise RuntimeError(f"unknown SQLite deletion recovery stage: {stage}")
    return target


def _canonical_sqlite_value(value: object) -> bytes:
    if value is None:
        return b"n"
    if isinstance(value, bytes):
        payload = value
        tag = b"b"
    elif isinstance(value, str):
        payload = value.encode()
        tag = b"s"
    elif isinstance(value, bool):
        payload = b"1" if value else b"0"
        tag = b"q"
    elif isinstance(value, int):
        payload = str(value).encode()
        tag = b"i"
    elif isinstance(value, float):
        payload = (
            b"nan"
            if math.isnan(value)
            else (b"+inf" if value == math.inf else b"-inf")
            if math.isinf(value)
            else value.hex().encode()
        )
        tag = b"f"
    elif hasattr(value, "isoformat"):
        payload = str(value.isoformat()).encode()  # type: ignore[union-attr]
        tag = b"d"
    else:
        raise RuntimeError(
            f"unsupported SQLite recovery value type: {type(value).__name__}",
        )
    return tag + len(payload).to_bytes(8, "big") + payload


def _sqlite_rows_identity(
    table_name: str,
    target_columns: list[str] | None = None,
) -> tuple[int, str]:
    source_columns = [
        column["name"]
        for column in _sqlite_schema_inventory(table_name)["columns"]
    ]
    selected_columns = target_columns or source_columns
    missing_columns = set(selected_columns) - set(source_columns)
    if missing_columns - {"expected_sha256"}:
        raise RuntimeError(
            f"SQLite recovery row projection has unknown columns: {missing_columns}",
        )
    projections = [
        f'"{column_name}"'
        if column_name in source_columns
        else f'NULL AS "{column_name}"'
        for column_name in selected_columns
    ]
    if "id" not in source_columns:
        raise RuntimeError(f"SQLite recovery table {table_name} lacks id identity")
    rows = op.get_bind().execute(
        sa.text(
            f'SELECT {", ".join(projections)} FROM "{table_name}" '
            'ORDER BY "id"',
        ),
    )
    digest = hashlib.sha256(b"fcr-0024-sqlite-rows-v1\0")
    for column_name in selected_columns:
        digest.update(_canonical_sqlite_value(column_name))
    row_count = 0
    for row in rows:
        row_count += 1
        digest.update(len(row).to_bytes(4, "big"))
        for value in row:
            digest.update(_canonical_sqlite_value(value))
    return row_count, digest.hexdigest()


def _sqlite_canonical_rows(
    table_name: str,
    target_columns: list[str],
) -> dict[bytes, str]:
    source_columns = [
        column["name"]
        for column in _sqlite_schema_inventory(table_name)["columns"]
    ]
    missing_columns = set(target_columns) - set(source_columns)
    if missing_columns - {"expected_sha256"}:
        raise RuntimeError(
            f"SQLite recovery row projection has unknown columns: {missing_columns}",
        )
    projections = [
        f'"{column_name}"'
        if column_name in source_columns
        else f'NULL AS "{column_name}"'
        for column_name in target_columns
    ]
    rows = op.get_bind().execute(
        sa.text(
            f'SELECT {", ".join(projections)} FROM "{table_name}" '
            'ORDER BY "id"',
        ),
    )
    id_index = target_columns.index("id")
    canonical: dict[bytes, str] = {}
    for row in rows:
        encoded = tuple(_canonical_sqlite_value(value) for value in row)
        identity = encoded[id_index]
        if identity in canonical:
            raise RuntimeError(
                f"SQLite recovery table {table_name} has duplicate identity",
            )
        row_digest = hashlib.sha256(b"fcr-0024-sqlite-row-v1\0")
        for column_name, value in zip(target_columns, encoded, strict=True):
            row_digest.update(_canonical_sqlite_value(column_name))
            row_digest.update(value)
        canonical[identity] = row_digest.hexdigest()
    return canonical


SQLITE_MANIFEST_FIELDS = (
    "manifest_version",
    "revision_id",
    "table_name",
    "stage",
    "source_schema_sha256",
    "staging_schema_sha256",
    "target_schema_sha256",
    "source_row_count",
    "target_row_count",
    "source_rows_sha256",
    "target_rows_sha256",
)


def _sqlite_default_value(value: object) -> str:
    raw = str(value or "").strip()
    while raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


def _load_sqlite_recovery_manifest() -> dict[str, Any] | None:
    inspector = sa.inspect(op.get_bind())
    if SQLITE_RECOVERY_MANIFEST_TABLE not in inspector.get_table_names():
        return None
    columns = inspector.get_columns(SQLITE_RECOVERY_MANIFEST_TABLE)
    if [column["name"] for column in columns] != list(SQLITE_MANIFEST_FIELDS):
        raise RuntimeError("SQLite deletion recovery manifest has an unsafe schema")
    for index, column in enumerate(columns):
        expected_type = "integer" if index in {0, 7, 8} else "text"
        if (
            str(column["type"]).lower() != expected_type
            or bool(column["nullable"])
            or int(column.get("primary_key") or 0) != (1 if index == 0 else 0)
            or column.get("default") is None
        ):
            raise RuntimeError(
                "SQLite deletion recovery manifest has an unsafe column",
            )
    if inspector.get_pk_constraint(SQLITE_RECOVERY_MANIFEST_TABLE).get(
        "constrained_columns",
    ) != ["manifest_version"]:
        raise RuntimeError("SQLite deletion recovery manifest has an unsafe PK")
    if (
        inspector.get_unique_constraints(SQLITE_RECOVERY_MANIFEST_TABLE)
        or inspector.get_check_constraints(SQLITE_RECOVERY_MANIFEST_TABLE)
        or inspector.get_foreign_keys(SQLITE_RECOVERY_MANIFEST_TABLE)
        or inspector.get_indexes(SQLITE_RECOVERY_MANIFEST_TABLE)
    ):
        raise RuntimeError(
            "SQLite deletion recovery manifest has unexpected constraints",
        )
    values = {
        column["name"]: _sqlite_default_value(column["default"])
        for column in columns
    }
    if values["manifest_version"] != str(SQLITE_RECOVERY_MANIFEST_VERSION):
        raise RuntimeError("SQLite deletion recovery manifest is expired")
    if values["revision_id"] != revision:
        raise RuntimeError("SQLite deletion recovery manifest is expired")
    stage = values["stage"]
    if stage not in SQLITE_RECOVERY_STAGES:
        raise RuntimeError("SQLite deletion recovery manifest has unknown stage")
    if values["table_name"] != SQLITE_RECOVERY_STAGES[stage]:
        raise RuntimeError("SQLite deletion recovery manifest table mismatch")
    for field in (
        "source_schema_sha256",
        "staging_schema_sha256",
        "target_schema_sha256",
        "source_rows_sha256",
        "target_rows_sha256",
    ):
        if re.fullmatch(r"[0-9a-f]{64}", values[field]) is None:
            raise RuntimeError("SQLite deletion recovery manifest digest is invalid")
    for field in ("source_row_count", "target_row_count"):
        if not values[field].isdigit():
            raise RuntimeError("SQLite deletion recovery manifest count is invalid")
        values[field] = int(values[field])
    values["manifest_version"] = int(values["manifest_version"])
    return values


def _drop_sqlite_recovery_manifest() -> None:
    op.execute(f'DROP TABLE IF EXISTS "{SQLITE_RECOVERY_MANIFEST_TABLE}"')


def _prepare_sqlite_batch_manifest(stage: str) -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    if stage not in SQLITE_RECOVERY_STAGES:
        raise RuntimeError(f"unknown SQLite deletion recovery stage: {stage}")
    if _load_sqlite_recovery_manifest() is not None:
        raise RuntimeError("SQLite deletion recovery manifest is already active")
    table_name = SQLITE_RECOVERY_STAGES[stage]
    source_inventory = _sqlite_schema_inventory(table_name)
    target_inventory = _sqlite_target_inventory(source_inventory, stage)
    staging_inventory = copy.deepcopy(target_inventory)
    staging_inventory["indexes"] = []
    source_columns = [column["name"] for column in source_inventory["columns"]]
    target_columns = [column["name"] for column in target_inventory["columns"]]
    source_row_count, source_rows_sha256 = _sqlite_rows_identity(
        table_name,
        source_columns,
    )
    target_row_count, target_rows_sha256 = _sqlite_rows_identity(
        table_name,
        target_columns,
    )
    values: dict[str, object] = {
        "manifest_version": SQLITE_RECOVERY_MANIFEST_VERSION,
        "revision_id": revision,
        "table_name": table_name,
        "stage": stage,
        "source_schema_sha256": _inventory_sha256(source_inventory),
        "staging_schema_sha256": _inventory_sha256(staging_inventory),
        "target_schema_sha256": _inventory_sha256(target_inventory),
        "source_row_count": source_row_count,
        "target_row_count": target_row_count,
        "source_rows_sha256": source_rows_sha256,
        "target_rows_sha256": target_rows_sha256,
    }
    if not all(
        re.fullmatch(r"[a-z0-9_]+", str(values[field]))
        for field in ("revision_id", "table_name", "stage")
    ):
        raise RuntimeError("SQLite deletion recovery manifest identity is invalid")
    for field in (
        "source_schema_sha256",
        "staging_schema_sha256",
        "target_schema_sha256",
        "source_rows_sha256",
        "target_rows_sha256",
    ):
        if re.fullmatch(r"[0-9a-f]{64}", str(values[field])) is None:
            raise RuntimeError("SQLite deletion recovery manifest digest is invalid")
    op.execute(
        f"""
        CREATE TABLE "{SQLITE_RECOVERY_MANIFEST_TABLE}" (
          manifest_version INTEGER NOT NULL PRIMARY KEY DEFAULT {values['manifest_version']},
          revision_id TEXT NOT NULL DEFAULT '{values['revision_id']}',
          table_name TEXT NOT NULL DEFAULT '{values['table_name']}',
          stage TEXT NOT NULL DEFAULT '{values['stage']}',
          source_schema_sha256 TEXT NOT NULL DEFAULT '{values['source_schema_sha256']}',
          staging_schema_sha256 TEXT NOT NULL DEFAULT '{values['staging_schema_sha256']}',
          target_schema_sha256 TEXT NOT NULL DEFAULT '{values['target_schema_sha256']}',
          source_row_count INTEGER NOT NULL DEFAULT {values['source_row_count']},
          target_row_count INTEGER NOT NULL DEFAULT {values['target_row_count']},
          source_rows_sha256 TEXT NOT NULL DEFAULT '{values['source_rows_sha256']}',
          target_rows_sha256 TEXT NOT NULL DEFAULT '{values['target_rows_sha256']}'
        )
        """,
    )
    _load_sqlite_recovery_manifest()


def _table_matches_sqlite_manifest(
    table_name: str,
    manifest: dict[str, Any],
    schema_prefix: str,
    row_prefix: str | None = None,
) -> bool:
    resolved_row_prefix = row_prefix or schema_prefix
    schema_sha256 = _inventory_sha256(_sqlite_schema_inventory(table_name))
    columns = [
        column["name"]
        for column in _sqlite_schema_inventory(table_name)["columns"]
    ]
    row_count, rows_sha256 = _sqlite_rows_identity(table_name, columns)
    return (
        schema_sha256 == manifest[f"{schema_prefix}_schema_sha256"]
        and row_count == manifest[f"{resolved_row_prefix}_row_count"]
        and rows_sha256 == manifest[f"{resolved_row_prefix}_rows_sha256"]
    )


def _finish_sqlite_batch_manifest(stage: str) -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    manifest = _load_sqlite_recovery_manifest()
    if manifest is None or manifest["stage"] != stage:
        raise RuntimeError("SQLite deletion recovery manifest is missing")
    if not _table_matches_sqlite_manifest(
        manifest["table_name"],
        manifest,
        "target",
    ):
        raise RuntimeError("SQLite deletion batch target identity mismatch")
    _drop_sqlite_recovery_manifest()


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
        for table_name in SQLITE_RECOVERY_STAGES.values()
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
    manifest = _load_sqlite_recovery_manifest()
    if not found_temporary_tables:
        if manifest is None:
            return
        table_name = manifest["table_name"]
        if table_name not in table_names:
            raise RuntimeError(
                "SQLite deletion recovery manifest source is missing",
            )
        if not (
            _table_matches_sqlite_manifest(table_name, manifest, "source")
            or _table_matches_sqlite_manifest(table_name, manifest, "target")
        ):
            raise RuntimeError("SQLite deletion recovery manifest is expired")
        _drop_sqlite_recovery_manifest()
        return
    if manifest is None:
        raise RuntimeError("SQLite deletion recovery manifest is missing")
    table_name = manifest["table_name"]
    temporary_table = f"_alembic_tmp_{table_name}"
    if found_temporary_tables != {temporary_table}:
        raise RuntimeError(
            "SQLite deletion recovery has multiple or mismatched temporary tables",
        )
    temporary_inventory = _sqlite_schema_inventory(temporary_table)
    if (
        _inventory_sha256(temporary_inventory)
        != manifest["staging_schema_sha256"]
    ):
        raise RuntimeError("SQLite deletion temporary table identity mismatch")
    source_exists = table_name in table_names
    if source_exists:
        source_is_pre_batch = _table_matches_sqlite_manifest(
            table_name,
            manifest,
            "source",
        )
        source_is_post_batch = _table_matches_sqlite_manifest(
            table_name,
            manifest,
            "target",
        )
        if not (source_is_pre_batch or source_is_post_batch):
            raise RuntimeError("SQLite deletion source table identity mismatch")
        source_inventory = _sqlite_schema_inventory(table_name)
        expected_target_inventory = (
            _sqlite_target_inventory(source_inventory, manifest["stage"])
            if source_is_pre_batch
            else source_inventory
        )
        expected_staging_inventory = copy.deepcopy(expected_target_inventory)
        expected_staging_inventory["indexes"] = []
        if (
            _inventory_sha256(expected_target_inventory)
            != manifest["target_schema_sha256"]
            or _inventory_sha256(expected_staging_inventory)
            != manifest["staging_schema_sha256"]
        ):
            raise RuntimeError("SQLite deletion recovery manifest is forged")
        target_columns = [
            column["name"]
            for column in expected_target_inventory["columns"]
        ]
        source_rows = _sqlite_canonical_rows(table_name, target_columns)
        temporary_rows = _sqlite_canonical_rows(
            temporary_table,
            target_columns,
        )
        if any(
            identity not in source_rows or source_rows[identity] != row
            for identity, row in temporary_rows.items()
        ):
            raise RuntimeError(
                "SQLite deletion temporary rows diverge from source",
            )
    elif not _table_matches_sqlite_manifest(
        temporary_table,
        manifest,
        "staging",
        "target",
    ):
        raise RuntimeError("SQLite deletion temp-only identity mismatch")
    _drop_sqlite_rebuild_triggers()
    if source_exists:
        op.execute(f'DROP TABLE "{temporary_table}"')
    else:
        op.execute(
            f'ALTER TABLE "{temporary_table}" RENAME TO "{table_name}"',
        )
    _drop_sqlite_recovery_manifest()
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
        manifest_stage = (
            f"{table_name}_cascade"
            if op.get_bind().dialect.name == "sqlite"
            and ondelete.upper() == "CASCADE"
            else None
        )
        if manifest_stage is not None:
            _prepare_sqlite_batch_manifest(manifest_stage)
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
        if manifest_stage is not None:
            _finish_sqlite_batch_manifest(manifest_stage)


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
    _prepare_sqlite_batch_manifest("artifact_detach_intent_fk")
    with op.batch_alter_table(
        "review_deletion_artifacts",
        recreate="always",
    ) as batch_op:
        if intent_foreign_key is not None:
            batch_op.drop_constraint(
                ARTIFACT_INTENT_FOREIGN_KEY,
                type_="foreignkey",
            )
    _finish_sqlite_batch_manifest("artifact_detach_intent_fk")
    for table_name in ("issue_revisions", "annotation_sets"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_no_delete")
    _prepare_sqlite_batch_manifest("intent_drop_claim_check")
    with op.batch_alter_table(
        "review_deletion_intents",
        recreate="always",
    ) as batch_op:
        batch_op.drop_constraint(INTENT_CLAIM_CHECK, type_="check")
    _finish_sqlite_batch_manifest("intent_drop_claim_check")
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
    _prepare_sqlite_batch_manifest("artifact_normalize")
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
    _finish_sqlite_batch_manifest("artifact_normalize")

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
