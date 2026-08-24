from __future__ import annotations

import hashlib
import fcntl
import os
import re
import threading
from pathlib import Path
from typing import Any

import pytest
from alembic import command as alembic_command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import Column, DateTime, String, create_engine, delete, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
    ReviewAnnotationSetModel,
    ReviewDeletionArtifactModel,
    ReviewDeletionIntentModel,
)

from .conftest import api_data, command, create_project_item
from .test_api_contract_runtime import (
    annotation,
    create_issue,
    finalize,
    prepare_ready_package,
)
from .test_task1_contract_and_migration import (
    _alembic_config,
    _set_migration_env,
)
from .test_task4_durable_deletion import _delete_item


def _normalize_sqlite_sql(value: object) -> str:
    return " ".join(str(value or "").lower().split())


def _sqlite_task4_schema_inventory(engine: Engine) -> dict[str, Any]:
    inspector = inspect(engine)
    tables = (
        "review_deletion_intents",
        "review_deletion_artifacts",
        "issue_revisions",
        "annotation_sets",
    )
    columns = {
        table_name: tuple(
            (
                column["name"],
                str(column["type"]).lower(),
                bool(column["nullable"]),
            )
            for column in inspector.get_columns(table_name)
        )
        for table_name in tables[:2]
    }
    checks = {
        table_name: tuple(
            sorted(
                (
                    str(constraint["name"]),
                    _normalize_sqlite_sql(constraint["sqltext"]),
                )
                for constraint in inspector.get_check_constraints(table_name)
            ),
        )
        for table_name in tables
    }
    foreign_keys = {
        table_name: tuple(
            sorted(
                (
                    str(foreign_key["name"]),
                    tuple(foreign_key["constrained_columns"]),
                    str(foreign_key["referred_table"]),
                    tuple(foreign_key["referred_columns"]),
                    str(
                        foreign_key["options"].get("ondelete", ""),
                    ).upper(),
                )
                for foreign_key in inspector.get_foreign_keys(table_name)
            ),
        )
        for table_name in tables[1:]
    }
    with engine.connect() as connection:
        triggers = tuple(
            sorted(
                (
                    table_name,
                    name,
                    _normalize_sqlite_sql(sql),
                )
                for table_name, name, sql in connection.execute(
                    text(
                        "SELECT tbl_name, name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND tbl_name IN ("
                        "'review_deletion_intents', "
                        "'review_deletion_artifacts', "
                        "'issue_revisions', 'annotation_sets')",
                    ),
                )
            ),
        )
        foreign_key_violations = tuple(
            tuple(row)
            for row in connection.execute(text("PRAGMA foreign_key_check"))
        )
    return {
        "columns": columns,
        "checks": checks,
        "foreign_keys": foreign_keys,
        "triggers": triggers,
        "foreign_key_violations": foreign_key_violations,
    }


