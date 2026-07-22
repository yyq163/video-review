from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command as alembic_command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.modules.final_cut_review.application.context import ExecutionContext, PrincipalRef, WriteGuardState
from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.final_cut_review.infra.database import Base
from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
    FileObjectModel,
    FinalizationRecordModel,
    IdempotencyRecordModel,
    OutboxEventModel,
    ProjectRefModel,
    ReviewIssueModel,
    ReviewIssueRevisionModel,
    ReviewItemModel,
    ReviewVersionModel,
    UploadSessionModel,
    utcnow,
)
from backend.app.settings import get_settings


def _suffix() -> str:
    return uuid.uuid4().hex[:12]


def _postgres_database_url() -> str:
    if os.environ.get("RUN_POSTGRES_CONSTRAINT_TESTS") != "1":
        pytest.skip("BLOCKED_TEST_ENV: RUN_POSTGRES_CONSTRAINT_TESTS=1 is required for PostgreSQL constraint tests")
    database_url = _required_postgresql_url("DATABASE_URL")
    runtime = make_url(database_url)
    owner = make_url(_postgres_owner_url(database_url))
    admin = make_url(_postgres_admin_url())
    expected_test_database = _safe_identifier(os.environ.get("POSTGRES_TEST_DB", ""))
    application_database = _safe_identifier(os.environ.get("POSTGRES_APP_DB", ""))
    if len({expected_test_database, application_database, admin.database}) != 3:
        pytest.fail("PostgreSQL hard gate requires distinct administration, application, and test databases")
    if runtime.database != expected_test_database or owner.database != expected_test_database:
        pytest.fail("DATABASE_URL and POSTGRES_OWNER_DATABASE_URL must target POSTGRES_TEST_DB")
    if len({runtime.username, owner.username, admin.username}) != 3:
        pytest.fail("PostgreSQL hard gate requires distinct runtime, owner, and administration roles")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            role_flags = connection.execute(
                text("SELECT rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            ).one()
            database_identity = connection.execute(text("SELECT current_database(), current_setting('server_version_num')::integer")).one()
            privileges = connection.execute(
                text(
                    "SELECT "
                    "has_database_privilege(current_user, current_database(), 'CONNECT'), "
                    "has_database_privilege(current_user, current_database(), 'CREATE'), "
                    "has_database_privilege(current_user, current_database(), 'TEMP'), "
                    "has_schema_privilege(current_user, 'public', 'USAGE'), "
                    "has_schema_privilege(current_user, 'public', 'CREATE')"
                )
            ).one()
            owned_relations = connection.execute(
                text(
                    "SELECT count(*) FROM pg_class "
                    "WHERE relnamespace = 'public'::regnamespace "
                    "AND relkind IN ('r','p','S','v','m','f') "
                    "AND pg_get_userbyid(relowner) = current_user"
                )
            ).scalar_one()
    finally:
        engine.dispose()
    assert tuple(role_flags) == (False, False, False, False, False, False)
    assert database_identity[0] == expected_test_database
    assert 160000 <= database_identity[1] < 170000
    assert tuple(privileges) == (True, False, False, True, False)
    assert owned_relations == 0
    return database_url


def _required_postgresql_url(name: str) -> str:
    value = os.environ.get(name, "")
    try:
        parsed = make_url(value)
    except Exception:
        pytest.fail(f"{name} must be a valid PostgreSQL URL")
    if not parsed.drivername.startswith("postgresql") or not parsed.username or not parsed.password or not parsed.database:
        pytest.fail(f"{name} must include PostgreSQL user, password, and database")
    return value


def _postgres_owner_url(database_url: str) -> str:
    owner_url = make_url(_required_postgresql_url("POSTGRES_OWNER_DATABASE_URL"))
    target_database = make_url(database_url).database
    return owner_url.set(database=target_database).render_as_string(hide_password=False)


def _prepare_postgresql_schema(database_url: str, revision: str = "head") -> None:
    previous_database_url = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = _postgres_owner_url(database_url)
        get_settings.cache_clear()
        root = Path(__file__).resolve().parents[2]
        cfg = Config(str(root / "backend/alembic.ini"))
        cfg.set_main_option("script_location", str(root / "backend/alembic"))
        alembic_command.upgrade(cfg, revision)
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        get_settings.cache_clear()


def _safe_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise ValueError("PostgreSQL identifier contains unsafe characters")
    return value


def _postgres_admin_url() -> str:
    return _required_postgresql_url("POSTGRES_ADMIN_DATABASE_URL")


@dataclass(frozen=True)
class _TemporaryPostgresDatabase:
    name: str
    url: str
    owner: str
    sentinel: str


def _postgres_temp_database(database_url: str) -> _TemporaryPostgresDatabase:
    owner_url = make_url(_postgres_owner_url(database_url))
    db_name = f"fcr_{uuid.uuid4().hex[:16]}"
    owner = _safe_identifier(owner_url.username or "")
    sentinel = f"fcr-test-database:{secrets.token_hex(32)}"
    owner_clause = f' OWNER "{owner}"'
    engine = create_engine(_postgres_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{_safe_identifier(db_name)}"{owner_clause}'))
            connection.execute(text(f'COMMENT ON DATABASE "{db_name}" IS \'{sentinel}\''))
    finally:
        engine.dispose()
    return _TemporaryPostgresDatabase(
        name=db_name,
        url=owner_url.set(database=db_name).render_as_string(hide_password=False),
        owner=owner,
        sentinel=sentinel,
    )


def _drop_postgres_temp_database(database: _TemporaryPostgresDatabase) -> None:
    db_name = _safe_identifier(database.name)
    expected_owner = _safe_identifier(database.owner)
    engine = create_engine(_postgres_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            identity = connection.execute(
                text(
                    "SELECT datname, pg_get_userbyid(datdba), "
                    "shobj_description(oid, 'pg_database') "
                    "FROM pg_database WHERE datname = :db_name"
                ),
                {"db_name": db_name},
            ).one_or_none()
            if identity is None or tuple(identity) != (db_name, expected_owner, database.sentinel):
                pytest.fail("refusing to drop temporary PostgreSQL database: identity verification failed")
            connection.execute(
                text(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = :db_name AND pid <> pg_backend_pid()
                    """
                ).bindparams(db_name=db_name)
            )
            connection.execute(text(f'DROP DATABASE "{db_name}"'))
    finally:
        engine.dispose()


def test_temporary_database_cleanup_fails_closed_before_drop_on_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _TemporaryPostgresDatabase(
        name="fcr_test_database",
        url="unused",
        owner="fcr_test_owner",
        sentinel="expected-sentinel",
    )

    class FakeResult:
        @staticmethod
        def one_or_none() -> tuple[str, str, str]:
            return (database.name, database.owner, "wrong-sentinel")

    class FakeConnection:
        def __init__(self) -> None:
            self.statements: list[object] = []

        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: object, _parameters: object = None) -> FakeResult:
            self.statements.append(statement)
            return FakeResult()

    class FakeEngine:
        def __init__(self) -> None:
            self.connection = FakeConnection()

        def connect(self) -> FakeConnection:
            return self.connection

        def dispose(self) -> None:
            return None

    engine = FakeEngine()
    monkeypatch.setattr("backend.tests.test_postgresql_constraints._postgres_admin_url", lambda: "postgresql://test.invalid/postgres")
    monkeypatch.setattr("backend.tests.test_postgresql_constraints.create_engine", lambda *_args, **_kwargs: engine)

    with pytest.raises(pytest.fail.Exception, match="identity verification failed"):
        _drop_postgres_temp_database(database)

    assert len(engine.connection.statements) == 1


def _metadata_diffs(database_url: str) -> list[object]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            migration_context = MigrationContext.configure(connection)
            return compare_metadata(migration_context, Base.metadata)
    finally:
        engine.dispose()


@pytest.mark.parametrize("command_type", ["CreateReviewItem", "FinalizeVersion"])
def test_concurrent_identical_idempotency_reservations_replay_winner(command_type: str) -> None:
    database_url = _postgres_database_url()
    key = f"idem_{command_type}_{uuid.uuid4().hex}"
    request_hash = hashlib.sha256(f"{command_type}:same-request".encode()).hexdigest()
    principal_hash = hashlib.sha256(b"same-principal").hexdigest()
    expected = {"operation_id": f"result_{uuid.uuid4().hex}"}
    barrier = threading.Barrier(2)
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    engine = create_engine(database_url, pool_size=2, max_overflow=0)

    def run() -> None:
        try:
            with Session(engine) as session:
                repo = SqlAlchemyReviewRepository(session, get_settings())
                barrier.wait(timeout=5)
                replay = repo.reserve_idempotency(key, command_type, request_hash, principal_hash)
                if replay is None:
                    time.sleep(0.2)
                    repo.complete_idempotency(key, expected)
                    session.commit()
                    replay = expected
                with lock:
                    results.append(cast(dict[str, object], replay))
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert results == [expected, expected]
    finally:
        with Session(engine) as session:
            record = session.get(IdempotencyRecordModel, key)
            if record:
                session.delete(record)
                session.commit()
        engine.dispose()


def _metadata_foreign_key_inventory() -> set[tuple[str, tuple[str, ...], str, tuple[str, ...], str]]:
    inventory: set[tuple[str, tuple[str, ...], str, tuple[str, ...], str]] = set()
    for table in Base.metadata.tables.values():
        for constraint in table.foreign_key_constraints:
            inventory.add(
                (
                    table.name,
                    tuple(column.name for column in constraint.columns),
                    constraint.referred_table.name,
                    tuple(element.column.name for element in constraint.elements),
                    (constraint.ondelete or "").upper(),
                )
            )
    return inventory


def _database_foreign_key_inventory(database_url: str) -> set[tuple[str, tuple[str, ...], str, tuple[str, ...], str]]:
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        return {
            (
                table_name,
                tuple(foreign_key["constrained_columns"]),
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
                str(foreign_key.get("options", {}).get("ondelete") or "").upper(),
            )
            for table_name in inspector.get_table_names()
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
    finally:
        engine.dispose()


def _assert_foreign_keys_match_metadata(database_url: str) -> None:
    assert _database_foreign_key_inventory(database_url) == _metadata_foreign_key_inventory()


def _assert_no_legacy_server_defaults(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        defaults = {
            table: {
                column["name"]: column.get("default")
                for column in inspector.get_columns(table)
                if column["name"] in {"owner_principal_id", "owner_principal_kind", "principal_hash"}
            }
            for table in ("file_objects", "upload_sessions", "idempotency_records")
        }
    finally:
        engine.dispose()
    assert defaults == {
        "file_objects": {"owner_principal_id": None, "owner_principal_kind": None},
        "upload_sessions": {"owner_principal_id": None, "owner_principal_kind": None},
        "idempotency_records": {"principal_hash": None},
    }


def test_postgresql_owner_defaults_match_metadata_after_previous_head_upgrade_and_fresh_install() -> None:
    database_url = _postgres_database_url()
    created: list[_TemporaryPostgresDatabase] = []
    try:
        previous = _postgres_temp_database(database_url)
        created.append(previous)
        _prepare_postgresql_schema(previous.url, "20260621_0003")
        _prepare_postgresql_schema(previous.url, "head")
        assert _metadata_diffs(previous.url) == []
        _assert_foreign_keys_match_metadata(previous.url)
        _assert_no_legacy_server_defaults(previous.url)

        fresh = _postgres_temp_database(database_url)
        created.append(fresh)
        _prepare_postgresql_schema(fresh.url, "head")
        assert _metadata_diffs(fresh.url) == []
        _assert_foreign_keys_match_metadata(fresh.url)
        _assert_no_legacy_server_defaults(fresh.url)
    finally:
        os.environ["DATABASE_URL"] = database_url
        get_settings.cache_clear()
        for database in reversed(created):
            _drop_postgres_temp_database(database)


def test_postgresql_reconciles_uncommitted_0017_and_0018_draft_shapes() -> None:
    database_url = _postgres_database_url()
    created: list[_TemporaryPostgresDatabase] = []
    try:
        missing_fields = _postgres_temp_database(database_url)
        created.append(missing_fields)
        _prepare_postgresql_schema(missing_fields.url, "20260714_0017")
        engine = create_engine(missing_fields.url)
        try:
            with engine.begin() as connection:
                connection.execute(text("DROP INDEX IF EXISTS uq_operation_logs_unknown_identity"))
                connection.execute(text("DROP INDEX IF EXISTS ix_package_snapshots_build_lease"))
                connection.execute(
                    text(
                        "ALTER TABLE operation_logs "
                        "DROP CONSTRAINT IF EXISTS ck_operation_logs_operation_identity_hash, "
                        "DROP COLUMN IF EXISTS operation_identity_hash"
                    )
                )
                connection.execute(
                    text(
                        "ALTER TABLE package_snapshots "
                        "DROP CONSTRAINT IF EXISTS ck_package_snapshots_build_lease, "
                        "DROP COLUMN IF EXISTS build_lease_id, "
                        "DROP COLUMN IF EXISTS build_lease_expires_at"
                    )
                )
        finally:
            engine.dispose()
        _prepare_postgresql_schema(missing_fields.url, "head")
        assert _metadata_diffs(missing_fields.url) == []

        wrong_definitions = _postgres_temp_database(database_url)
        created.append(wrong_definitions)
        _prepare_postgresql_schema(wrong_definitions.url, "20260714_0017")
        engine = create_engine(wrong_definitions.url)
        try:
            with engine.begin() as connection:
                connection.execute(text("DROP INDEX uq_operation_logs_unknown_identity"))
                connection.execute(text("DROP INDEX ix_package_snapshots_build_lease"))
                connection.execute(
                    text(
                        "ALTER TABLE operation_logs "
                        "DROP CONSTRAINT ck_operation_logs_operation_identity_hash, "
                        "ADD CONSTRAINT ck_operation_logs_operation_identity_hash "
                        "CHECK (operation_identity_hash IS NULL)"
                    )
                )
                connection.execute(
                    text(
                        "ALTER TABLE package_snapshots "
                        "DROP CONSTRAINT ck_package_snapshots_build_lease, "
                        "ADD CONSTRAINT ck_package_snapshots_build_lease "
                        "CHECK (build_lease_id IS NULL)"
                    )
                )
                connection.execute(
                    text("CREATE INDEX ix_package_snapshots_build_lease ON package_snapshots (status)")
                )
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX uq_operation_logs_unknown_identity "
                        "ON operation_logs (operation_identity_hash) "
                        "WHERE result = 'ok' AND operation_identity_hash IS NOT NULL"
                    )
                )
                connection.execute(
                    text("UPDATE alembic_version SET version_num = '20260714_0018'")
                )
        finally:
            engine.dispose()
        _prepare_postgresql_schema(wrong_definitions.url, "head")
        assert _metadata_diffs(wrong_definitions.url) == []
    finally:
        os.environ["DATABASE_URL"] = database_url
        get_settings.cache_clear()
        for database in reversed(created):
            _drop_postgres_temp_database(database)


def test_postgresql_operation_log_attribution_upgrade_preserves_legacy_writes_and_downgrades() -> None:
    database_url = _postgres_database_url()
    database = _postgres_temp_database(database_url)
    legacy_request_id = f"legacy_{uuid.uuid4().hex}"
    legacy_writer_request_id = f"legacy_writer_{uuid.uuid4().hex}"
    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "backend/alembic.ini"))
    cfg.set_main_option("script_location", str(root / "backend/alembic"))
    try:
        _prepare_postgresql_schema(database.url, "20260714_0014")
        engine = create_engine(database.url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO operation_logs (
                            request_id, entry_source, capability, result, error_code, created_at
                        ) VALUES (
                            :request_id, 'edit', 'review.project.update', 'ok', NULL, now()
                        )
                        """
                    ),
                    {"request_id": legacy_request_id},
                )
        finally:
            engine.dispose()

        _prepare_postgresql_schema(database.url, "20260714_0015")
        engine = create_engine(database.url)
        try:
            columns = {column["name"]: column for column in inspect(engine).get_columns("operation_logs")}
            assert columns["command_type"]["default"] is None
            assert columns["principal_kind"]["default"] is None
            assert columns["resource_type"]["default"] is None
        finally:
            engine.dispose()

        _prepare_postgresql_schema(database.url, "head")
        engine = create_engine(database.url)
        try:
            inspector = inspect(engine)
            columns = {column["name"]: column for column in inspector.get_columns("operation_logs")}
            assert {
                "command_type",
                "principal_kind",
                "principal_id",
                "client_ip",
                "user_agent",
                "idempotency_key_hash",
                "resource_type",
                "resource_id",
                "failure_stage",
            } <= columns.keys()
            assert columns["command_type"]["nullable"] is False
            assert columns["principal_kind"]["nullable"] is False
            assert columns["resource_type"]["nullable"] is False
            assert "LegacyOperation" in str(columns["command_type"]["default"])
            assert "anonymous" in str(columns["principal_kind"]["default"])
            assert "request" in str(columns["resource_type"]["default"])
            assert "ix_operation_logs_command_type" in {
                index["name"] for index in inspector.get_indexes("operation_logs")
            }
            assert "ck_operation_logs_idempotency_hash" in {
                constraint["name"] for constraint in inspector.get_check_constraints("operation_logs")
            }

            with engine.begin() as connection:
                migrated = connection.execute(
                    text(
                        """
                        SELECT request_id, command_type, principal_kind, principal_id,
                               client_ip, user_agent, idempotency_key_hash,
                               resource_type, resource_id, failure_stage
                        FROM operation_logs
                        WHERE request_id = :request_id
                        """
                    ),
                    {"request_id": legacy_request_id},
                ).one()
                connection.execute(
                    text(
                        """
                        INSERT INTO operation_logs (
                            request_id, entry_source, capability, result, error_code, created_at
                        ) VALUES (
                            :request_id, 'embedded', NULL, 'ok', NULL, now()
                        )
                        """
                    ),
                    {"request_id": legacy_writer_request_id},
                )
                legacy_writer = connection.execute(
                    text(
                        """
                        SELECT request_id, command_type, principal_kind, resource_type
                        FROM operation_logs
                        WHERE request_id = :request_id
                        """
                    ),
                    {"request_id": legacy_writer_request_id},
                ).one()
            assert tuple(migrated) == (
                legacy_request_id,
                "LegacyOperation",
                "anonymous",
                None,
                None,
                None,
                None,
                "request",
                None,
                None,
            )
            assert tuple(legacy_writer) == (
                legacy_writer_request_id,
                "LegacyOperation",
                "anonymous",
                "request",
            )
        finally:
            engine.dispose()

        previous_database_url = os.environ.get("DATABASE_URL")
        try:
            os.environ["DATABASE_URL"] = _postgres_owner_url(database.url)
            get_settings.cache_clear()
            alembic_command.downgrade(cfg, "20260714_0014")
        finally:
            if previous_database_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous_database_url
            get_settings.cache_clear()

        engine = create_engine(database.url)
        try:
            inspector = inspect(engine)
            downgraded_columns = {column["name"] for column in inspector.get_columns("operation_logs")}
            assert {"command_type", "principal_kind", "resource_type"}.isdisjoint(downgraded_columns)
            with engine.connect() as connection:
                preserved_rows = connection.execute(
                    text(
                        """
                        SELECT request_id, entry_source, capability, result
                        FROM operation_logs
                        WHERE request_id IN (:legacy_request_id, :legacy_writer_request_id)
                        ORDER BY request_id
                        """
                    ),
                    {
                        "legacy_request_id": legacy_request_id,
                        "legacy_writer_request_id": legacy_writer_request_id,
                    },
                ).all()
            assert [tuple(row) for row in preserved_rows] == sorted(
                [
                    (legacy_request_id, "edit", "review.project.update", "ok"),
                    (legacy_writer_request_id, "embedded", None, "ok"),
                ]
            )
        finally:
            engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = database_url
        get_settings.cache_clear()
        _drop_postgres_temp_database(database)


def test_postgresql_package_digest_migration_up_down_and_reupgrade_with_legacy_ready_row() -> None:
    database_url = _postgres_database_url()
    database = _postgres_temp_database(database_url)
    migration_url = database.url
    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "backend/alembic.ini"))
    cfg.set_main_option("script_location", str(root / "backend/alembic"))
    project_id = f"prj_{uuid.uuid4().hex}"
    package_id = f"pkg_{uuid.uuid4().hex}"
    preparing_package_id = f"pkg_{uuid.uuid4().hex}"
    ready_backfill_package_id = f"pkg_{uuid.uuid4().hex}"
    ready_reserved_package_id = f"pkg_{uuid.uuid4().hex}"
    failed_package_id = f"pkg_{uuid.uuid4().hex}"
    try:
        _prepare_postgresql_schema(migration_url, "20260709_0006")
        engine = create_engine(migration_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO project_refs (
                            id, project_code, project_name, source, lifecycle_status,
                            lock_version, created_at, updated_at
                        ) VALUES (
                            :project_id, :project_code, 'Package migration project',
                            'local', 'active', 1, now(), now()
                        )
                        """
                    ),
                    {"project_id": project_id, "project_code": f"MIG_{uuid.uuid4().hex[:12]}"},
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO package_snapshots (
                            id, project_ref_id, status, package_filename, storage_path,
                            expires_at, total_bytes, failure_details, items, created_at, updated_at
                        ) VALUES (
                            :package_id, :project_id, 'ready', :filename, :storage_path,
                            now() + interval '1 day', 12, NULL, '[]', now(), now()
                        )
                        """
                    ),
                    {
                        "package_id": package_id,
                        "project_id": project_id,
                        "filename": f"{package_id}.zip",
                        "storage_path": f"/data/packages/{package_id}.zip",
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO package_snapshots (
                            id, project_ref_id, status, package_filename, storage_path,
                            expires_at, total_bytes, failure_details, items, created_at, updated_at
                        ) VALUES (
                            :package_id, :project_id, 'preparing', :filename, :storage_path,
                            now() + interval '1 day', 12, NULL,
                            '[{"archive_name":"legacy.mp4"}]', now(), now()
                        )
                        """
                    ),
                    {
                        "package_id": preparing_package_id,
                        "project_id": project_id,
                        "filename": f"{preparing_package_id}.zip",
                        "storage_path": f"/data/packages/{preparing_package_id}.zip",
                    },
                )
        finally:
            engine.dispose()

        _prepare_postgresql_schema(migration_url, "20260713_0009")
        engine = create_engine(migration_url)
        try:
            with engine.begin() as connection:
                for ready_id, storage_bytes in (
                    (ready_backfill_package_id, 12),
                    (ready_reserved_package_id, 2_000_000),
                ):
                    connection.execute(
                        text(
                            """
                            INSERT INTO package_snapshots (
                                id, project_ref_id, status, package_filename, storage_path,
                                expires_at, total_bytes, storage_bytes, sha256, build_attempts,
                                failure_details, items, created_at, updated_at
                            ) VALUES (
                                :package_id, :project_id, 'ready', :filename, :storage_path,
                                now() + interval '1 day', 12, :storage_bytes, :sha256, 1,
                                NULL, '[]', now(), now()
                            )
                            """
                        ),
                        {
                            "package_id": ready_id,
                            "project_id": project_id,
                            "filename": f"{ready_id}.zip",
                            "storage_path": f"/data/packages/{ready_id}.zip",
                            "storage_bytes": storage_bytes,
                            "sha256": "a" * 64,
                        },
                    )
                connection.execute(
                    text(
                        """
                        INSERT INTO package_snapshots (
                            id, project_ref_id, status, package_filename, storage_path,
                            expires_at, total_bytes, storage_bytes, build_attempts,
                            failure_details, items, created_at, updated_at
                        ) VALUES (
                            :package_id, :project_id, 'failed', :filename, :storage_path,
                            now() + interval '1 day', 12, 0, 3,
                            '{"error_code":"PACKAGE_BUILD_INTERRUPTED"}', '[]', now(), now()
                        )
                        """
                    ),
                    {
                        "package_id": failed_package_id,
                        "project_id": project_id,
                        "filename": f"{failed_package_id}.zip",
                        "storage_path": f"/data/packages/{failed_package_id}.zip",
                    },
                )
        finally:
            engine.dispose()
        _prepare_postgresql_schema(migration_url, "head")
        engine = create_engine(migration_url)
        try:
            inspector = inspect(engine)
            package_columns = {column["name"] for column in inspector.get_columns("package_snapshots")}
            assert {
                "sha256",
                "download_session_hash",
                "download_session_expires_at",
                "download_lease_id",
                "download_lease_expires_at",
                "last_download_finished_at",
                "storage_reclaimed_at",
                "storage_bytes",
                "build_attempts",
                "next_build_attempt_at",
            } <= package_columns
            upload_columns = {column["name"] for column in inspector.get_columns("upload_sessions")}
            assert {
                "finalization_idempotency_key_hash",
                "finalization_request_hash",
            } <= upload_columns
            upload_check_names = {constraint["name"] for constraint in inspector.get_check_constraints("upload_sessions")}
            assert "ck_upload_sessions_finalization_lease" in upload_check_names
            check_names = {constraint["name"] for constraint in inspector.get_check_constraints("package_snapshots")}
            assert {
                "ck_package_snapshots_sha256",
                "ck_package_snapshots_ready_sha256",
                "ck_package_snapshots_download_session_pair",
                "ck_package_snapshots_download_lease_pair",
                "ck_package_snapshots_storage_bytes",
                "ck_package_snapshots_build_attempts",
            } <= check_names
            index_names = {index["name"] for index in inspector.get_indexes("package_snapshots")}
            assert {
                "uq_package_snapshots_preparing_project",
                "ix_package_snapshots_worker_queue",
                "ix_package_snapshots_cleanup",
                "ix_package_snapshots_quota",
            } <= index_names
            assert "ix_upload_sessions_cleanup" in {index["name"] for index in inspector.get_indexes("upload_sessions")}
            with engine.connect() as connection:
                migrated = connection.execute(
                    text(
                        "SELECT status, sha256, expires_at <= now() AS expired_now, "
                        "storage_reclaimed_at, storage_bytes "
                        "FROM package_snapshots WHERE id = :package_id"
                    ),
                    {"package_id": package_id},
                ).one()
                preparing_storage_bytes = connection.scalar(
                    text("SELECT storage_bytes FROM package_snapshots WHERE id = :package_id"),
                    {"package_id": preparing_package_id},
                )
                ready_storage_bytes = {
                    row.id: row.storage_bytes
                    for row in connection.execute(
                        text("SELECT id, storage_bytes FROM package_snapshots WHERE id IN (:backfill_id, :reserved_id)"),
                        {
                            "backfill_id": ready_backfill_package_id,
                            "reserved_id": ready_reserved_package_id,
                        },
                    )
                }
                failed_storage = connection.execute(
                    text("SELECT storage_bytes, storage_reclaimed_at FROM package_snapshots WHERE id = :package_id"),
                    {"package_id": failed_package_id},
                ).one()
            assert tuple(migrated) == (
                "expired",
                None,
                True,
                None,
                12 + 1_048_576,
            )
            assert preparing_storage_bytes == 12 + 1_048_576
            assert ready_storage_bytes == {
                ready_backfill_package_id: 12 + 1_048_576,
                ready_reserved_package_id: 2_000_000,
            }
            assert tuple(failed_storage) == (12 + 1_048_576, None)
        finally:
            engine.dispose()
        os.environ["DATABASE_URL"] = migration_url
        get_settings.cache_clear()
        alembic_command.downgrade(cfg, "20260709_0006")
        engine = create_engine(migration_url)
        try:
            downgraded_columns = {column["name"] for column in inspect(engine).get_columns("package_snapshots")}
            assert "sha256" not in downgraded_columns
            assert "download_session_hash" not in downgraded_columns
            assert "storage_bytes" not in downgraded_columns
            downgraded_upload_columns = {column["name"] for column in inspect(engine).get_columns("upload_sessions")}
            assert "finalization_idempotency_key_hash" not in downgraded_upload_columns
            assert "finalization_request_hash" not in downgraded_upload_columns
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        text("SELECT status FROM package_snapshots WHERE id = :package_id"),
                        {"package_id": package_id},
                    )
                    == "expired"
                )
        finally:
            engine.dispose()

        _prepare_postgresql_schema(migration_url, "head")
        assert _metadata_diffs(migration_url) == []
        _assert_foreign_keys_match_metadata(migration_url)
    finally:
        os.environ["DATABASE_URL"] = database_url
        get_settings.cache_clear()
        _drop_postgres_temp_database(database)


def test_postgresql_package_worker_advisory_lock_survives_session_commit_and_releases() -> None:
    database_url = _postgres_database_url()
    from backend.app.package_builds import PACKAGE_BUILD_ADVISORY_LOCK_KEY, _package_build_lock

    test_engine = create_engine(database_url, pool_pre_ping=True)
    test_session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    try:
        with test_session() as session:
            with _package_build_lock(session) as acquired:
                assert acquired is True
                session.execute(text("SELECT 1"))
                session.commit()
                with test_engine.connect() as competitor:
                    assert (
                        competitor.scalar(
                            text("SELECT pg_try_advisory_lock(:lock_key)"),
                            {"lock_key": PACKAGE_BUILD_ADVISORY_LOCK_KEY},
                        )
                        is False
                    )
                    competitor.rollback()
        with test_engine.connect() as competitor:
            assert (
                competitor.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": PACKAGE_BUILD_ADVISORY_LOCK_KEY},
                )
                is True
            )
            assert (
                competitor.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": PACKAGE_BUILD_ADVISORY_LOCK_KEY},
                )
                is True
            )
            competitor.commit()
    finally:
        test_engine.dispose()


def test_postgresql_upload_quota_migration_conservatively_reserves_legacy_terminal_rows() -> None:
    database_url = _postgres_database_url()
    database = _postgres_temp_database(database_url)
    migration_url = database.url
    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "backend/alembic.ini"))
    cfg.set_main_option("script_location", str(root / "backend/alembic"))
    upload_id = f"upl_{uuid.uuid4().hex}"
    try:
        _prepare_postgresql_schema(migration_url, "20260713_0011")
        engine = create_engine(migration_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO upload_sessions (
                            id, original_filename, mime_type, declared_size, expected_sha256,
                            status, temp_path, owner_principal_id, owner_principal_kind,
                            received_parts, file_id, duration_ms, width, height, fps_num, fps_den
                        ) VALUES (
                            :upload_id, 'legacy.mp4', 'video/mp4', 321, :sha256,
                            'aborted', :temp_path, 'legacy-owner', 'system',
                            CAST('{}' AS json), NULL, 1000, 1920, 1080, 25, 1
                        )
                        """
                    ),
                    {
                        "upload_id": upload_id,
                        "sha256": "a" * 64,
                        "temp_path": f"/data/storage/uploads/{upload_id}.parts",
                    },
                )
        finally:
            engine.dispose()

        _prepare_postgresql_schema(migration_url, "head")
        engine = create_engine(migration_url)
        try:
            with engine.connect() as connection:
                reservation = connection.execute(
                    text("SELECT reserved_bytes, parts_cleanup_confirmed_at FROM upload_sessions WHERE id = :upload_id"),
                    {"upload_id": upload_id},
                ).one()
            assert tuple(reservation) == (642, None)
        finally:
            engine.dispose()

        os.environ["DATABASE_URL"] = migration_url
        get_settings.cache_clear()
        alembic_command.downgrade(cfg, "20260713_0011")
        _prepare_postgresql_schema(migration_url, "head")
        engine = create_engine(migration_url)
        try:
            with engine.connect() as connection:
                reservation = connection.execute(
                    text("SELECT reserved_bytes, parts_cleanup_confirmed_at FROM upload_sessions WHERE id = :upload_id"),
                    {"upload_id": upload_id},
                ).one()
            assert tuple(reservation) == (642, None)
        finally:
            engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = database_url
        get_settings.cache_clear()
        _drop_postgres_temp_database(database)


def test_postgresql_upload_identity_migration_resets_legacy_finalizing_and_backfills_package_reservation() -> None:
    database_url = _postgres_database_url()
    database = _postgres_temp_database(database_url)
    migration_url = database.url
    upload_id = f"upl_{uuid.uuid4().hex}"
    project_id = f"prj_{uuid.uuid4().hex}"
    package_id = f"pkg_{uuid.uuid4().hex}"
    declared_size = 321
    package_total_bytes = 987
    try:
        _prepare_postgresql_schema(migration_url, "20260714_0012")
        engine = create_engine(migration_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO upload_sessions (
                            id, original_filename, mime_type, declared_size, expected_sha256,
                            status, temp_path, owner_principal_id, owner_principal_kind,
                            received_parts, reserved_bytes, finalization_lease_id,
                            finalization_lease_expires_at, finalization_file_id, file_id,
                            duration_ms, width, height, fps_num, fps_den, created_at, updated_at
                        ) VALUES (
                            :upload_id, 'legacy-finalizing.mp4', 'video/mp4', :declared_size, :sha256,
                            'finalizing', :temp_path, 'legacy-owner', 'system',
                            CAST('{}' AS json), :reserved_bytes, :lease_id,
                            now() + interval '5 minutes', :finalization_file_id, NULL,
                            1000, 1920, 1080, 25, 1, now(), now()
                        )
                        """
                    ),
                    {
                        "upload_id": upload_id,
                        "declared_size": declared_size,
                        "sha256": "a" * 64,
                        "temp_path": f"/data/storage/uploads/{upload_id}.parts",
                        "reserved_bytes": declared_size * 2,
                        "lease_id": f"lease_{uuid.uuid4().hex}",
                        "finalization_file_id": f"file_{uuid.uuid4().hex}",
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO project_refs (
                            id, project_code, project_name, source, lifecycle_status,
                            lock_version, created_at, updated_at
                        ) VALUES (
                            :project_id, :project_code, '0013 migration project',
                            'local', 'active', 1, now(), now()
                        )
                        """
                    ),
                    {"project_id": project_id, "project_code": f"MIG_{uuid.uuid4().hex[:12]}"},
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO package_snapshots (
                            id, project_ref_id, status, package_filename, storage_path,
                            expires_at, total_bytes, storage_bytes, sha256, build_attempts,
                            next_build_attempt_at, download_session_hash,
                            download_session_expires_at, download_lease_id,
                            download_lease_expires_at, last_download_finished_at,
                            storage_reclaimed_at, failure_details, items, created_at, updated_at
                        ) VALUES (
                            :package_id, :project_id, 'expired', :filename, :storage_path,
                            now() - interval '1 day', :total_bytes, :total_bytes, NULL, 0,
                            NULL, NULL, NULL, NULL, NULL, NULL,
                            NULL, NULL, CAST('[]' AS json), now(), now()
                        )
                        """
                    ),
                    {
                        "package_id": package_id,
                        "project_id": project_id,
                        "filename": f"{package_id}.zip",
                        "storage_path": f"/data/packages/{package_id}.zip",
                        "total_bytes": package_total_bytes,
                    },
                )
        finally:
            engine.dispose()

        _prepare_postgresql_schema(migration_url, "20260714_0013")
        engine = create_engine(migration_url)
        try:
            with engine.connect() as connection:
                upload = connection.execute(
                    text(
                        "SELECT status, reserved_bytes, finalization_lease_id, "
                        "finalization_lease_expires_at, finalization_file_id, "
                        "finalization_idempotency_key_hash, finalization_request_hash "
                        "FROM upload_sessions WHERE id = :upload_id"
                    ),
                    {"upload_id": upload_id},
                ).one()
                storage_bytes = connection.scalar(
                    text("SELECT storage_bytes FROM package_snapshots WHERE id = :package_id"),
                    {"package_id": package_id},
                )
            assert tuple(upload) == (
                "receiving",
                declared_size * 2,
                None,
                None,
                None,
                None,
                None,
            )
            assert storage_bytes == package_total_bytes + 1_048_576
        finally:
            engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = database_url
        get_settings.cache_clear()
        _drop_postgres_temp_database(database)


def test_postgresql_competing_publish_with_expired_lease_creates_one_file_object() -> None:
    from backend.app.modules.review_media import service as service_module

    database_url = _postgres_database_url()
    _prepare_postgresql_schema(database_url)
    engine = create_engine(database_url)
    PgSession = sessionmaker(bind=engine, expire_on_commit=False)
    settings = get_settings()
    file_root = settings.storage_root / "files"
    file_root.mkdir(parents=True, exist_ok=True)
    upload_id = f"upl_{uuid.uuid4().hex}"
    file_id = f"file_{uuid.uuid4().hex}"
    lease_id = f"lease_{uuid.uuid4().hex}"
    final_path = file_root / file_id
    blob = b"expired-lease-publish"
    final_path.write_bytes(blob)
    metadata = final_path.stat()
    context = ExecutionContext(
        entry_source="edit",
        request_id=f"req_{_suffix()}",
        principal=PrincipalRef(kind="system", id="lease-publisher", project_ref_ids=("*",)),
        write_guard=WriteGuardState(mode="none", verified=True),
    )
    claim = service_module.UploadFinalizationClaim(
        upload_id=upload_id,
        lease_id=lease_id,
        file_id=file_id,
        recovered_expired_lease=False,
        resumed_active_lease=False,
        original_filename="expired-lease.mp4",
        mime_type="video/mp4",
        declared_size=len(blob),
        expected_sha256=hashlib.sha256(blob).hexdigest(),
        owner_principal_id="lease-publisher",
        owner_principal_kind="system",
        part_paths=(),
    )
    finalized = service_module.FinalizedUploadFile(
        file_id=file_id,
        storage_path=final_path,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        sha256=hashlib.sha256(blob).hexdigest(),
        probe=service_module.MediaProbeResult(
            duration_ms=1000,
            width=1920,
            height=1080,
            fps_num=25,
            fps_den=1,
        ),
        part_paths=(),
    )
    with PgSession() as seed:
        seed.add(
            UploadSessionModel(
                id=upload_id,
                original_filename=claim.original_filename,
                mime_type=claim.mime_type,
                declared_size=claim.declared_size,
                expected_sha256=claim.expected_sha256,
                status="finalizing",
                temp_path=str(settings.storage_root / "uploads" / f"{upload_id}.parts"),
                owner_principal_id=claim.owner_principal_id,
                owner_principal_kind=claim.owner_principal_kind,
                received_parts={},
                finalization_lease_id=lease_id,
                finalization_lease_expires_at=utcnow() - timedelta(seconds=1),
                finalization_file_id=file_id,
                finalization_idempotency_key_hash="a" * 64,
                finalization_request_hash="b" * 64,
                duration_ms=1000,
                width=1920,
                height=1080,
                fps_num=25,
                fps_den=1,
            )
        )
        seed.commit()

    barrier = threading.Barrier(2)
    results: list[str] = []
    failures: list[BaseException] = []
    result_lock = threading.Lock()

    def publish() -> None:
        try:
            with PgSession() as publish_session:
                barrier.wait(timeout=5)
                prepared = service_module.LocalMediaService(
                    publish_session,
                    settings,
                    context,
                ).publish_completion(claim, finalized)
                publish_session.commit()
                result = str(prepared.response["file_id"])
        except BaseException as exc:
            with result_lock:
                failures.append(exc)
        else:
            with result_lock:
                results.append(result)

    first = threading.Thread(target=publish)
    second = threading.Thread(target=publish)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    try:
        assert not first.is_alive()
        assert not second.is_alive()
        assert failures == []
        assert results == [file_id, file_id]
        with PgSession() as observer:
            upload = observer.get(UploadSessionModel, upload_id)
            assert upload is not None and upload.status == "completed"
            assert upload.file_id == file_id
            assert observer.scalar(select(func.count()).select_from(FileObjectModel).where(FileObjectModel.id == file_id)) == 1
            observer.delete(upload)
            observer.flush()
            file = observer.get(FileObjectModel, file_id)
            assert file is not None
            observer.delete(file)
            observer.commit()
    finally:
        final_path.unlink(missing_ok=True)
        engine.dispose()


def test_postgresql_expired_finalization_takeover_serializes_identity_check() -> None:
    from backend.app.modules.review_media.service import LocalMediaService, UploadFinalizationClaim
    from backend.app.upload_parts import new_upload_part_path

    database_url = _postgres_database_url()
    _prepare_postgresql_schema(database_url)
    engine = create_engine(database_url)
    PgSession = sessionmaker(bind=engine, expire_on_commit=False)
    settings = get_settings()
    upload_id = f"upl_{uuid.uuid4().hex}"
    original_lease_id = f"lease_{uuid.uuid4().hex}"
    file_id = f"file_{uuid.uuid4().hex}"
    key_hash = "a" * 64
    request_hash = "b" * 64
    upload_root = settings.storage_root / "uploads"
    upload_root.mkdir(parents=True, exist_ok=True)
    temp_path = upload_root / f"{upload_id}.parts"
    part_path = new_upload_part_path(upload_root, upload_id, 1)
    part_path.write_bytes(b"serialized-takeover")
    context = ExecutionContext(
        entry_source="edit",
        request_id=f"req_{_suffix()}",
        principal=PrincipalRef(kind="system", id="lease-takeover", project_ref_ids=("*",)),
        write_guard=WriteGuardState(mode="none", verified=True),
    )
    with PgSession() as seed:
        seed.add(
            UploadSessionModel(
                id=upload_id,
                original_filename="serialized-takeover.mp4",
                mime_type="video/mp4",
                declared_size=part_path.stat().st_size,
                expected_sha256="0" * 64,
                status="finalizing",
                temp_path=str(temp_path),
                owner_principal_id=context.principal.id,
                owner_principal_kind=context.principal.kind,
                received_parts={"1": {"path": str(part_path), "size": part_path.stat().st_size}},
                finalization_lease_id=original_lease_id,
                finalization_lease_expires_at=utcnow() - timedelta(seconds=1),
                finalization_file_id=file_id,
                finalization_idempotency_key_hash=key_hash,
                finalization_request_hash=request_hash,
                duration_ms=1000,
                width=1920,
                height=1080,
                fps_num=25,
                fps_den=1,
            )
        )
        seed.commit()

    barrier = threading.Barrier(2)
    results: list[str] = []
    result_lock = threading.Lock()

    def takeover(candidate_key_hash: str, candidate_request_hash: str) -> None:
        with PgSession() as session:
            barrier.wait(timeout=5)
            try:
                claim = LocalMediaService(session, settings, context).claim_completion(
                    upload_id,
                    idempotency_key_hash=candidate_key_hash,
                    request_hash=candidate_request_hash,
                )
                assert isinstance(claim, UploadFinalizationClaim)
                assert claim.recovered_expired_lease is True
                session.commit()
                outcome = "taken"
            except ReviewError as exc:
                session.rollback()
                outcome = exc.code
        with result_lock:
            results.append(outcome)

    matching = threading.Thread(target=takeover, args=(key_hash, request_hash))
    mismatched = threading.Thread(target=takeover, args=("c" * 64, request_hash))
    matching.start()
    mismatched.start()
    matching.join(timeout=10)
    mismatched.join(timeout=10)
    try:
        assert not matching.is_alive()
        assert not mismatched.is_alive()
        assert sorted(results) == ["IDEMPOTENCY_CONFLICT", "taken"]
        with PgSession() as observer:
            upload = observer.get(UploadSessionModel, upload_id)
            assert upload is not None
            assert upload.status == "finalizing"
            assert upload.finalization_lease_id != original_lease_id
            assert upload.finalization_idempotency_key_hash == key_hash
            assert upload.finalization_request_hash == request_hash
            observer.delete(upload)
            observer.commit()
    finally:
        part_path.unlink(missing_ok=True)
        engine.dispose()


def _file(file_id: str) -> FileObjectModel:
    storage_dir = get_settings().storage_root.resolve() / "files"
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / file_id
    blob = f"postgres-delete-fixture-{file_id}".encode()
    storage_path.write_bytes(blob)
    return FileObjectModel(
        id=file_id,
        original_filename=f"{file_id}.mp4",
        mime_type="video/mp4",
        file_size=len(blob),
        sha256=hashlib.sha256(blob).hexdigest(),
        storage_path=str(storage_path),
        owner_principal_id="pg-test",
        owner_principal_kind="system",
        duration_ms=1000,
        width=1920,
        height=1080,
        fps_num=25,
        fps_den=1,
        media_probe_version="pg-test",
    )


def _version(version_id: str, project_id: str, item_id: str, file: FileObjectModel, version_no: int, is_current: bool) -> ReviewVersionModel:
    return ReviewVersionModel(
        id=version_id,
        project_ref_id=project_id,
        review_item_id=item_id,
        version_no=version_no,
        version_label=f"V{version_no}",
        is_current=is_current,
        original_file_id=file.id,
        original_filename=file.original_filename,
        mime_type=file.mime_type,
        file_size=file.file_size,
        sha256=file.sha256,
        duration_ms=file.duration_ms,
        width=file.width,
        height=file.height,
        fps_num=file.fps_num,
        fps_den=file.fps_den,
        media_probe_version=file.media_probe_version,
        playback_asset_id=file.id,
    )


def _seed_item(session: Session, prefix: str) -> tuple[ProjectRefModel, ReviewItemModel, ReviewVersionModel, FileObjectModel]:
    project = ProjectRefModel(id=f"prj_{prefix}", project_code=f"PG_{prefix}", project_name=f"PG {prefix}")
    file = _file(f"file_{uuid.uuid4().hex}")
    item = ReviewItemModel(id=f"item_{prefix}", project_ref_id=project.id, item_code=f"ITEM_{prefix}", title="PG item")
    session.add_all([project, file, item])
    session.flush()
    version = _version(f"ver_{prefix}_v1", project.id, item.id, file, 1, True)
    session.add(version)
    session.flush()
    item.current_version_id = version.id
    session.commit()
    return project, item, version, file


def test_postgresql_delete_review_item_detaches_outbox_and_removes_file() -> None:
    database_url = _postgres_database_url()
    _prepare_postgresql_schema(database_url)
    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        prefix = _suffix()
        project, item, version, file = _seed_item(session, prefix)
        storage_path = Path(file.storage_path)
        assert storage_path.exists()
        context = ExecutionContext(
            entry_source="edit",
            request_id=f"req_{prefix}",
            principal=PrincipalRef(kind="system", id="pg-test", project_ref_ids=(project.id,)),
            write_guard=WriteGuardState(mode="none", verified=True),
        )
        repository = SqlAlchemyReviewRepository(session, get_settings())
        deleted = repository.execute(
            "DeleteReviewItem",
            {"project_ref_id": project.id, "review_item_id": item.id, "confirmed": True},
            context,
            item.lock_version,
        )
        session.commit()
        repository.run_post_commit_file_deletions()

        assert deleted["id"] == item.id
        assert session.get(ReviewItemModel, item.id) is None
        assert session.get(ReviewVersionModel, version.id) is None
        assert session.get(FileObjectModel, file.id) is None
        assert not storage_path.exists()
        events = list(
            session.scalars(
                select(OutboxEventModel).where(
                    OutboxEventModel.project_ref_id == project.id,
                    OutboxEventModel.aggregate_id == item.id,
                    OutboxEventModel.event_type == "review.item.deleted",
                )
            )
        )
        assert len(events) == 1
        assert events[0].review_item_id is None
        assert events[0].version_id is None
        assert events[0].issue_id is None
        assert events[0].finalization_id is None
        assert events[0].correlation_id == context.request_id
    finally:
        session.close()
        engine.dispose()


def test_postgresql_pending_delete_cleanup_isolates_persistent_file_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.maintenance import _cleanup_pending_deletes

    database_url = _postgres_database_url()
    _prepare_postgresql_schema(database_url)
    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine)
    files_root = tmp_path / "files"
    pending_root = tmp_path / "pending-deletes"
    files_root.mkdir()
    pending_root.mkdir()
    blocked_file_id = f"file_{uuid.uuid4().hex}"
    removable_file_id = f"file_{uuid.uuid4().hex}"
    blocked_target = files_root / blocked_file_id
    removable_target = files_root / removable_file_id
    blocked_target.write_bytes(b"blocked")
    removable_target.write_bytes(b"removable")
    blocked_identity = blocked_target.stat()
    removable_identity = removable_target.stat()
    blocked_tombstone = pending_root / "blocked.json"
    removable_tombstone = pending_root / "removable.json"
    created_at = datetime.now(timezone.utc).isoformat()
    blocked_tombstone.write_text(
        json.dumps(
            {
                "file_id": blocked_file_id,
                "storage_path": str(blocked_target),
                "created_at": created_at,
                "device": blocked_identity.st_dev,
                "inode": blocked_identity.st_ino,
                "ctime_ns": blocked_identity.st_ctime_ns,
                "size": blocked_identity.st_size,
            }
        ),
        encoding="utf-8",
    )
    removable_tombstone.write_text(
        json.dumps(
            {
                "file_id": removable_file_id,
                "storage_path": str(removable_target),
                "created_at": created_at,
                "device": removable_identity.st_dev,
                "inode": removable_identity.st_ino,
                "ctime_ns": removable_identity.st_ctime_ns,
                "size": removable_identity.st_size,
            }
        ),
        encoding="utf-8",
    )
    original_rename = os.rename

    def persistent_failure(source: str | Path, target: str | Path, **kwargs: Any) -> None:
        if Path(source).name == blocked_target.name and kwargs.get("src_dir_fd") is not None:
            raise OSError("forced persistent unlink failure")
        return original_rename(source, target, **kwargs)

    monkeypatch.setattr(os, "rename", persistent_failure)
    try:
        with SessionLocal() as session:
            removed, failed = _cleanup_pending_deletes(session, tmp_path)
    finally:
        engine.dispose()

    assert (removed, failed) == (1, 1)
    assert blocked_target.exists()
    assert blocked_tombstone.exists()
    assert not removable_target.exists()
    assert not removable_tombstone.exists()


def test_postgresql_upload_write_lock_excludes_maintenance_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from backend.app.maintenance import _claim_terminal_or_stale_uploads
    from backend.app.modules.review_media.service import LocalMediaService
    from backend.app.settings import get_settings
    from backend.app.upload_parts import new_upload_part_path

    database_url = _postgres_database_url()
    _prepare_postgresql_schema(database_url)
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    get_settings.cache_clear()
    settings = get_settings()
    engine = create_engine(database_url)
    PgSession = sessionmaker(bind=engine, expire_on_commit=False)
    upload_id = f"upl_{uuid.uuid4().hex}"
    upload_root = settings.storage_root / "uploads"
    upload_root.mkdir(parents=True, exist_ok=True)
    temp_path = upload_root / f"{upload_id}.parts"
    with PgSession() as seed:
        seed.add(
            UploadSessionModel(
                id=upload_id,
                original_filename="concurrency.mp4",
                mime_type="video/mp4",
                declared_size=64,
                expected_sha256="0" * 64,
                status="initiated",
                temp_path=str(temp_path),
                owner_principal_id="pg-upload-lock",
                owner_principal_kind="system",
                received_parts={},
                duration_ms=1000,
                width=1920,
                height=1080,
                fps_num=25,
                fps_den=1,
                updated_at=utcnow() - timedelta(seconds=settings.upload_session_ttl_seconds + 1),
            )
        )
        seed.commit()

    context = ExecutionContext(
        entry_source="edit",
        request_id=f"req_{_suffix()}",
        principal=PrincipalRef(kind="system", id="pg-upload-lock", project_ref_ids=("*",)),
        write_guard=WriteGuardState(mode="none", verified=True),
    )
    staged_path = new_upload_part_path(upload_root, upload_id, 1)
    staged_path.write_bytes(b"x" * 16)
    statements: list[str] = []
    writer = PgSession()
    try:

        @event.listens_for(writer.connection(), "before_cursor_execute")
        def capture_writer_sql(_conn: object, _cursor: object, statement: str, _parameters: object, _context: object, _executemany: bool) -> None:
            statements.append(statement)

        service = LocalMediaService(writer, settings, context)
        prepared = service.put_part_file(upload_id, 1, staged_path, 16)
        assert any("FROM upload_sessions" in statement and "FOR UPDATE" in statement for statement in statements)
        writer.rollback()
        service.discard_uncommitted_part(prepared)
    finally:
        writer.close()

    locker = PgSession()
    claimer = PgSession()
    try:
        locked_upload = locker.scalar(select(UploadSessionModel).where(UploadSessionModel.id == upload_id).with_for_update())
        assert locked_upload is not None
        stale_before = utcnow() - timedelta(seconds=settings.upload_session_ttl_seconds)
        assert upload_id not in _claim_terminal_or_stale_uploads(claimer, stale_before)
        locker.rollback()
        assert upload_id in _claim_terminal_or_stale_uploads(claimer, stale_before)
    finally:
        locker.close()
        claimer.close()
        engine.dispose()
        get_settings.cache_clear()


def test_postgresql_concurrent_upload_init_enforces_global_quota_with_advisory_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import func

    from backend.app.modules.review_media.service import LocalMediaService

    database_url = _postgres_database_url()
    _prepare_postgresql_schema(database_url)
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    monkeypatch.setenv("UPLOAD_STORAGE_LOW_WATERMARK_BYTES", "0")
    get_settings.cache_clear()
    settings = get_settings()
    engine = create_engine(database_url)
    PgSession = sessionmaker(bind=engine, expire_on_commit=False)
    principal_id = f"quota-race-{uuid.uuid4().hex}"
    with PgSession() as observer:
        active_filter = UploadSessionModel.parts_cleanup_confirmed_at.is_(None)
        current_count, current_bytes = observer.execute(
            select(
                func.count(UploadSessionModel.id),
                func.coalesce(func.sum(UploadSessionModel.reserved_bytes), 0),
            ).where(active_filter)
        ).one()
    settings.max_active_upload_sessions_global = int(current_count) + 1
    settings.max_active_upload_sessions_per_principal = 8
    settings.max_reserved_upload_bytes_global = int(current_bytes) + 1024 * 1024
    settings.max_reserved_upload_bytes_per_principal = 1024 * 1024
    context = ExecutionContext(
        entry_source="edit",
        request_id=f"req-{_suffix()}",
        principal=PrincipalRef(kind="system", id=principal_id, project_ref_ids=("*",)),
        write_guard=WriteGuardState(mode="none", verified=True),
    )
    payload = {
        "original_filename": "quota-race.mp4",
        "mime_type": "video/mp4",
        "file_size": 128,
        "sha256": "a" * 64,
    }
    barrier = threading.Barrier(2)
    results: list[str] = []
    statements: list[str] = []
    results_lock = threading.Lock()

    @event.listens_for(engine, "before_cursor_execute")
    def capture_advisory_lock(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "pg_advisory_xact_lock" in statement:
            statements.append(statement)

    def initialize() -> None:
        with PgSession() as session:
            try:
                barrier.wait(timeout=5)
                LocalMediaService(session, settings, context).init_upload(payload)
                session.commit()
            except ReviewError as exc:
                session.rollback()
                result = exc.code
            else:
                result = "created"
            with results_lock:
                results.append(result)

    first = threading.Thread(target=initialize)
    second = threading.Thread(target=initialize)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(results) == ["RESOURCE_STATE_CONFLICT", "created"]
    assert len(statements) == 2

    with PgSession() as cleanup:
        created = cleanup.scalars(
            select(UploadSessionModel).where(
                UploadSessionModel.owner_principal_id == principal_id,
                UploadSessionModel.owner_principal_kind == "system",
            )
        ).all()
        assert len(created) == 1
        cleanup.delete(created[0])
        cleanup.commit()
    engine.dispose()
    get_settings.cache_clear()


def test_postgresql_same_request_id_concurrent_part_writes_do_not_share_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.review_media.service import LocalMediaService
    from backend.app.settings import get_settings
    from backend.app.upload_parts import new_upload_part_path

    database_url = _postgres_database_url()
    _prepare_postgresql_schema(database_url)
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    get_settings.cache_clear()
    settings = get_settings()
    engine = create_engine(database_url)
    PgSession = sessionmaker(bind=engine, expire_on_commit=False)
    upload_id = f"upl_{uuid.uuid4().hex}"
    upload_root = settings.storage_root / "uploads"
    upload_root.mkdir(parents=True, exist_ok=True)
    temp_path = upload_root / f"{upload_id}.parts"
    with PgSession() as seed:
        seed.add(
            UploadSessionModel(
                id=upload_id,
                original_filename="same-request.mp4",
                mime_type="video/mp4",
                declared_size=16,
                expected_sha256="0" * 64,
                status="initiated",
                temp_path=str(temp_path),
                owner_principal_id="same-request-writer",
                owner_principal_kind="system",
                received_parts={},
                duration_ms=1000,
                width=1920,
                height=1080,
                fps_num=25,
                fps_den=1,
            )
        )
        seed.commit()

    context = ExecutionContext(
        entry_source="edit",
        request_id=uuid.uuid4().hex,
        principal=PrincipalRef(kind="system", id="same-request-writer", project_ref_ids=("*",)),
        write_guard=WriteGuardState(mode="none", verified=True),
    )
    first_candidate = new_upload_part_path(upload_root, upload_id, 1)
    second_candidate = new_upload_part_path(upload_root, upload_id, 1)
    assert first_candidate != second_candidate
    first_candidate.write_bytes(b"a" * 16)
    second_candidate.write_bytes(b"b" * 16)
    first_locked = threading.Event()
    release_first = threading.Event()
    second_prepared = threading.Event()
    failures: list[BaseException] = []

    def write_candidate(candidate: Path, hold_lock: bool) -> None:
        try:
            with PgSession() as session:
                service = LocalMediaService(session, settings, context)
                prepared = service.put_part_file(upload_id, 1, candidate, 16)
                if hold_lock:
                    first_locked.set()
                    assert release_first.wait(timeout=5)
                else:
                    second_prepared.set()
                session.commit()
                service.discard_superseded_part(prepared)
        except BaseException as exc:
            failures.append(exc)

    first_thread = threading.Thread(target=write_candidate, args=(first_candidate, True))
    second_thread = threading.Thread(target=write_candidate, args=(second_candidate, False))
    first_thread.start()
    assert first_locked.wait(timeout=5)
    second_thread.start()
    time.sleep(0.2)
    assert not second_prepared.is_set()
    release_first.set()
    first_thread.join(timeout=10)
    second_thread.join(timeout=10)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert failures == []
    with PgSession() as observer:
        upload = observer.get(UploadSessionModel, upload_id)
        assert upload is not None
        selected_path = Path(upload.received_parts["1"]["path"])
        assert selected_path == second_candidate
        assert selected_path.read_bytes() == b"b" * 16
    assert not first_candidate.exists()

    second_candidate.unlink(missing_ok=True)
    engine.dispose()
    get_settings.cache_clear()


def test_postgresql_orphan_cleanup_skips_put_transaction_and_preserves_committed_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from backend.app.maintenance_cleanup import _cleanup_orphan_upload_parts
    from backend.app.settings import get_settings
    from backend.app.upload_parts import new_upload_part_path

    database_url = _postgres_database_url()
    _prepare_postgresql_schema(database_url)
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    get_settings.cache_clear()
    settings = get_settings()
    engine = create_engine(database_url)
    PgSession = sessionmaker(bind=engine, expire_on_commit=False)
    upload_id = f"upl_{uuid.uuid4().hex}"
    upload_root = settings.storage_root / "uploads"
    upload_root.mkdir(parents=True, exist_ok=True)
    temp_path = upload_root / f"{upload_id}.parts"
    candidate = new_upload_part_path(upload_root, upload_id, 1)
    candidate.write_bytes(b"candidate")
    stale_at = (utcnow() - timedelta(seconds=settings.upload_session_ttl_seconds + 5)).timestamp()
    os.utime(candidate, (stale_at, stale_at))
    with PgSession() as seed:
        seed.add(
            UploadSessionModel(
                id=upload_id,
                original_filename="orphan-race.mp4",
                mime_type="video/mp4",
                declared_size=candidate.stat().st_size,
                expected_sha256="0" * 64,
                status="initiated",
                temp_path=str(temp_path),
                owner_principal_id="orphan-race",
                owner_principal_kind="system",
                received_parts={},
                duration_ms=1000,
                width=1920,
                height=1080,
                fps_num=25,
                fps_den=1,
            )
        )
        seed.commit()

    writer = PgSession()
    cleaner = PgSession()
    try:
        upload = writer.scalar(select(UploadSessionModel).where(UploadSessionModel.id == upload_id).with_for_update())
        assert upload is not None
        upload.received_parts = {"1": {"path": str(candidate), "size": candidate.stat().st_size}}
        upload.status = "receiving"
        writer.flush()

        stale_before = utcnow() - timedelta(seconds=settings.upload_session_ttl_seconds)
        assert _cleanup_orphan_upload_parts(cleaner, upload_root, stale_before) == (0, 0)
        assert candidate.exists()

        writer.commit()
        assert _cleanup_orphan_upload_parts(cleaner, upload_root, stale_before) == (0, 0)
        assert candidate.exists()
        with PgSession() as observer:
            persisted = observer.get(UploadSessionModel, upload_id)
            assert persisted is not None
            assert persisted.received_parts["1"]["path"] == str(candidate)
    finally:
        writer.close()
        cleaner.close()
        candidate.unlink(missing_ok=True)
        engine.dispose()
        get_settings.cache_clear()


def test_postgresql_issue_delete_and_request_changes_serialize() -> None:
    database_url = _postgres_database_url()
    _prepare_postgresql_schema(database_url)
    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine)
    seed_session = SessionLocal()
    try:
        prefix = _suffix()
        project, item, version, file = _seed_item(seed_session, prefix)
        playback_path = Path(file.storage_path)
        playback_path.write_bytes(b"postgres-lock-test")
        issue_id = f"iss_{prefix}_race"
        revision_id = f"rev_{prefix}_race"
        item.workflow_status = "in_review"
        seed_session.add(
            ReviewIssueModel(
                id=issue_id,
                project_ref_id=project.id,
                review_item_id=item.id,
                version_id=version.id,
                issue_no=1,
                status="unresolved",
                current_revision_id=revision_id,
                timestamp_ms=1,
                frame_number=1,
            )
        )
        seed_session.add(
            ReviewIssueRevisionModel(
                id=revision_id,
                project_ref_id=project.id,
                review_item_id=item.id,
                version_id=version.id,
                issue_id=issue_id,
                revision_no=1,
                content="race",
            )
        )
        seed_session.commit()
        project_id = project.id
        item_id = item.id
        version_id = version.id
    finally:
        seed_session.close()

    context = ExecutionContext(
        entry_source="review",
        request_id=f"req_{prefix}",
        principal=PrincipalRef(kind="system", id="pg-test", project_ref_ids=(project_id,)),
        write_guard=WriteGuardState(mode="none", verified=True),
    )
    delete_session = SessionLocal()
    delete_repo = SqlAlchemyReviewRepository(delete_session, get_settings())
    issue = delete_session.get(ReviewIssueModel, issue_id)
    assert issue is not None
    delete_repo.execute(
        "SoftDeleteReviewIssue",
        {
            "project_ref_id": project_id,
            "review_item_id": item_id,
            "version_id": version_id,
            "issue_id": issue_id,
        },
        context,
        issue.lock_version,
    )

    with SessionLocal() as blocked_session:
        blocked_session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        blocked_item = blocked_session.get(ReviewItemModel, item_id)
        assert blocked_item is not None
        statements: list[str] = []
        blocked_connection = blocked_session.connection()

        @event.listens_for(blocked_connection, "before_cursor_execute")
        def record_issue_statement(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
            statements.append(" ".join(statement.split()))

        with pytest.raises(DBAPIError) as exc_info:
            SqlAlchemyReviewRepository(blocked_session, get_settings()).execute(
                "RequestChanges",
                {
                    "project_ref_id": project_id,
                    "review_item_id": item_id,
                    "version_id": version_id,
                    "summary": "must serialize",
                },
                context,
                blocked_item.lock_version,
            )
        sqlstate = getattr(exc_info.value.orig, "sqlstate", None) or getattr(exc_info.value.orig, "pgcode", None)
        assert sqlstate == "55P03"
        assert any("FROM project_refs" in statement and "FOR UPDATE" in statement for statement in statements)
        assert not any("FROM review_issues" in statement and "count" in statement.lower() for statement in statements)
        blocked_session.rollback()

    delete_session.commit()

    with SessionLocal() as request_session:
        request_item = request_session.get(ReviewItemModel, item_id)
        assert request_item is not None
        with pytest.raises(ReviewError) as review_error_info:
            SqlAlchemyReviewRepository(request_session, get_settings()).execute(
                "RequestChanges",
                {
                    "project_ref_id": project_id,
                    "review_item_id": item_id,
                    "version_id": version_id,
                    "summary": "must serialize",
                },
                context,
                request_item.lock_version,
            )
        assert review_error_info.value.code == "NO_UNRESOLVED_ISSUE"

    with SessionLocal() as check_session:
        stored_item = check_session.get(ReviewItemModel, item_id)
        stored_issue = check_session.get(ReviewIssueModel, issue_id)
        assert stored_item is not None and stored_item.workflow_status == "in_review"
        assert stored_issue is not None and stored_issue.deleted_at is not None

    delete_session.close()
    playback_path.unlink(missing_ok=True)
    engine.dispose()


def test_postgresql_project_delete_and_child_create_serialize() -> None:
    database_url = _postgres_database_url()
    _prepare_postgresql_schema(database_url)
    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine)
    seed_session = SessionLocal()
    try:
        prefix = _suffix()
        project, _, _, _ = _seed_item(seed_session, prefix)
        child_file = _file(f"file_{prefix}_child")
        seed_session.add(child_file)
        seed_session.commit()
        project_id = project.id
        file_id = child_file.id
    finally:
        seed_session.close()

    context = ExecutionContext(
        entry_source="edit",
        request_id=f"req_{prefix}",
        principal=PrincipalRef(kind="system", id="pg-test", project_ref_ids=(project_id,)),
        write_guard=WriteGuardState(mode="none", verified=True),
    )
    delete_session = SessionLocal()
    delete_project = delete_session.get(ProjectRefModel, project_id)
    assert delete_project is not None
    SqlAlchemyReviewRepository(delete_session, get_settings()).execute(
        "SoftDeleteProject",
        {"project_ref_id": project_id},
        context,
        delete_project.lock_version,
    )

    with SessionLocal() as blocked_session:
        blocked_session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        statements: list[str] = []
        blocked_connection = blocked_session.connection()

        @event.listens_for(blocked_connection, "before_cursor_execute")
        def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
            statements.append(" ".join(statement.split()))

        with pytest.raises(DBAPIError) as exc_info:
            SqlAlchemyReviewRepository(blocked_session, get_settings()).execute(
                "CreateReviewItem",
                {
                    "project_ref_id": project_id,
                    "item_code": f"ITEM_{prefix}_CHILD",
                    "title": "blocked child",
                    "original_file_id": file_id,
                },
                context,
                None,
            )
        sqlstate = getattr(exc_info.value.orig, "sqlstate", None) or getattr(exc_info.value.orig, "pgcode", None)
        assert sqlstate == "55P03"
        assert any("FROM project_refs" in statement and "FOR UPDATE" in statement for statement in statements)
        assert not any("INSERT INTO review_items" in statement for statement in statements)
        blocked_session.rollback()

    delete_session.commit()
    with SessionLocal() as rejected_session:
        with pytest.raises(ReviewError) as review_error_info:
            SqlAlchemyReviewRepository(rejected_session, get_settings()).execute(
                "CreateReviewItem",
                {
                    "project_ref_id": project_id,
                    "item_code": f"ITEM_{prefix}_AFTER_DELETE",
                    "title": "rejected child",
                    "original_file_id": file_id,
                },
                context,
                None,
            )
        assert review_error_info.value.code == "RESOURCE_STATE_CONFLICT"

    delete_session.close()
    engine.dispose()


def test_postgresql_deferrable_current_revision_and_finalization_constraints() -> None:
    database_url = _postgres_database_url()
    _prepare_postgresql_schema(database_url)
    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        prefix = _suffix()
        project, item, version, file = _seed_item(session, prefix)

        bad_issue = ReviewIssueModel(
            id=f"iss_{prefix}_null",
            project_ref_id=project.id,
            review_item_id=item.id,
            version_id=version.id,
            issue_no=1,
            status="unresolved",
            current_revision_id=None,  # type: ignore[arg-type]
            timestamp_ms=1,
            frame_number=1,
        )
        session.add(bad_issue)
        with pytest.raises(DBAPIError):
            session.commit()
        session.rollback()

        issue_id = f"iss_{prefix}_ok"
        revision_id = f"rev_{prefix}_ok"
        session.add(
            ReviewIssueModel(
                id=issue_id,
                project_ref_id=project.id,
                review_item_id=item.id,
                version_id=version.id,
                issue_no=2,
                status="unresolved",
                current_revision_id=revision_id,
                timestamp_ms=1,
                frame_number=1,
            )
        )
        session.add(
            ReviewIssueRevisionModel(
                id=revision_id,
                project_ref_id=project.id,
                review_item_id=item.id,
                version_id=version.id,
                issue_id=issue_id,
                revision_no=1,
                content="initial",
            )
        )
        session.commit()

        other_issue_id = f"iss_{prefix}_other"
        other_revision_id = f"rev_{prefix}_other"
        session.add(
            ReviewIssueModel(
                id=other_issue_id,
                project_ref_id=project.id,
                review_item_id=item.id,
                version_id=version.id,
                issue_no=3,
                status="unresolved",
                current_revision_id=other_revision_id,
                timestamp_ms=2,
                frame_number=2,
            )
        )
        session.add(
            ReviewIssueRevisionModel(
                id=other_revision_id,
                project_ref_id=project.id,
                review_item_id=item.id,
                version_id=version.id,
                issue_id=other_issue_id,
                revision_no=1,
                content="other",
            )
        )
        session.commit()

        issue = session.get(ReviewIssueModel, issue_id)
        assert issue is not None
        issue.current_revision_id = other_revision_id
        with pytest.raises(DBAPIError):
            session.commit()
        session.rollback()

        item.workflow_status = "finalized"
        item.active_finalization_id = None
        with pytest.raises(DBAPIError):
            session.commit()
        session.rollback()
        item_after_failed_finalized_state = session.get(ReviewItemModel, item.id)
        assert item_after_failed_finalized_state is not None

        file_v2 = _file(f"file_{prefix}_v2")
        version_v2 = _version(f"ver_{prefix}_v2", project.id, item_after_failed_finalized_state.id, file_v2, 2, True)
        item_after_failed_finalized_state.current_version_id = None
        session.flush()
        version.is_current = False
        session.add(file_v2)
        session.add(version_v2)
        session.flush()
        item_after_failed_finalized_state.current_version_id = version_v2.id
        session.commit()

        item_with_noncurrent_version = session.get(ReviewItemModel, item_after_failed_finalized_state.id)
        assert item_with_noncurrent_version is not None
        item_with_noncurrent_version.current_version_id = version.id
        with pytest.raises(DBAPIError):
            session.commit()
        session.rollback()

        item_without_current_flag = session.get(ReviewItemModel, item_after_failed_finalized_state.id)
        assert item_without_current_flag is not None
        item_without_current_flag.current_version_id = version.id
        item_without_current_flag.current_version_is_current = None  # type: ignore[assignment]
        with pytest.raises(DBAPIError):
            session.commit()
        session.rollback()

        stale_finalization_id = f"fin_{prefix}_old"
        stale_finalization = FinalizationRecordModel(
            id=stale_finalization_id,
            project_ref_id=project.id,
            review_item_id=item_after_failed_finalized_state.id,
            version_id=version.id,
            version_no=version.version_no,
            original_file_id=file.id,
            original_filename=file.original_filename,
            mime_type=file.mime_type,
            file_size=file.file_size,
            sha256=file.sha256,
            duration_ms=file.duration_ms,
            width=file.width,
            height=file.height,
            fps_num=file.fps_num,
            fps_den=file.fps_den,
            media_probe_version=file.media_probe_version,
            status="active",
        )
        session.add(stale_finalization)
        session.commit()

        item_with_stale_finalization = session.get(ReviewItemModel, item_after_failed_finalized_state.id)
        assert item_with_stale_finalization is not None
        item_with_stale_finalization.workflow_status = "finalized"
        item_with_stale_finalization.active_finalization_id = stale_finalization_id
        with pytest.raises(DBAPIError):
            session.commit()
        session.rollback()

        item_without_current_version = session.get(ReviewItemModel, item_after_failed_finalized_state.id)
        assert item_without_current_version is not None
        item_without_current_version.workflow_status = "finalized"
        item_without_current_version.current_version_id = None
        item_without_current_version.active_finalization_id = stale_finalization_id
        with pytest.raises(DBAPIError):
            session.commit()
        session.rollback()

        positive_prefix = _suffix()
        positive_project, positive_item, positive_version, positive_file = _seed_item(session, positive_prefix)
        current_finalization = FinalizationRecordModel(
            id=f"fin_{positive_prefix}_current",
            project_ref_id=positive_project.id,
            review_item_id=positive_item.id,
            version_id=positive_version.id,
            version_no=positive_version.version_no,
            original_file_id=positive_file.id,
            original_filename=positive_file.original_filename,
            mime_type=positive_file.mime_type,
            file_size=positive_file.file_size,
            sha256=positive_file.sha256,
            duration_ms=positive_file.duration_ms,
            width=positive_file.width,
            height=positive_file.height,
            fps_num=positive_file.fps_num,
            fps_den=positive_file.fps_den,
            media_probe_version=positive_file.media_probe_version,
            status="active",
        )
        current_finalization_id = current_finalization.id
        session.add(current_finalization)
        session.commit()

        positive_item_reloaded = session.get(ReviewItemModel, positive_item.id)
        assert positive_item_reloaded is not None
        positive_item_reloaded.active_finalization_id = current_finalization_id
        positive_item_reloaded.workflow_status = "finalized"
        session.commit()
        assert positive_item_reloaded.active_finalization_id == current_finalization_id
        assert positive_item_reloaded.current_version_id == positive_version.id
    finally:
        session.close()
        engine.dispose()