def _install_committed_0023_sqlite_rows(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO review_deletion_intents (
                  id, command_type, operation_identity_hash, subject_type,
                  project_ref_id_snapshot, review_item_id_snapshot,
                  status, attempts, last_error_code, completed_at,
                  created_at, updated_at
                ) VALUES (
                  'delete_c043_legacy', 'DeleteReviewItem', :identity,
                  'review_item', 'project-c043', 'item-c043', 'pending', 0,
                  NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """,
            ),
            {"identity": "c" * 64},
        )
        connection.execute(
            text(
                """
                INSERT INTO review_deletion_artifacts (
                  id, deletion_intent_id, artifact_kind,
                  relative_logical_target, expected_device, expected_inode,
                  expected_ctime_ns, expected_size, status, attempts,
                  last_error_code, deleted_at, created_at, updated_at
                ) VALUES (
                  'artifact_c043_legacy', 'delete_c043_legacy',
                  'original_media', 'files/file-c043-legacy', 1, 2, 3, 4,
                  'pending', 0, NULL, NULL,
                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """,
            ),
        )


def _install_c043_0023_sqlite_shape(engine: Engine) -> None:
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        with operations.batch_alter_table(
            "review_deletion_intents",
            recreate="always",
        ) as batch_op:
            batch_op.add_column(
                Column("cleanup_claim_id", String(length=64), nullable=True),
            )
            batch_op.add_column(
                Column(
                    "cleanup_claim_expires_at",
                    DateTime(timezone=True),
                    nullable=True,
                ),
            )
            batch_op.create_check_constraint(
                "ck_review_deletion_intents_cleanup_claim",
                "(status = 'cleaning' AND cleanup_claim_id IS NOT NULL "
                "AND cleanup_claim_expires_at IS NOT NULL) OR "
                "(status != 'cleaning' AND cleanup_claim_id IS NULL "
                "AND cleanup_claim_expires_at IS NULL)",
            )
        with operations.batch_alter_table(
            "review_deletion_artifacts",
            recreate="always",
        ) as batch_op:
            batch_op.add_column(
                Column("expected_sha256", String(length=64), nullable=False),
            )
            batch_op.create_check_constraint(
                "ck_review_deletion_artifacts_sha256",
                "length(expected_sha256) = 64",
            )
        for table_name in ("issue_revisions", "annotation_sets"):
            connection.execute(
                text(f"DROP TRIGGER IF EXISTS trg_{table_name}_no_delete"),
            )
            connection.execute(
                text(
                    f"""
                    CREATE TRIGGER trg_{table_name}_no_delete
                    BEFORE DELETE ON {table_name}
                    WHEN NOT EXISTS (
                      SELECT 1
                      FROM review_deletion_intents intent
                      WHERE intent.status = 'pending'
                        AND intent.project_ref_id_snapshot = OLD.project_ref_id
                        AND (
                          intent.subject_type = 'project'
                          OR (
                            intent.subject_type = 'review_item'
                            AND intent.review_item_id_snapshot = OLD.review_item_id
                          )
                        )
                    )
                    BEGIN
                      SELECT RAISE(ABORT, '{table_name} are immutable');
                    END
                    """,
                ),
            )
        connection.execute(
            text(
                """
                INSERT INTO review_deletion_intents (
                  id, command_type, operation_identity_hash, subject_type,
                  project_ref_id_snapshot, review_item_id_snapshot,
                  status, attempts, last_error_code, cleanup_claim_id,
                  cleanup_claim_expires_at, completed_at, created_at, updated_at
                ) VALUES (
                  'delete_c043_legacy', 'DeleteReviewItem', :identity,
                  'review_item', 'project-c043', 'item-c043', 'pending', 0,
                  NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """,
            ),
            {"identity": "c" * 64},
        )
        connection.execute(
            text(
                """
                INSERT INTO review_deletion_artifacts (
                  id, deletion_intent_id, artifact_kind,
                  relative_logical_target, expected_device, expected_inode,
                  expected_ctime_ns, expected_size, expected_sha256,
                  status, attempts, last_error_code, deleted_at,
                  created_at, updated_at
                ) VALUES (
                  'artifact_c043_legacy', 'delete_c043_legacy',
                  'original_media', 'files/file-c043-legacy', 1, 2, 3, 4,
                  :sha256, 'pending', 0, NULL, NULL,
                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """,
            ),
            {"sha256": hashlib.sha256(b"c043 legacy artifact").hexdigest()},
        )


def test_committed_0023_keeps_task1_schema_and_0024_adds_task4_cleanup_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "task4-forward-0024.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    config = _alembic_config(root)

    alembic_command.upgrade(config, "20260824_0023")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        intent_columns = {
            column["name"]
            for column in inspector.get_columns("review_deletion_intents")
        }
        artifact_columns = {
            column["name"]
            for column in inspector.get_columns("review_deletion_artifacts")
        }
        assert "cleanup_claim_id" not in intent_columns
        assert "cleanup_claim_expires_at" not in intent_columns
        assert "expected_sha256" not in artifact_columns
    finally:
        engine.dispose()

    alembic_command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        intent_columns = {
            column["name"]
            for column in inspector.get_columns("review_deletion_intents")
        }
        artifact_columns = {
            column["name"]
            for column in inspector.get_columns("review_deletion_artifacts")
        }
        assert {"cleanup_claim_id", "cleanup_claim_expires_at"} <= intent_columns
        assert "expected_sha256" in artifact_columns
    finally:
        engine.dispose()


def test_0024_sqlite_committed_and_c043_stamped_shapes_converge_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]

    def migrate_starting_shape(
        db_path: Path,
        *,
        c043_shape: bool,
    ) -> tuple[
        dict[str, Any],
        tuple[object, ...],
        tuple[object, ...],
    ]:
        _set_migration_env(db_path, tmp_path, monkeypatch)
        config = _alembic_config(root)
        alembic_command.upgrade(config, "20260824_0023")
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            if c043_shape:
                _install_c043_0023_sqlite_shape(engine)
            else:
                _install_committed_0023_sqlite_rows(engine)
        finally:
            engine.dispose()
        alembic_command.upgrade(config, "20260824_0024")
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            inventory = _sqlite_task4_schema_inventory(engine)
            with engine.connect() as connection:
                intent_row = tuple(
                    connection.execute(
                        text(
                            "SELECT command_type, operation_identity_hash, "
                            "subject_type, project_ref_id_snapshot, "
                            "review_item_id_snapshot, status, attempts, "
                            "last_error_code, cleanup_claim_id, "
                            "cleanup_claim_expires_at, completed_at "
                            "FROM review_deletion_intents "
                            "WHERE id = 'delete_c043_legacy'",
                        ),
                    ).one(),
                )
                artifact_row = tuple(
                    connection.execute(
                        text(
                            "SELECT artifact_kind, relative_logical_target, "
                            "expected_device, expected_inode, "
                            "expected_ctime_ns, expected_size, "
                            "expected_sha256, status, attempts, "
                            "last_error_code, deleted_at "
                            "FROM review_deletion_artifacts "
                            "WHERE id = 'artifact_c043_legacy'",
                        ),
                    ).one(),
                )
                assert connection.scalar(
                    text("SELECT version_num FROM alembic_version"),
                ) == "20260824_0024"
            return inventory, intent_row, artifact_row
        finally:
            engine.dispose()

    committed_inventory, committed_intent, committed_artifact = (
        migrate_starting_shape(
            tmp_path / "task4-parity-committed-0023.db",
            c043_shape=False,
        )
    )
    c043_inventory, c043_intent, c043_artifact = migrate_starting_shape(
        tmp_path / "task4-parity-c043-0023.db",
        c043_shape=True,
    )

    assert committed_inventory == c043_inventory
    assert committed_intent == c043_intent == (
        "DeleteReviewItem",
        "c" * 64,
        "review_item",
        "project-c043",
        "item-c043",
        "pending",
        0,
        None,
        None,
        None,
        None,
    )
    assert committed_artifact[:6] == c043_artifact[:6] == (
        "original_media",
        "files/file-c043-legacy",
        1,
        2,
        3,
        4,
    )
    assert committed_artifact[6] is None
    assert c043_artifact[6] == hashlib.sha256(
        b"c043 legacy artifact",
    ).hexdigest()
    assert committed_artifact[7:] == c043_artifact[7:] == (
        "pending",
        0,
        None,
        None,
    )
    intent_checks = {
        name
        for name, _sql in committed_inventory["checks"][
            "review_deletion_intents"
        ]
    }
    assert "ck_review_deletion_intents_cleanup_claim" not in intent_checks
    assert committed_inventory["foreign_key_violations"] == ()


@pytest.mark.parametrize(
    "failure_window",
    (
        "after_artifact_detach",
        "after_artifact_detach_divergent",
        "after_artifact_detach_temp_only_ctas",
        "during_intent_parent_rebuild",
        "after_final_artifact_normalization",
    ),
)
def test_0024_sqlite_c043_retry_recovers_known_batch_failure_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_window: str,
) -> None:
    root = Path(__file__).resolve().parents[2]
    canonical_path = tmp_path / f"task4-retry-canonical-{failure_window}.db"
    failed_path = tmp_path / f"task4-retry-failed-{failure_window}.db"
    allowed_temporary_tables = {
        "_alembic_tmp_review_deletion_artifacts",
        "_alembic_tmp_review_deletion_intents",
        "_alembic_tmp_issue_revisions",
        "_alembic_tmp_annotation_sets",
    }

    def prepare_c043_start(db_path: Path) -> object:
        _set_migration_env(db_path, tmp_path, monkeypatch)
        config = _alembic_config(root)
        alembic_command.upgrade(config, "20260824_0023")
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            _install_c043_0023_sqlite_shape(engine)
        finally:
            engine.dispose()
        return config

    canonical_config = prepare_c043_start(canonical_path)
    alembic_command.upgrade(canonical_config, "20260824_0024")
    canonical_engine = create_engine(f"sqlite:///{canonical_path}")
    try:
        canonical_inventory = _sqlite_task4_schema_inventory(canonical_engine)
    finally:
        canonical_engine.dispose()

    failed_config = prepare_c043_start(failed_path)
    artifact_rename_count = 0
    injected = False

    class InjectedMigrationFailure(RuntimeError):
        pass

    def inject_after_real_ddl(
        connection: Any,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal artifact_rename_count, injected
        if injected:
            return
        database = connection.engine.url.database
        if database is None or Path(database).resolve() != failed_path.resolve():
            return
        normalized = _normalize_sqlite_sql(statement)
        artifact_rename = (
            "alter table _alembic_tmp_review_deletion_artifacts "
            "rename to review_deletion_artifacts"
        )
        if artifact_rename in normalized:
            artifact_rename_count += 1
        should_inject = (
            failure_window
            in {
                "after_artifact_detach",
                "after_artifact_detach_divergent",
                "after_artifact_detach_temp_only_ctas",
            }
            and artifact_rename_count == 1
        ) or (
            failure_window == "during_intent_parent_rebuild"
            and normalized.startswith("drop table review_deletion_intents")
        ) or (
            failure_window == "after_final_artifact_normalization"
            and artifact_rename_count == 2
        )
        if should_inject:
            injected = True
            raise InjectedMigrationFailure(failure_window)

    event.listen(Engine, "after_cursor_execute", inject_after_real_ddl)
    try:
        with pytest.raises(InjectedMigrationFailure, match=failure_window):
            alembic_command.upgrade(failed_config, "20260824_0024")
    finally:
        event.remove(Engine, "after_cursor_execute", inject_after_real_ddl)
    assert injected is True

    failed_engine = create_engine(f"sqlite:///{failed_path}")
    try:
        inspector = inspect(failed_engine)
        table_names = set(inspector.get_table_names())
        temporary_tables = {
            table_name
            for table_name in table_names
            if table_name.startswith("_alembic_tmp_")
        }
        assert temporary_tables <= allowed_temporary_tables
        assert "fcr_0024_recovery_manifest" in table_names
        with failed_engine.connect() as connection:
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version"),
            ) == "20260824_0023"
            for table_name, row_id in (
                ("review_deletion_intents", "delete_c043_legacy"),
                ("review_deletion_artifacts", "artifact_c043_legacy"),
            ):
                candidates = {
                    table_name,
                    f"_alembic_tmp_{table_name}",
                } & table_names
                assert candidates
                assert sum(
                    int(
                        connection.scalar(
                            text(
                                f'SELECT count(*) FROM "{candidate}" '
                                "WHERE id = :row_id",
                            ),
                            {"row_id": row_id},
                        )
                        or 0
                    )
                    for candidate in candidates
                ) >= 1
    finally:
        failed_engine.dispose()

    if failure_window in {
        "after_artifact_detach_divergent",
        "after_artifact_detach_temp_only_ctas",
    }:
        failed_engine = create_engine(f"sqlite:///{failed_path}")
        try:
            with failed_engine.begin() as connection:
                if failure_window == "after_artifact_detach_divergent":
                    connection.execute(
                        text(
                            "INSERT INTO "
                            "_alembic_tmp_review_deletion_artifacts "
                            "SELECT * FROM review_deletion_artifacts",
                        ),
                    )
                    connection.execute(
                        text(
                            "UPDATE _alembic_tmp_review_deletion_artifacts "
                            "SET relative_logical_target = "
                            "'files/file-same-id-different-content' "
                            "WHERE id = 'artifact_c043_legacy'",
                        ),
                    )
                else:
                    connection.execute(
                        text(
                            "DROP TABLE "
                            "_alembic_tmp_review_deletion_artifacts",
                        ),
                    )
                    connection.execute(
                        text(
                            "CREATE TABLE "
                            "_alembic_tmp_review_deletion_artifacts "
                            "AS SELECT * FROM review_deletion_artifacts",
                        ),
                    )
                    connection.execute(
                        text("DROP TABLE review_deletion_artifacts"),
                    )
        finally:
            failed_engine.dispose()

        def recovery_snapshot() -> tuple[object, ...]:
            snapshot_engine = create_engine(f"sqlite:///{failed_path}")
            try:
                with snapshot_engine.connect() as connection:
                    schema = tuple(
                        tuple(row)
                        for row in connection.execute(
                            text(
                                "SELECT type, name, tbl_name, sql "
                                "FROM sqlite_master "
                                "WHERE name NOT LIKE 'sqlite_%' "
                                "ORDER BY type, name",
                            ),
                        )
                    )
                    table_names = {
                        row[1] for row in schema if row[0] == "table"
                    }
                    rows = tuple(
                        (
                            table_name,
                            tuple(
                                tuple(row)
                                for row in connection.execute(
                                    text(
                                        f'SELECT * FROM "{table_name}" '
                                        "ORDER BY id",
                                    ),
                                )
                            ),
                        )
                        for table_name in (
                            "review_deletion_artifacts",
                            "_alembic_tmp_review_deletion_artifacts",
                        )
                        if table_name in table_names
                    )
                    revision_value = connection.scalar(
                        text("SELECT version_num FROM alembic_version"),
                    )
                return schema, rows, revision_value
            finally:
                snapshot_engine.dispose()

        before_rejected_retry = recovery_snapshot()
        _set_migration_env(failed_path, tmp_path, monkeypatch)
        with pytest.raises(RuntimeError):
            alembic_command.upgrade(failed_config, "20260824_0024")
        assert recovery_snapshot() == before_rejected_retry
        return

    _set_migration_env(failed_path, tmp_path, monkeypatch)
    alembic_command.upgrade(failed_config, "20260824_0024")
    recovered_engine = create_engine(f"sqlite:///{failed_path}")
    try:
        assert _sqlite_task4_schema_inventory(
            recovered_engine,
        ) == canonical_inventory
        recovered_inspector = inspect(recovered_engine)
        assert not {
            table_name
            for table_name in recovered_inspector.get_table_names()
            if table_name.startswith("_alembic_tmp_")
        }
        assert "fcr_0024_recovery_manifest" not in {
            table_name for table_name in recovered_inspector.get_table_names()
        }
        with recovered_engine.connect() as connection:
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version"),
            ) == "20260824_0024"
            assert tuple(
                connection.execute(
                    text(
                        "SELECT status, cleanup_claim_id, "
                        "cleanup_claim_expires_at FROM "
                        "review_deletion_intents "
                        "WHERE id = 'delete_c043_legacy'",
                    ),
                ).one(),
            ) == ("pending", None, None)
            assert tuple(
                connection.execute(
                    text(
                        "SELECT status, expected_sha256, "
                        "relative_logical_target FROM "
                        "review_deletion_artifacts "
                        "WHERE id = 'artifact_c043_legacy'",
                    ),
                ).one(),
            ) == (
                "pending",
                hashlib.sha256(b"c043 legacy artifact").hexdigest(),
                "files/file-c043-legacy",
            )
    finally:
        recovered_engine.dispose()


@pytest.mark.parametrize(
    ("manifest_corruption", "expected_error"),
    (
        ("forged_digest", "identity mismatch"),
        ("expired_revision", "expired"),
        ("unknown_stage", "unknown stage"),
    ),
)
def test_0024_sqlite_recovery_manifest_corruption_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_corruption: str,
    expected_error: str,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / f"task4-manifest-{manifest_corruption}.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    config = _alembic_config(root)
    alembic_command.upgrade(config, "20260824_0023")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _install_c043_0023_sqlite_shape(engine)
    finally:
        engine.dispose()

    injected = False

    class InjectedMigrationFailure(RuntimeError):
        pass

    def fail_after_detach(
        connection: Any,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal injected
        database = connection.engine.url.database
        if (
            not injected
            and database is not None
            and Path(database).resolve() == db_path.resolve()
            and "alter table _alembic_tmp_review_deletion_artifacts "
            "rename to review_deletion_artifacts"
            in _normalize_sqlite_sql(statement)
        ):
            injected = True
            raise InjectedMigrationFailure(manifest_corruption)

    event.listen(Engine, "after_cursor_execute", fail_after_detach)
    try:
        with pytest.raises(InjectedMigrationFailure):
            alembic_command.upgrade(config, "20260824_0024")
    finally:
        event.remove(Engine, "after_cursor_execute", fail_after_detach)
    assert injected is True

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            manifest_sql = connection.scalar(
                text(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'fcr_0024_recovery_manifest'",
                ),
            )
            assert isinstance(manifest_sql, str)
            connection.execute(text("DROP TABLE fcr_0024_recovery_manifest"))
            if manifest_corruption == "forged_digest":
                manifest_sql = re.sub(
                    r"(source_schema_sha256 TEXT NOT NULL DEFAULT ')[0-9a-f]{64}'",
                    rf"\g<1>{'f' * 64}'",
                    manifest_sql,
                )
            elif manifest_corruption == "expired_revision":
                manifest_sql = manifest_sql.replace(
                    "'20260824_0024'",
                    "'20260824_0000'",
                    1,
                )
            else:
                manifest_sql = manifest_sql.replace(
                    "'artifact_detach_intent_fk'",
                    "'unknown_stage'",
                    1,
                )
            connection.execute(text(manifest_sql))
    finally:
        engine.dispose()

    def snapshot() -> tuple[object, ...]:
        snapshot_engine = create_engine(f"sqlite:///{db_path}")
        try:
            with snapshot_engine.connect() as connection:
                return (
                    tuple(
                        tuple(row)
                        for row in connection.execute(
                            text(
                                "SELECT type, name, tbl_name, sql "
                                "FROM sqlite_master "
                                "WHERE name NOT LIKE 'sqlite_%' "
                                "ORDER BY type, name",
                            ),
                        )
                    ),
                    connection.scalar(
                        text("SELECT version_num FROM alembic_version"),
                    ),
                )
        finally:
            snapshot_engine.dispose()

    before = snapshot()
    with pytest.raises(RuntimeError, match=expected_error):
        alembic_command.upgrade(config, "20260824_0024")
    assert snapshot() == before


def test_0024_sqlite_retry_after_manifest_clear_before_stamp_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    canonical_path = tmp_path / "task4-pre-stamp-canonical.db"
    failed_path = tmp_path / "task4-pre-stamp-failed.db"

    def prepare(db_path: Path) -> object:
        _set_migration_env(db_path, tmp_path, monkeypatch)
        config = _alembic_config(root)
        alembic_command.upgrade(config, "20260824_0023")
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            _install_c043_0023_sqlite_shape(engine)
        finally:
            engine.dispose()
        return config

    canonical_config = prepare(canonical_path)
    alembic_command.upgrade(canonical_config, "20260824_0024")
    canonical_engine = create_engine(f"sqlite:///{canonical_path}")
    try:
        canonical_inventory = _sqlite_task4_schema_inventory(canonical_engine)
    finally:
        canonical_engine.dispose()

    failed_config = prepare(failed_path)
    injected = False

    class InjectedStampFailure(RuntimeError):
        pass

    def fail_before_stamp(
        connection: Any,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal injected
        database = connection.engine.url.database
        if (
            not injected
            and database is not None
            and Path(database).resolve() == failed_path.resolve()
            and _normalize_sqlite_sql(statement).startswith(
                "update alembic_version set version_num=",
            )
        ):
            injected = True
            raise InjectedStampFailure("before 0024 stamp")

    event.listen(Engine, "before_cursor_execute", fail_before_stamp)
    try:
        with pytest.raises(InjectedStampFailure):
            alembic_command.upgrade(failed_config, "20260824_0024")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_before_stamp)
    assert injected is True

    engine = create_engine(f"sqlite:///{failed_path}")
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        temporary_tables = {
            table_name
            for table_name in table_names
            if table_name.startswith("_alembic_tmp_")
        }
        if temporary_tables:
            assert "fcr_0024_recovery_manifest" in table_names
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version"),
            ) == "20260824_0023"
    finally:
        engine.dispose()

    _set_migration_env(failed_path, tmp_path, monkeypatch)
    alembic_command.upgrade(failed_config, "20260824_0024")
    engine = create_engine(f"sqlite:///{failed_path}")
    try:
        assert _sqlite_task4_schema_inventory(engine) == canonical_inventory
        assert "fcr_0024_recovery_manifest" not in inspect(
            engine,
        ).get_table_names()
    finally:
        engine.dispose()


def test_0024_sqlite_unknown_alembic_temporary_table_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "task4-unknown-alembic-temp.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    config = _alembic_config(root)
    alembic_command.upgrade(config, "20260824_0023")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _install_c043_0023_sqlite_shape(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE _alembic_tmp_unrelated ("
                    "id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
                ),
            )
            connection.execute(
                text(
                    "INSERT INTO _alembic_tmp_unrelated (id, payload) "
                    "VALUES ('sentinel', 'must-survive')",
                ),
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="unknown Alembic temporary table"):
        alembic_command.upgrade(config, "20260824_0024")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version"),
            ) == "20260824_0023"
            assert connection.scalar(
                text(
                    "SELECT payload FROM _alembic_tmp_unrelated "
                    "WHERE id = 'sentinel'",
                ),
            ) == "must-survive"
    finally:
        engine.dispose()


def test_0024_sqlite_known_temp_preflight_failure_changes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "task4-known-temp-preflight.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    config = _alembic_config(root)
    alembic_command.upgrade(config, "20260824_0023")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _install_c043_0023_sqlite_shape(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE _alembic_tmp_review_deletion_intents "
                    "AS SELECT * FROM review_deletion_intents",
                ),
            )
            connection.execute(
                text(
                    "CREATE TABLE _alembic_tmp_review_deletion_artifacts "
                    "AS SELECT * FROM review_deletion_artifacts",
                ),
            )
            connection.execute(
                text(
                    "UPDATE _alembic_tmp_review_deletion_artifacts "
                    "SET relative_logical_target = "
                    "'files/file-same-id-different-content' "
                    "WHERE id = 'artifact_c043_legacy'",
                ),
            )
    finally:
        engine.dispose()

    def snapshot() -> tuple[object, ...]:
        snapshot_engine = create_engine(f"sqlite:///{db_path}")
        try:
            with snapshot_engine.connect() as connection:
                schema = tuple(
                    tuple(row)
                    for row in connection.execute(
                        text(
                            "SELECT type, name, tbl_name, sql "
                            "FROM sqlite_master "
                            "WHERE name NOT LIKE 'sqlite_%' "
                            "ORDER BY type, name",
                        ),
                    )
                )
                rows = tuple(
                    (
                        table_name,
                        tuple(
                            tuple(row)
                            for row in connection.execute(
                                text(
                                    f'SELECT * FROM "{table_name}" '
                                    "ORDER BY id",
                                ),
                            )
                        ),
                    )
                    for table_name in (
                        "review_deletion_intents",
                        "review_deletion_artifacts",
                        "_alembic_tmp_review_deletion_intents",
                        "_alembic_tmp_review_deletion_artifacts",
                    )
                )
                revision = connection.scalar(
                    text("SELECT version_num FROM alembic_version"),
                )
            return schema, rows, revision
        finally:
            snapshot_engine.dispose()

    before = snapshot()
    with pytest.raises(RuntimeError):
        alembic_command.upgrade(config, "20260824_0024")
    assert snapshot() == before


def test_0024_reconciles_c043_0023_sqlite_shape_without_losing_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "task4-c043-0023-shape.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    config = _alembic_config(root)
    alembic_command.upgrade(config, "20260824_0023")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _install_c043_0023_sqlite_shape(engine)
        assert {
            "cleanup_claim_id",
            "cleanup_claim_expires_at",
        } <= {
            column["name"]
            for column in inspect(engine).get_columns(
                "review_deletion_intents",
            )
        }
        assert "expected_sha256" in {
            column["name"]
            for column in inspect(engine).get_columns(
                "review_deletion_artifacts",
            )
        }
    finally:
        engine.dispose()

    alembic_command.upgrade(config, "20260824_0024")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        intent_columns = [
            column["name"]
            for column in inspector.get_columns("review_deletion_intents")
        ]
        artifact_columns = [
            column["name"]
            for column in inspector.get_columns("review_deletion_artifacts")
        ]
        assert intent_columns.count("cleanup_claim_id") == 1
        assert intent_columns.count("cleanup_claim_expires_at") == 1
        assert artifact_columns.count("expected_sha256") == 1
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints(
                "review_deletion_artifacts",
            )
        } >= {
            "ck_review_deletion_artifacts_sha256",
            "ck_review_deletion_artifacts_kind_namespace",
        }
        for table_name in ("issue_revisions", "annotation_sets"):
            issue_fk = next(
                foreign_key
                for foreign_key in inspector.get_foreign_keys(table_name)
                if foreign_key["name"]
                == f"fk_{table_name}_{'issue_identity'}"
            )
            assert issue_fk["options"].get("ondelete") == "CASCADE"
        with engine.connect() as connection:
            intent_row = connection.execute(
                text(
                    "SELECT status, cleanup_claim_id, cleanup_claim_expires_at "
                    "FROM review_deletion_intents "
                    "WHERE id = 'delete_c043_legacy'",
                ),
            ).one()
            artifact_row = connection.execute(
                text(
                    "SELECT expected_sha256, relative_logical_target "
                    "FROM review_deletion_artifacts "
                    "WHERE id = 'artifact_c043_legacy'",
                ),
            ).one()
            trigger_rows = connection.execute(
                text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'trigger' AND name IN ("
                    "'trg_issue_revisions_no_delete', "
                    "'trg_annotation_sets_no_delete', "
                    "'trg_review_deletion_artifacts_sha_insert', "
                    "'trg_review_deletion_intents_cleanup_claim_insert', "
                    "'trg_review_deletion_intents_cleanup_claim_update')",
                ),
            ).all()
        assert tuple(intent_row) == ("pending", None, None)
        assert tuple(artifact_row) == (
            hashlib.sha256(b"c043 legacy artifact").hexdigest(),
            "files/file-c043-legacy",
        )
        trigger_sql = {name: sql for name, sql in trigger_rows}
        assert len(trigger_sql) == 5
        for table_name in ("issue_revisions", "annotation_sets"):
            sql = trigger_sql[f"trg_{table_name}_no_delete"]
            assert "review_deletion_intents" not in sql
            assert "when exists" in sql.lower()
            assert "review_issues" in sql
    finally:
        engine.dispose()


def test_fabricated_pending_intent_cannot_delete_one_immutable_annotation(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    project, item = create_project_item(client, code="TASK4_FAKE_INTENT")
    issue = create_issue(
        client,
        project["project_ref_id"],
        item,
        content="fabricated intent must not authorize partial deletion",
        ann=annotation("fake-intent"),
    )
    annotation_id = issue["current_annotation_set"]["id"]
    with SessionLocal() as session:
        session.add(
            ReviewDeletionIntentModel(
                id="delete_" + "1" * 32,
                command_type="DeleteReviewItem",
                operation_identity_hash="1" * 64,
                subject_type="review_item",
                project_ref_id_snapshot=project["project_ref_id"],
                review_item_id_snapshot=item["id"],
                status="pending",
                attempts=0,
            ),
        )
        session.flush()
        with pytest.raises((DBAPIError, IntegrityError)):
            session.execute(
                delete(ReviewAnnotationSetModel).where(
                    ReviewAnnotationSetModel.id == annotation_id,
                ),
            )
            session.commit()
        session.rollback()
    with SessionLocal() as observer:
        assert observer.get(ReviewAnnotationSetModel, annotation_id) is not None


@pytest.mark.parametrize(
    ("artifact_kind", "relative_target"),
    [
        ("project_package", "files/file-legacy"),
        ("original_media", "packages/pkg_" + "2" * 32 + ".zip"),
    ],
)
def test_database_rejects_artifact_kind_namespace_mismatch(
    client: TestClient,
    artifact_kind: str,
    relative_target: str,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    del client
    with SessionLocal() as session:
        intent = ReviewDeletionIntentModel(
            id="delete_" + "2" * 32,
            command_type="DeleteReviewItem",
            operation_identity_hash="2" * 64,
            subject_type="review_item",
            project_ref_id_snapshot="project-kind-mismatch",
            review_item_id_snapshot="item-kind-mismatch",
            status="pending",
            attempts=0,
        )
        session.add(intent)
        session.flush()
        session.add(
            ReviewDeletionArtifactModel(
                id="artifact_" + "2" * 32,
                deletion_intent_id=intent.id,
                artifact_kind=artifact_kind,
                relative_logical_target=relative_target,
                expected_device=1,
                expected_inode=2,
                expected_ctime_ns=3,
                expected_size=4,
                expected_sha256=hashlib.sha256(b"mismatch").hexdigest(),
                status="pending",
                attempts=0,
            ),
        )
        with pytest.raises((DBAPIError, IntegrityError)):
            session.commit()


@pytest.mark.parametrize(
    ("artifact_kind", "relative_target"),
    [
        ("project_package", "files/file_" + "3" * 32),
        ("thumbnail", "packages/pkg_" + "3" * 32 + ".zip"),
    ],
)
def test_runtime_rejects_artifact_kind_namespace_mismatch(
    client: TestClient,
    artifact_kind: str,
    relative_target: str,
) -> None:
    from backend.app.maintenance_cleanup import _delete_review_deletion_artifact
    from backend.app.settings import get_settings

    del client
    artifact = ReviewDeletionArtifactModel(
        id="artifact_" + "3" * 32,
        deletion_intent_id="delete_" + "3" * 32,
        artifact_kind=artifact_kind,
        relative_logical_target=relative_target,
        expected_device=1,
        expected_inode=2,
        expected_ctime_ns=3,
        expected_size=4,
        expected_sha256=hashlib.sha256(b"mismatch").hexdigest(),
        status="pending",
        attempts=0,
    )
    with pytest.raises(OSError, match="kind.*namespace"):
        _delete_review_deletion_artifact(
            artifact,
            claim_id="cleanup_" + "3" * 32,
            settings=get_settings(),
        )


@pytest.mark.parametrize(
    ("artifact_kind", "namespace", "name"),
    [
        ("project_package", "files", "file_" + "5" * 32),
        ("thumbnail", "packages", "pkg_" + "5" * 32 + ".zip"),
    ],
)
def test_runtime_kind_namespace_mismatch_preserves_target_and_failed_intent(
    client: TestClient,
    artifact_kind: str,
    namespace: str,
    name: str,
) -> None:
    from backend.app.maintenance_cleanup import process_review_deletion_intents
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.settings import get_settings

    del client
    root = (
        get_settings().storage_root
        if namespace == "files"
        else get_settings().package_root
    )
    target = root / namespace / name if namespace == "files" else root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    content = b"kind namespace mismatch must survive"
    target.write_bytes(content)
    identity = target.stat()
    intent_id = "delete_" + "5" * 32
    with SessionLocal() as session:
        session.connection().exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        session.add(
            ReviewDeletionIntentModel(
                id=intent_id,
                command_type="DeleteReviewItem",
                operation_identity_hash="5" * 64,
                subject_type="review_item",
                project_ref_id_snapshot="project-kind-runtime",
                review_item_id_snapshot="item-kind-runtime",
                status="pending",
                attempts=0,
            ),
        )
        session.flush()
        session.add(
            ReviewDeletionArtifactModel(
                id="artifact_" + "5" * 32,
                deletion_intent_id=intent_id,
                artifact_kind=artifact_kind,
                relative_logical_target=f"{namespace}/{name}",
                expected_device=identity.st_dev,
                expected_inode=identity.st_ino,
                expected_ctime_ns=identity.st_ctime_ns,
                expected_size=identity.st_size,
                expected_sha256=hashlib.sha256(content).hexdigest(),
                status="pending",
                attempts=0,
            ),
        )
        session.commit()
        session.connection().exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        session.commit()

    assert process_review_deletion_intents(get_settings()) == (0, 1)
    assert target.read_bytes() == content
    with SessionLocal() as observer:
        intent = observer.get(ReviewDeletionIntentModel, intent_id)
        artifact = observer.get(
            ReviewDeletionArtifactModel,
            "artifact_" + "5" * 32,
        )
        assert intent is not None and intent.status == "failed"
        assert artifact is not None and artifact.status == "failed"


def test_0024_downgrade_restores_0023_and_reupgrade_is_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "task4-forward-roundtrip.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    config = _alembic_config(root)
    alembic_command.upgrade(config, "head")
    alembic_command.downgrade(config, "20260824_0023")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "cleanup_claim_id" not in {
            column["name"]
            for column in inspect(engine).get_columns("review_deletion_intents")
        }
        with engine.connect() as connection:
            immutable_trigger = connection.scalar(
                text(
                    "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                    "AND name = 'trg_annotation_sets_no_delete'",
                ),
            )
        assert isinstance(immutable_trigger, str)
        assert "outside parent deletion" not in immutable_trigger
        assert "RAISE(ABORT" in immutable_trigger
    finally:
        engine.dispose()
    alembic_command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "cleanup_claim_id" in {
            column["name"]
            for column in inspect(engine).get_columns("review_deletion_intents")
        }
    finally:
        engine.dispose()


def test_old_0023_rows_forward_migrate_without_fabricating_missing_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "task4-old-0023-data.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    config = _alembic_config(root)
    alembic_command.upgrade(config, "20260824_0023")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO review_deletion_intents (
                      id, command_type, operation_identity_hash, subject_type,
                      project_ref_id_snapshot, review_item_id_snapshot,
                      status, attempts, created_at, updated_at
                    ) VALUES (
                      'delete_legacy_0023', 'DeleteReviewItem', :identity,
                      'review_item', 'project-legacy', 'item-legacy',
                      'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """,
                ),
                {"identity": "4" * 64},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO review_deletion_artifacts (
                      id, deletion_intent_id, artifact_kind,
                      relative_logical_target, expected_device, expected_inode,
                      expected_ctime_ns, expected_size, status, attempts,
                      created_at, updated_at
                    ) VALUES (
                      'artifact_legacy_0023', 'delete_legacy_0023',
                      'original_media', 'files/file-legacy', 1, 2, 3, 4,
                      'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """,
                ),
            )
    finally:
        engine.dispose()

    alembic_command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            assert connection.scalar(
                text(
                    "SELECT expected_sha256 FROM review_deletion_artifacts "
                    "WHERE id = 'artifact_legacy_0023'",
                ),
            ) is None
            connection.execute(
                text(
                    "UPDATE review_deletion_artifacts SET status = 'failed' "
                    "WHERE id = 'artifact_legacy_0023'",
                ),
            )
        with pytest.raises((DBAPIError, IntegrityError)):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO review_deletion_artifacts (
                          id, deletion_intent_id, artifact_kind,
                          relative_logical_target, expected_device,
                          expected_inode, expected_ctime_ns, expected_size,
                          expected_sha256, status, attempts, created_at, updated_at
                        ) VALUES (
                          'artifact_new_without_sha', 'delete_legacy_0023',
                          'original_media', 'files/file-new', 1, 2, 3, 4,
                          NULL, 'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """,
                    ),
                )
        from backend.app.maintenance_cleanup import (
            _claim_review_deletion_intents,
            _cleanup_claimed_review_deletions,
        )
        from backend.app.modules.final_cut_review.infra.sqlalchemy_models import utcnow
        from backend.app.settings import get_settings

        LocalSession = sessionmaker(bind=engine, expire_on_commit=False)
        with LocalSession() as session:
            claims = _claim_review_deletion_intents(session, utcnow())
            assert _cleanup_claimed_review_deletions(
                session,
                claims,
                get_settings(),
            ) == (0, 1)
            legacy_intent = session.get(
                ReviewDeletionIntentModel,
                "delete_legacy_0023",
            )
            assert legacy_intent is not None
            assert legacy_intent.status == "failed"
    finally:
        engine.dispose()


def _pause_postcommit_deletion_processor(monkeypatch: pytest.MonkeyPatch) -> object:
    import backend.app.maintenance_cleanup as maintenance_cleanup

    original = maintenance_cleanup.process_review_deletion_intents
    monkeypatch.setattr(
        maintenance_cleanup,
        "process_review_deletion_intents",
        lambda *_args, **_kwargs: (0, 0),
    )
    return original


def _make_ttl_expired(path: Path) -> None:
    from backend.app.settings import get_settings

    old = 1_700_000_000 - get_settings().upload_session_ttl_seconds
    os.utime(path, (old, old), follow_symlinks=False)


def _is_deletion_artifact_protection_query(statement: str) -> bool:
    normalized = " ".join(statement.lower().split())
    return (
        normalized.startswith("select")
        and "from review_deletion_artifacts" in normalized
        and "relative_logical_target" in normalized
    )


def test_sqlite_target_locks_sort_and_release_on_outer_rollback(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.settings import get_settings
    from backend.app.storage_mutation_locks import (
        acquire_storage_target_mutation_locks,
    )

    del client
    first = SessionLocal()
    acquired_by_second = threading.Event()
    second_errors: list[BaseException] = []
    try:
        acquired = acquire_storage_target_mutation_locks(
            first,
            get_settings(),
            [
                "packages/pkg_" + "b" * 32 + ".zip",
                "files/file_" + "a" * 32,
            ],
        )
        assert acquired == (
            "files/file_" + "a" * 32,
            "packages/pkg_" + "b" * 32 + ".zip",
        )

        def contend() -> None:
            try:
                with SessionLocal() as second:
                    acquire_storage_target_mutation_locks(
                        second,
                        get_settings(),
                        ["files/file_" + "a" * 32],
                    )
                    acquired_by_second.set()
                    second.rollback()
            except BaseException as error:
                second_errors.append(error)

        contender = threading.Thread(
            target=contend,
            name="task4-sqlite-lock-rollback-contender",
        )
        contender.start()
        crossed_before_rollback = acquired_by_second.wait(timeout=0.25)
        first.rollback()
        contender.join(timeout=5)
        assert not contender.is_alive()
        assert not crossed_before_rollback
        assert acquired_by_second.is_set()
        assert second_errors == []
    finally:
        first.rollback()
        first.close()


def test_sqlite_lock_release_error_does_not_change_committed_ack(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.storage_mutation_locks as storage_mutation_locks

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.settings import get_settings

    del client
    target = "files/file_" + "d" * 32
    first = SessionLocal()
    storage_mutation_locks.acquire_storage_target_mutation_locks(
        first,
        get_settings(),
        [target],
    )
    original_flock = storage_mutation_locks.fcntl.flock

    def fail_explicit_unlock(descriptor: int, operation: int) -> object:
        if operation == fcntl.LOCK_UN:
            raise OSError("forced explicit unlock failure")
        return original_flock(descriptor, operation)

    monkeypatch.setattr(
        storage_mutation_locks.fcntl,
        "flock",
        fail_explicit_unlock,
    )
    try:
        first.commit()
    finally:
        first.close()
    monkeypatch.setattr(
        storage_mutation_locks.fcntl,
        "flock",
        original_flock,
    )
    with SessionLocal() as second:
        assert storage_mutation_locks.acquire_storage_target_mutation_locks(
            second,
            get_settings(),
            [target],
        ) == (target,)
        second.rollback()


def test_delete_first_target_lock_blocks_regular_orphan_protection_query(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance_cleanup

    from backend.app.modules.final_cut_review.infra import database
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.deletion import (
        DurableDeletionCoordinator,
    )
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        ReviewDeletionIntentModel,
        ReviewVersionModel,
        utcnow,
    )
    from backend.app.settings import get_settings

    project, item = create_project_item(client, code="TASK4_LOCK_DELETE_FIRST")
    with SessionLocal() as observer:
        version = observer.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        target = get_settings().storage_root / "files" / version.original_file_id
    _make_ttl_expired(target)
    _pause_postcommit_deletion_processor(monkeypatch)

    deletion_at_capture = threading.Event()
    release_deletion = threading.Event()
    orphan_reached_protection_query = threading.Event()
    original_create_intent = DurableDeletionCoordinator._create_intent

    def pause_after_target_locks(
        coordinator: DurableDeletionCoordinator,
        **kwargs: Any,
    ) -> ReviewDeletionIntentModel:
        deletion_at_capture.set()
        if not release_deletion.wait(timeout=5):
            raise TimeoutError("test did not release deletion capture")
        return original_create_intent(coordinator, **kwargs)

    monkeypatch.setattr(
        DurableDeletionCoordinator,
        "_create_intent",
        pause_after_target_locks,
    )

    def observe_orphan_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == "task4-orphan-delete-first"
            and _is_deletion_artifact_protection_query(statement)
        ):
            orphan_reached_protection_query.set()

    event.listen(database.engine, "before_cursor_execute", observe_orphan_query)
    deletion_responses: list[object] = []
    deletion_errors: list[BaseException] = []
    orphan_results: list[tuple[int, int]] = []
    orphan_errors: list[BaseException] = []

    def run_deletion() -> None:
        try:
            deletion_responses.append(
                _delete_item(client, project["project_ref_id"], item),
            )
        except BaseException as error:
            deletion_errors.append(error)

    def run_orphan_cleanup() -> None:
        try:
            with SessionLocal() as session:
                orphan_results.append(
                    maintenance_cleanup._cleanup_orphan_managed_files(
                        session,
                        get_settings().storage_root / "files",
                        utcnow(),
                        kind="file",
                    ),
                )
        except BaseException as error:
            orphan_errors.append(error)

    deletion_thread = threading.Thread(
        target=run_deletion,
        name="task4-delete-first",
    )
    orphan_thread = threading.Thread(
        target=run_orphan_cleanup,
        name="task4-orphan-delete-first",
    )
    try:
        deletion_thread.start()
        assert deletion_at_capture.wait(timeout=5)
        orphan_thread.start()
        query_crossed_while_delete_held_target = (
            orphan_reached_protection_query.wait(timeout=0.5)
        )
    finally:
        release_deletion.set()
        deletion_thread.join(timeout=10)
        orphan_thread.join(timeout=10)
        event.remove(database.engine, "before_cursor_execute", observe_orphan_query)

    assert not deletion_thread.is_alive()
    assert not orphan_thread.is_alive()
    assert not query_crossed_while_delete_held_target
    assert deletion_errors == []
    assert orphan_errors == []
    assert len(deletion_responses) == 1
    response = deletion_responses[0]
    assert getattr(response, "status_code", None) == 200
    assert orphan_results == [(0, 0)]
    assert target.is_file()
    with SessionLocal() as observer:
        intent = observer.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.review_item_id_snapshot == item["id"],
            ),
        )
        assert intent is not None and intent.status == "pending"


def test_package_orphan_lock_blocks_delete_then_protects_hash_mismatch_replacement(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance_cleanup

    from backend.app.modules.final_cut_review.infra import database
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.deletion import (
        DurableDeletionCoordinator,
    )
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        ReviewDeletionArtifactModel,
        ReviewDeletionIntentModel,
        utcnow,
    )
    from backend.app.settings import get_settings

    project, item = create_project_item(client, code="TASK4_LOCK_ORPHAN_FIRST")
    finalize(client, project["project_ref_id"], item)
    package, _package_command = prepare_ready_package(
        client,
        project["project_ref_id"],
    )
    with SessionLocal() as observer:
        package_row = observer.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None
        target = Path(package_row.storage_path)
    current_item = api_data(
        client.get(
            f"/api/v1/final-cut-review/projects/{project['project_ref_id']}"
            f"/items/{item['id']}",
        ),
    )
    revoke = command(
        "RevokeFinalization",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "confirmed": True,
        },
    )
    revoked = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}"
        f"/items/{item['id']}/finalization/revoke",
        json=revoke,
        headers={
            "Idempotency-Key": revoke["command_id"],
            "If-Match": str(current_item["lock_version"]),
        },
    )
    assert revoked.status_code == 200, revoked.text
    current_project = api_data(
        client.get(
            f"/api/v1/final-cut-review/projects/{project['project_ref_id']}",
        ),
    )
    _make_ttl_expired(target)
    _pause_postcommit_deletion_processor(monkeypatch)

    orphan_holds_protection_boundary = threading.Event()
    release_orphan = threading.Event()
    deletion_at_capture = threading.Event()
    original_create_intent = DurableDeletionCoordinator._create_intent

    def pause_deletion_capture(
        coordinator: DurableDeletionCoordinator,
        **kwargs: Any,
    ) -> ReviewDeletionIntentModel:
        deletion_at_capture.set()
        return original_create_intent(coordinator, **kwargs)

    monkeypatch.setattr(
        DurableDeletionCoordinator,
        "_create_intent",
        pause_deletion_capture,
    )

    def pause_after_protection_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == "task4-orphan-package-first"
            and _is_deletion_artifact_protection_query(statement)
        ):
            orphan_holds_protection_boundary.set()
            if not release_orphan.wait(timeout=5):
                raise TimeoutError("test did not release package orphan query")

    event.listen(database.engine, "after_cursor_execute", pause_after_protection_query)
    orphan_results: list[tuple[int, int]] = []
    orphan_errors: list[BaseException] = []
    deletion_responses: list[object] = []
    deletion_errors: list[BaseException] = []

    def run_orphan_cleanup() -> None:
        try:
            with SessionLocal() as session:
                orphan_results.append(
                    maintenance_cleanup._cleanup_orphan_managed_files(
                        session,
                        get_settings().package_root,
                        utcnow(),
                        kind="package",
                    ),
                )
        except BaseException as error:
            orphan_errors.append(error)

    def run_project_deletion() -> None:
        try:
            deletion = command(
                "DeleteProject",
                {
                    "project_ref_id": project["project_ref_id"],
                    "confirmed": True,
                },
            )
            deletion_responses.append(
                client.post(
                    f"/api/v1/final-cut-review/review/projects/"
                    f"{project['project_ref_id']}/delete",
                    json=deletion,
                    headers={
                        "Idempotency-Key": deletion["command_id"],
                        "If-Match": str(current_project["lock_version"]),
                    },
                ),
            )
        except BaseException as error:
            deletion_errors.append(error)

    orphan_thread = threading.Thread(
        target=run_orphan_cleanup,
        name="task4-orphan-package-first",
    )
    deletion_thread = threading.Thread(
        target=run_project_deletion,
        name="task4-delete-package-second",
    )
    try:
        orphan_thread.start()
        assert orphan_holds_protection_boundary.wait(timeout=5)
        deletion_thread.start()
        deletion_crossed_while_orphan_held_target = deletion_at_capture.wait(
            timeout=0.5,
        )
    finally:
        release_orphan.set()
        orphan_thread.join(timeout=10)
        deletion_thread.join(timeout=10)
        event.remove(database.engine, "after_cursor_execute", pause_after_protection_query)

    assert not orphan_thread.is_alive()
    assert not deletion_thread.is_alive()
    assert not deletion_crossed_while_orphan_held_target
    assert orphan_errors == []
    assert deletion_errors == []
    assert orphan_results == [(0, 0)]
    assert len(deletion_responses) == 1
    response = deletion_responses[0]
    assert getattr(response, "status_code", None) == 200
    assert target.is_file()

    target.unlink()
    replacement = b"package replacement with a mismatched captured sha256"
    target.write_bytes(replacement)
    _make_ttl_expired(target)
    with SessionLocal() as session:
        assert maintenance_cleanup._cleanup_orphan_managed_files(
            session,
            get_settings().package_root,
            utcnow(),
            kind="package",
        ) == (0, 0)
    assert target.read_bytes() == replacement
    with SessionLocal() as observer:
        intent = observer.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.project_ref_id_snapshot
                == project["project_ref_id"],
                ReviewDeletionIntentModel.subject_type == "project",
            ),
        )
        assert intent is not None and intent.status == "pending"
        artifact = observer.scalar(
            select(ReviewDeletionArtifactModel).where(
                ReviewDeletionArtifactModel.deletion_intent_id == intent.id,
                ReviewDeletionArtifactModel.artifact_kind == "project_package",
            ),
        )
        assert artifact is not None
        assert artifact.expected_sha256 != hashlib.sha256(replacement).hexdigest()


def test_scoped_cleanup_claim_does_not_consume_unrelated_pending_intent(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.maintenance_cleanup import (
        _claim_review_deletion_intents,
        _cleanup_claimed_review_deletions,
    )
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        ReviewVersionModel,
        utcnow,
    )
    from backend.app.settings import get_settings

    project, item = create_project_item(client, code="TASK4_SCOPED_CLEANUP")
    with SessionLocal() as observer:
        version = observer.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        target = get_settings().storage_root / "files" / version.original_file_id

    _pause_postcommit_deletion_processor(monkeypatch)
    deleted = _delete_item(client, project["project_ref_id"], item)
    assert deleted.status_code == 200, deleted.text
    replacement = b"scoped cleanup must preserve this replacement"
    target.unlink()
    target.write_bytes(replacement)

    unrelated_intent_id = "delete_" + "9" * 32
    with SessionLocal.begin() as session:
        session.add(
            ReviewDeletionIntentModel(
                id=unrelated_intent_id,
                command_type="DeleteReviewItem",
                operation_identity_hash="9" * 64,
                subject_type="review_item",
                project_ref_id_snapshot="unrelated-project",
                review_item_id_snapshot="unrelated-item",
                status="pending",
                attempts=0,
            ),
        )

    with SessionLocal() as session:
        target_intent = session.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.review_item_id_snapshot == item["id"],
            ),
        )
        assert target_intent is not None
        claims = _claim_review_deletion_intents(
            session,
            utcnow(),
            intent_ids=[target_intent.id],
        )
        assert [intent_id for intent_id, _claim_id in claims] == [
            target_intent.id,
        ]
        assert _cleanup_claimed_review_deletions(
            session,
            claims,
            get_settings(),
        ) == (0, 1)

    assert target.read_bytes() == replacement
    with SessionLocal() as observer:
        failed_target = observer.get(ReviewDeletionIntentModel, target_intent.id)
        unrelated = observer.get(ReviewDeletionIntentModel, unrelated_intent_id)
        assert failed_target is not None and failed_target.status == "failed"
        assert unrelated is not None and unrelated.status == "pending"


def test_orphan_sweep_preserves_pending_regular_artifact_replacement(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance_cleanup

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        ReviewDeletionArtifactModel,
        ReviewDeletionIntentModel,
        ReviewVersionModel,
    )
    from backend.app.settings import get_settings

    project, item = create_project_item(client, code="TASK4_ORPHAN_REPLACEMENT")
    with SessionLocal() as observer:
        version = observer.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        target = get_settings().storage_root / "files" / version.original_file_id
    original_processor = _pause_postcommit_deletion_processor(monkeypatch)
    deleted = _delete_item(client, project["project_ref_id"], item)
    assert deleted.status_code == 200, deleted.text
    target.unlink()
    replacement = b"orphan-sweep-must-not-delete-replacement"
    target.write_bytes(replacement)
    _make_ttl_expired(target)
    monkeypatch.setattr(
        maintenance_cleanup,
        "process_review_deletion_intents",
        original_processor,
    )

    first = maintenance_cleanup.cleanup_temporary_files()
    second = maintenance_cleanup.cleanup_temporary_files()
    assert target.read_bytes() == replacement
    assert first["failed_review_deletions"] == 1
    assert second["failed_review_deletions"] == 1
    with SessionLocal() as observer:
        intent = observer.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.review_item_id_snapshot == item["id"],
            ),
        )
        assert intent is not None and intent.status == "failed"
        artifact = observer.scalar(
            select(ReviewDeletionArtifactModel).where(
                ReviewDeletionArtifactModel.deletion_intent_id == intent.id,
            ),
        )
        assert artifact is not None and artifact.status == "failed"


def test_orphan_sweep_preserves_pending_regular_artifact_on_sha_mismatch(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance_cleanup

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        ReviewDeletionArtifactModel,
        ReviewDeletionIntentModel,
        ReviewVersionModel,
    )
    from backend.app.settings import get_settings

    project, item = create_project_item(client, code="TASK4_ORPHAN_SHA")
    with SessionLocal() as observer:
        version = observer.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        target = get_settings().storage_root / "files" / version.original_file_id
    original_processor = _pause_postcommit_deletion_processor(monkeypatch)
    deleted = _delete_item(client, project["project_ref_id"], item)
    assert deleted.status_code == 200, deleted.text
    _make_ttl_expired(target)
    with SessionLocal.begin() as session:
        intent = session.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.review_item_id_snapshot == item["id"],
            ),
        )
        assert intent is not None
        artifact = session.scalar(
            select(ReviewDeletionArtifactModel).where(
                ReviewDeletionArtifactModel.deletion_intent_id == intent.id,
            ),
        )
        assert artifact is not None
        artifact.expected_sha256 = "f" * 64
    monkeypatch.setattr(
        maintenance_cleanup,
        "process_review_deletion_intents",
        original_processor,
    )

    result = maintenance_cleanup.cleanup_temporary_files()
    assert target.is_file()
    assert result["failed_review_deletions"] == 1
    with SessionLocal() as observer:
        intent = observer.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.review_item_id_snapshot == item["id"],
            ),
        )
        assert intent is not None and intent.status == "failed"


def test_orphan_sweep_preserves_pending_package_artifact_replacement(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance_cleanup

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        ReviewDeletionIntentModel,
    )
    project, item = create_project_item(client, code="TASK4_ORPHAN_PACKAGE")
    finalize(client, project["project_ref_id"], item)
    package, _package_command = prepare_ready_package(
        client,
        project["project_ref_id"],
    )
    with SessionLocal() as observer:
        package_row = observer.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None
        target = Path(package_row.storage_path)
    current_item = api_data(
        client.get(
            f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}",
        ),
    )
    revoke = command(
        "RevokeFinalization",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "confirmed": True,
        },
    )
    revoked = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/finalization/revoke",
        json=revoke,
        headers={
            "Idempotency-Key": revoke["command_id"],
            "If-Match": str(current_item["lock_version"]),
        },
    )
    assert revoked.status_code == 200, revoked.text
    current_project = api_data(
        client.get(
            f"/api/v1/final-cut-review/projects/{project['project_ref_id']}",
        ),
    )
    original_processor = _pause_postcommit_deletion_processor(monkeypatch)
    deletion = command(
        "DeleteProject",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
    )
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/delete",
        json=deletion,
        headers={
            "Idempotency-Key": deletion["command_id"],
            "If-Match": str(current_project["lock_version"]),
        },
    )
    assert response.status_code == 200, response.text
    target.unlink()
    replacement = b"package-orphan-sweep-must-not-delete"
    target.write_bytes(replacement)
    _make_ttl_expired(target)
    monkeypatch.setattr(
        maintenance_cleanup,
        "process_review_deletion_intents",
        original_processor,
    )

    result = maintenance_cleanup.cleanup_temporary_files()
    assert target.read_bytes() == replacement
    assert result["failed_review_deletions"] == 1
    with SessionLocal() as observer:
        intent = observer.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.project_ref_id_snapshot
                == project["project_ref_id"],
                ReviewDeletionIntentModel.subject_type == "project",
            ),
        )
        assert intent is not None and intent.status == "failed"


def test_unrelated_corrupt_or_locked_quarantine_does_not_block_other_intents(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance_cleanup

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        ReviewDeletionIntentModel,
        ReviewVersionModel,
    )
    from backend.app.safe_files import (
        DELETE_QUARANTINE_DIRECTORY,
        DELETE_QUARANTINE_MANIFEST,
        DELETE_QUARANTINE_TARGET,
    )
    from backend.app.settings import get_settings

    safe_project, safe_item = create_project_item(
        client,
        code="TASK4_QUARANTINE_SAFE",
    )
    bad_project, bad_item = create_project_item(
        client,
        code="TASK4_QUARANTINE_BAD",
    )
    with SessionLocal() as observer:
        safe_version = observer.get(
            ReviewVersionModel,
            safe_item["current_version_id"],
        )
        bad_version = observer.get(
            ReviewVersionModel,
            bad_item["current_version_id"],
        )
        assert safe_version is not None and bad_version is not None
        safe_target = (
            get_settings().storage_root / "files" / safe_version.original_file_id
        )
        bad_target = (
            get_settings().storage_root / "files" / bad_version.original_file_id
        )
    original_processor = _pause_postcommit_deletion_processor(monkeypatch)
    assert _delete_item(
        client,
        safe_project["project_ref_id"],
        safe_item,
    ).status_code == 200
    assert _delete_item(
        client,
        bad_project["project_ref_id"],
        bad_item,
    ).status_code == 200
    monkeypatch.setattr(
        maintenance_cleanup,
        "process_review_deletion_intents",
        original_processor,
    )

    quarantine_root = get_settings().storage_root / DELETE_QUARANTINE_DIRECTORY
    quarantine_root.mkdir(mode=0o700, exist_ok=True)
    matching_entry = quarantine_root / ("a" * 32)
    matching_entry.mkdir(mode=0o700)
    matching_target = matching_entry / DELETE_QUARANTINE_TARGET
    os.rename(bad_target, matching_target)
    (matching_entry / DELETE_QUARANTINE_MANIFEST).write_bytes(b"{")
    os.chmod(matching_entry / DELETE_QUARANTINE_MANIFEST, 0o600)

    unrelated_entry = quarantine_root / ("b" * 32)
    unrelated_entry.mkdir(mode=0o700)
    (unrelated_entry / DELETE_QUARANTINE_MANIFEST).write_bytes(b"not-json")
    os.chmod(unrelated_entry / DELETE_QUARANTINE_MANIFEST, 0o600)
    (unrelated_entry / DELETE_QUARANTINE_TARGET).write_bytes(b"unrelated")
    unrelated_fd = os.open(
        unrelated_entry,
        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    fcntl.flock(unrelated_fd, fcntl.LOCK_EX)
    try:
        completed, failed = original_processor(get_settings())
    finally:
        fcntl.flock(unrelated_fd, fcntl.LOCK_UN)
        os.close(unrelated_fd)

    assert (completed, failed) == (1, 1)
    assert not safe_target.exists()
    assert matching_target.is_file()
    with SessionLocal() as observer:
        safe_intent = observer.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.review_item_id_snapshot
                == safe_item["id"],
            ),
        )
        bad_intent = observer.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.review_item_id_snapshot
                == bad_item["id"],
            ),
        )
        assert safe_intent is not None and safe_intent.status == "complete"
        assert bad_intent is not None and bad_intent.status == "failed"

    assert original_processor(get_settings()) == (0, 1)
    assert matching_target.is_file()
