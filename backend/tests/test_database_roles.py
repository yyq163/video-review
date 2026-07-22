from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
import yaml  # type: ignore[import-untyped]
from psycopg import sql
from sqlalchemy.engine import make_url

import backend.scripts.run_database_migrations as migration_runner
from backend.scripts.bootstrap_database_roles import (
    BootstrapConfig,
    _ensure_login_role,
    bootstrap_database_roles,
    require_identifier,
)
from backend.scripts.grant_runtime_role import RuntimeGrantConfig, grant_runtime_role

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_SERVICES = ("backend", "maintenance", "package-worker")
HIGH_PRIVILEGE_ENV_PREFIXES = (
    "POSTGRES_ADMIN_",
    "POSTGRES_OWNER_",
    "DATABASE_ADMIN_",
    "DATABASE_OWNER_",
)
UPLOAD_RUNTIME_DEFAULTS = {
    "UPLOAD_PART_READ_TIMEOUT_SECONDS": "120",
    "MAX_INFLIGHT_UPLOAD_PARTS_PER_PRINCIPAL": "16",
    "MAX_INFLIGHT_UPLOAD_PARTS_PER_SESSION": "1",
    "MAX_INFLIGHT_UPLOAD_PART_CANDIDATES": "64",
    "MAX_ACTIVE_UPLOAD_SESSIONS_PER_PRINCIPAL": "16",
}


def test_runtime_grant_script_excludes_migration_dml_and_sequence_update() -> None:
    source = (ROOT / "backend/scripts/grant_runtime_role.py").read_text(encoding="utf-8")
    assert "autocommit=True" not in source
    assert "GRANT USAGE, SELECT ON ALL SEQUENCES" in source
    assert "GRANT USAGE, SELECT ON SEQUENCES" in source
    assert "USAGE, SELECT, UPDATE ON" not in source
    assert "REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER" in source
    assert "ON TABLE public.alembic_version" in source
    assert "GRANT SELECT ON TABLE public.alembic_version" in source
    assert "REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER" in source
    assert "ON TABLE public.operation_logs" in source
    for object_type in ("TABLES", "SEQUENCES", "FUNCTIONS"):
        revoke = f"ALTER DEFAULT PRIVILEGES FOR ROLE {{}} IN SCHEMA public REVOKE ALL PRIVILEGES ON {object_type} FROM {{}}"
        assert revoke in source
    assert source.index("REVOKE ALL PRIVILEGES ON TABLES FROM {}") < source.index("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}")
    assert source.index("REVOKE ALL PRIVILEGES ON SEQUENCES FROM {}") < source.index("GRANT USAGE, SELECT ON SEQUENCES TO {}")
    assert "ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC" in source


def test_compose_isolates_database_role_credentials_and_migration_lifecycle() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    class ComposeLoader(yaml.SafeLoader):
        pass

    ComposeLoader.add_constructor("!reset", lambda _loader, _node: None)
    delivery = yaml.load(
        (ROOT / "docker-compose.delivery.yml").read_text(encoding="utf-8"),
        Loader=ComposeLoader,
    )
    services = compose["services"]

    assert set(services["postgres"]["environment"]) == {
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD_FILE",
    }
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD_FILE"] == ("/run/secrets/postgres_admin_password")
    assert services["postgres"]["secrets"] == ["postgres_admin_password"]
    postgres_healthcheck = " ".join(services["postgres"]["healthcheck"]["test"])
    assert 'PGPASSWORD="$$(cat /run/secrets/postgres_admin_password)"' in postgres_healthcheck
    assert "psql -w" in postgres_healthcheck
    assert "SELECT 1" in postgres_healthcheck
    assert "pg_isready" not in postgres_healthcheck
    for service_name in RUNTIME_SERVICES:
        environment = services[service_name]["environment"]
        assert environment["DATABASE_USER"].startswith("${POSTGRES_APP_USER:")
        assert environment["DATABASE_PASSWORD_FILE"] == "/run/secrets/postgres_app_password"
        assert "DATABASE_PASSWORD" not in environment
        assert "postgres_app_password" in services[service_name]["secrets"]
        assert not any(key.startswith(prefix) for key in environment for prefix in HIGH_PRIVILEGE_ENV_PREFIXES)
        assert "POSTGRES_ADMIN_PASSWORD" not in environment
        assert "POSTGRES_OWNER_PASSWORD" not in environment

    migrate_environment = services["migrate"]["environment"]
    assert {
        "POSTGRES_ADMIN_PASSWORD_FILE",
        "POSTGRES_OWNER_PASSWORD_FILE",
        "POSTGRES_APP_PASSWORD_FILE",
        "DATABASE_PASSWORD_FILE",
    } <= set(migrate_environment)
    assert not {
        "POSTGRES_ADMIN_PASSWORD",
        "POSTGRES_OWNER_PASSWORD",
        "POSTGRES_APP_PASSWORD",
        "DATABASE_PASSWORD",
    } & set(migrate_environment)
    assert set(services["migrate"]["secrets"]) == {
        "postgres_admin_password",
        "postgres_owner_password",
        "postgres_app_password",
    }
    assert compose["secrets"] == {
        "postgres_admin_password": {"file": "${COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE:?COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE is required}"},
        "postgres_owner_password": {"file": "${COMPOSE_POSTGRES_OWNER_PASSWORD_FILE:?COMPOSE_POSTGRES_OWNER_PASSWORD_FILE is required}"},
        "postgres_app_password": {"file": "${COMPOSE_POSTGRES_APP_PASSWORD_FILE:?COMPOSE_POSTGRES_APP_PASSWORD_FILE is required}"},
        "write_guard_code": {"file": "${COMPOSE_WRITE_GUARD_CODE_FILE:?COMPOSE_WRITE_GUARD_CODE_FILE is required}"},
        "write_guard_session_secret": {"file": "${COMPOSE_WRITE_GUARD_SESSION_SECRET_FILE:?COMPOSE_WRITE_GUARD_SESSION_SECRET_FILE is required}"},
    }
    assert services["migrate"]["restart"] == "no"
    assert "/var/lib/fj-runtime-state" in services["migrate"]["tmpfs"]
    assert delivery["services"]["migrate"]["image"] == (
        "${BACKEND_IMAGE_REPOSITORY:?BACKEND_IMAGE_REPOSITORY is required}@sha256:${BACKEND_IMAGE_DIGEST:?BACKEND_IMAGE_DIGEST is required}"
    )
    assert delivery["services"]["migrate"]["build"] is None
    assert services["migrate"]["command"] == ["python", "backend/scripts/run_database_migrations.py"]
    assert services["backend"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert "alembic" not in " ".join(services["backend"].get("command", []))
    for name, default in UPLOAD_RUNTIME_DEFAULTS.items():
        assert services["backend"]["environment"][name].endswith(f":-{default}}}")
        assert name not in services["maintenance"]["environment"]
        assert name not in services["package-worker"]["environment"]
        assert f"{name}={default}" in (ROOT / ".env.example").read_text(encoding="utf-8")
    for service_name in ("backend", "maintenance"):
        assert services[service_name]["environment"]["UPLOAD_SESSION_TTL_SECONDS"].endswith(":-900}")
    assert "UPLOAD_SESSION_TTL_SECONDS" not in services["package-worker"]["environment"]
    assert "UPLOAD_SESSION_TTL_SECONDS=900" in (ROOT / ".env.example").read_text(encoding="utf-8")


def test_backup_smoke_claims_only_exclusively_created_resources() -> None:
    compose_smoke = (ROOT / "scripts/postgres-backup-restore-smoke.sh").read_text(encoding="utf-8")
    host_smoke = (ROOT / "scripts/postgres-host-backup-restore-smoke.sh").read_text(encoding="utf-8")

    assert 'REVOKE ALL PRIVILEGES ON DATABASE \\"$OWNED_DB\\" FROM PUBLIC' in compose_smoke
    assert compose_smoke.index("blob_identity=$(compose run") < compose_smoke.index("    blob_created=1")
    assert compose_smoke.index("    blob_created=1") < compose_smoke.index(
        'test "$blob_identity" = "$blob_device:$blob_inode"'
    )
    compose_source_arm = compose_smoke.index("\nsource_db_created=1\n")
    compose_source_create = compose_smoke.index('create_owned_database "$source_db"')
    compose_restore_arm = compose_smoke.index("\nrestore_db_created=1\n")
    compose_restore_create = compose_smoke.index('create_owned_database "$restore_db"')
    assert compose_source_arm < compose_source_create < compose_smoke.index('mark_owned_database "$source_db"')
    assert compose_restore_arm < compose_restore_create
    restore_create = compose_smoke.index('create_owned_database "$restore_db"')
    assert compose_smoke.index('mark_owned_database "$restore_db"', restore_create) < compose_smoke.index(
        " pg_restore ", restore_create
    )
    drop_function = compose_smoke[compose_smoke.index("drop_owned_database()") : compose_smoke.index("mark_owned_database()")]
    assert drop_function.index('test "$OWNED_DB" = "$expected_db_name"') < drop_function.index(
        "if ! database_identity=$("
    )
    assert 'if [ -z "$database_identity" ]; then\n    exit 0' in drop_function
    assert '("$POSTGRES_OWNER_USER:$EXPECTED_DB_SENTINEL")' in drop_function
    assert drop_function.index('case "$database_identity" in') < drop_function.index(
        'REVOKE ALL PRIVILEGES ON DATABASE \\"$OWNED_DB\\" FROM PUBLIC'
    )
    assert "dropdb --force" in drop_function
    host_source_arm = host_smoke.index("\nsource_created=1\n")
    host_source_create = host_smoke.index('create_owned_database "$source_db"')
    host_restore_arm = host_smoke.index("\nrestore_created=1\n")
    host_restore_create = host_smoke.index('create_owned_database "$restore_db"')
    assert host_source_arm < host_source_create < host_smoke.index('secure_owned_database "$source_db"')
    assert host_restore_arm < host_restore_create
    assert host_smoke.index("blob_identity=$(") < host_smoke.index("blob_created=1")
    assert host_smoke.index("blob_created=1") < host_smoke.index(
        'if [ "$blob_identity" != "$blob_device:$blob_inode" ]'
    )
    mark_function = compose_smoke[
        compose_smoke.index("mark_owned_database()") : compose_smoke.index("cleanup()")
    ]
    assert mark_function.index('test "$database_owner" = "$POSTGRES_OWNER_USER"') < mark_function.index(
        'REVOKE ALL PRIVILEGES ON DATABASE \\"$OWNED_DB\\" FROM PUBLIC'
    )
    assert mark_function.index('test "$database_sentinel" = "$EXPECTED_DB_SENTINEL"') < mark_function.index(
        'REVOKE ALL PRIVILEGES ON DATABASE \\"$OWNED_DB\\" FROM PUBLIC'
    )
    host_secure_function = host_smoke[
        host_smoke.index("secure_owned_database()") : host_smoke.index("run_owner()")
    ]
    assert host_secure_function.index('test "$(database_identity "$database_name")"') < host_secure_function.index(
        'REVOKE ALL PRIVILEGES ON DATABASE \\"$database_name\\" FROM PUBLIC'
    )
    compose_create_function = compose_smoke[
        compose_smoke.index("create_owned_database()") : compose_smoke.index("drop_owned_database()")
    ]
    host_create_function = host_smoke[
        host_smoke.index("create_owned_database()") : host_smoke.index("secure_owned_database()")
    ]
    assert compose_create_function.index("existing_identity") < compose_create_function.index("createdb")
    assert host_create_function.index("database_identity") < host_create_function.index("createdb")
    assert "created_identity" not in compose_create_function
    assert "created_identity" not in host_create_function
    assert "dropdb" not in compose_create_function
    assert "dropdb" not in host_create_function
    host_drop_function = host_smoke[host_smoke.index("drop_owned_database()") : host_smoke.index("cleanup()")]
    assert 'validate_owned_database_identity "$database_name" "$expected_identity"' in host_drop_function
    assert 'if ! actual_identity=$(database_identity "$database_name")' in host_drop_function
    assert 'if [ -z "$actual_identity" ]; then\n        return 0' in host_drop_function
    assert '("$POSTGRES_OWNER_USER:$expected_identity")' in host_drop_function
    assert host_drop_function.index('case "$actual_identity" in') < host_drop_function.index("dropdb --force")
    assert "could not verify temporary database identity during cleanup" in host_drop_function


def test_database_migration_runner_holds_one_lock_for_both_databases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BootstrapConfig(
        host="127.0.0.1",
        port=5432,
        admin_database="postgres",
        admin_user="test_admin",
        admin_password="not-used",
        owner_user="test_owner",
        owner_password="not-used",
        runtime_user="test_runtime",
        runtime_password="not-used",
        application_database="test_application",
        test_database="test_database",
    )
    events: list[str] = []
    runs: list[tuple[tuple[str, ...], str]] = []
    bootstrap_calls: list[tuple[bool, bool]] = []

    class FakeCursor:
        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: object, parameters: object = None) -> None:
            del parameters
            events.append(f"sql:{statement}")

        def fetchone(self) -> tuple[object, ...]:
            statement = events[-1]
            return (True,) if "pg_advisory_unlock" in statement else (None,)

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> FakeCursor:
            return FakeCursor()

    monkeypatch.setattr(migration_runner, "_connect", lambda *_args: FakeConnection())
    monkeypatch.setattr(migration_runner, "_ensure_admin_connection", lambda _connection: None)

    def capture_bootstrap(
        _config: BootstrapConfig,
        *,
        keep_runtime_quiesced: bool,
        migration_sequence_lock_held: bool,
    ) -> None:
        events.append("bootstrap")
        bootstrap_calls.append((keep_runtime_quiesced, migration_sequence_lock_held))

    monkeypatch.setattr(migration_runner, "bootstrap_database_roles", capture_bootstrap)

    def capture_run(command: Sequence[str], environment: Mapping[str, str]) -> None:
        events.append("run")
        runs.append((tuple(command), environment["DATABASE_NAME"]))

    monkeypatch.setattr(migration_runner, "_run", capture_run)
    monkeypatch.setenv("DATABASE_NAME", config.application_database)

    migration_runner.run_database_migrations(config)

    assert events[0].startswith("sql:") and "pg_advisory_lock" in events[0]
    assert events[-1].startswith("sql:") and "pg_advisory_unlock" in events[-1]
    assert bootstrap_calls == [(True, True)]
    assert len(runs) == 4
    assert [database for _command, database in runs] == [
        config.application_database,
        config.application_database,
        config.test_database,
        config.test_database,
    ]
    assert sum("alembic" in command for command, _database in runs) == 2
    assert sum(any(part.endswith("grant_runtime_role.py") for part in command) for command, _database in runs) == 2


def test_database_migration_runner_preserves_command_failure_when_unlock_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BootstrapConfig(
        host="127.0.0.1",
        port=5432,
        admin_database="postgres",
        admin_user="test_admin",
        admin_password="not-used",
        owner_user="test_owner",
        owner_password="not-used",
        runtime_user="test_runtime",
        runtime_password="not-used",
        application_database="test_application",
        test_database="test_database",
    )

    class FakeCursor:
        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def execute(_statement: object, _parameters: object = None) -> None:
            return None

        @staticmethod
        def fetchone() -> tuple[bool]:
            return (False,)

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def cursor() -> FakeCursor:
            return FakeCursor()

    command_failure = subprocess.CalledProcessError(2, ["migration"])
    activations: list[str] = []
    monkeypatch.setattr(migration_runner, "_connect", lambda *_args: FakeConnection())
    monkeypatch.setattr(migration_runner, "_ensure_admin_connection", lambda _connection: None)
    monkeypatch.setattr(migration_runner, "bootstrap_database_roles", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        migration_runner,
        "_activate_login_role",
        lambda _connection, role_name: activations.append(role_name),
    )
    monkeypatch.setattr(migration_runner, "_run", lambda *_args: (_ for _ in ()).throw(command_failure))
    monkeypatch.setenv("DATABASE_NAME", config.application_database)

    with pytest.raises(subprocess.CalledProcessError) as captured:
        migration_runner.run_database_migrations(config)
    assert captured.value is command_failure
    assert any("lock release also failed" in note for note in getattr(captured.value, "__notes__", []))
    assert activations == []


@pytest.mark.parametrize("failed_command_index", range(4))
def test_database_migration_runner_keeps_runtime_nologin_after_any_sequence_failure(
    monkeypatch: pytest.MonkeyPatch,
    failed_command_index: int,
) -> None:
    config = BootstrapConfig(
        host="127.0.0.1",
        port=5432,
        admin_database="postgres",
        admin_user="test_admin",
        admin_password="not-used",
        owner_user="test_owner",
        owner_password="not-used",
        runtime_user="test_runtime",
        runtime_password="not-used",
        application_database="test_application",
        test_database="test_database",
    )
    activations: list[str] = []
    run_index = 0

    class FakeCursor:
        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def execute(_statement: object, _parameters: object = None) -> None:
            return None

        @staticmethod
        def fetchone() -> tuple[bool]:
            return (True,)

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def cursor() -> FakeCursor:
            return FakeCursor()

    def fail_selected_command(_command: Sequence[str], _environment: Mapping[str, str]) -> None:
        nonlocal run_index
        current_index = run_index
        run_index += 1
        if current_index == failed_command_index:
            raise subprocess.CalledProcessError(2, ["migration-step"])

    monkeypatch.setattr(migration_runner, "_connect", lambda *_args: FakeConnection())
    monkeypatch.setattr(migration_runner, "_ensure_admin_connection", lambda _connection: None)
    monkeypatch.setattr(migration_runner, "bootstrap_database_roles", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(migration_runner, "_run", fail_selected_command)
    monkeypatch.setattr(
        migration_runner,
        "_activate_login_role",
        lambda _connection, role_name: activations.append(role_name),
    )
    monkeypatch.setenv("DATABASE_NAME", config.application_database)

    with pytest.raises(subprocess.CalledProcessError):
        migration_runner.run_database_migrations(config)

    assert activations == []


def test_database_role_bootstrap_preserves_primary_failure_when_unlock_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.scripts.bootstrap_database_roles as bootstrap_mod

    config = BootstrapConfig(
        host="127.0.0.1",
        port=5432,
        admin_database="postgres",
        admin_user="test_admin",
        admin_password="not-used",
        owner_user="test_owner",
        owner_password="not-used",
        runtime_user="test_runtime",
        runtime_password="not-used",
        application_database="test_application",
        test_database="test_database",
    )

    class FakeCursor:
        statement = ""

        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: object, _parameters: object = None) -> None:
            self.statement = str(statement)

        def fetchone(self) -> tuple[bool]:
            return ("pg_try_advisory_lock" in self.statement,)

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def cursor() -> FakeCursor:
            return FakeCursor()

    monkeypatch.setattr(bootstrap_mod, "_connect", lambda *_args: FakeConnection())
    monkeypatch.setattr(bootstrap_mod, "_ensure_admin_connection", lambda _connection: None)
    monkeypatch.setattr(
        bootstrap_mod,
        "_preflight_existing_databases",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("primary bootstrap failure")),
    )

    with pytest.raises(RuntimeError, match="primary bootstrap failure") as captured:
        bootstrap_mod.bootstrap_database_roles(config)

    assert any("lock release also failed" in note for note in getattr(captured.value, "__notes__", []))


@pytest.mark.parametrize(
    "value",
    ("", "9starts_with_digit", "contains-dash", "contains space", "x" * 64, 'quoted"name'),
)
def test_bootstrap_rejects_unsafe_postgresql_identifiers(value: str) -> None:
    with pytest.raises(ValueError, match="valid unquoted PostgreSQL identifier"):
        require_identifier("TEST_IDENTIFIER", value)


@pytest.mark.parametrize(
    ("role_exists", "expected_verb"),
    ((False, "CREATE"), (True, "ALTER")),
)
def test_bootstrap_role_password_sql_contains_verifier_not_raw_secret(
    role_exists: bool,
    expected_verb: str,
) -> None:
    role_name = "test_runtime"
    raw_secret = "Raw-role-secret:must-not-enter-SQL!"
    verifier = "SCRAM-SHA-256$4096:c2FsdC1mb3ItdGVzdHM=$c3RvcmVkLWtleS1mb3ItdGVzdHM=:c2VydmVyLWtleS1mb3ItdGVzdHM="

    class FakePgconn:
        @staticmethod
        def encrypt_password(password: bytes, user: bytes, algorithm: bytes) -> bytes:
            assert password == raw_secret.encode()
            assert user == role_name.encode()
            assert algorithm == b"scram-sha-256"
            return verifier.encode("ascii")

    class FakeCursor:
        def __init__(self) -> None:
            self.statements: list[tuple[object, object]] = []

        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: object, parameters: object = None) -> None:
            self.statements.append((statement, parameters))

        def fetchone(self) -> tuple[int] | None:
            return (1,) if role_exists else None

        @staticmethod
        def fetchall() -> list[tuple[object, ...]]:
            return []

    class FakeConnection:
        def __init__(self) -> None:
            self.pgconn = FakePgconn()
            self.cursor_instance = FakeCursor()

        def cursor(self) -> FakeCursor:
            return self.cursor_instance

    connection = FakeConnection()
    _ensure_login_role(connection, role_name, raw_secret)  # type: ignore[arg-type]

    rendered_statements = [
        statement.as_string() if isinstance(statement, sql.Composable) else str(statement) for statement, _parameters in connection.cursor_instance.statements
    ]
    password_statement = next(statement for statement in rendered_statements if statement.startswith(f"{expected_verb} ROLE"))
    assert raw_secret not in password_statement
    assert verifier in password_statement
    assert all(raw_secret not in str(parameters) for _statement, parameters in connection.cursor_instance.statements)


@dataclass(frozen=True)
class ResourceIdentity:
    name: str
    owner: str
    sentinel: str


@dataclass(frozen=True)
class RoleTestEnvironment:
    config: BootstrapConfig
    databases: tuple[ResourceIdentity, ...]
    roles: tuple[ResourceIdentity, ...]


def _admin_connection_config() -> tuple[str, int, str, str, str]:
    if os.environ.get("RUN_POSTGRES_ROLE_TESTS") != "1":
        pytest.skip("PostgreSQL role gate requires RUN_POSTGRES_ROLE_TESTS=1")
    if admin_url := os.environ.get("POSTGRES_ADMIN_DATABASE_URL"):
        parsed = make_url(admin_url)
        if not parsed.drivername.startswith("postgresql"):
            pytest.fail("POSTGRES_ADMIN_DATABASE_URL must point to PostgreSQL")
        return (
            parsed.host or "127.0.0.1",
            parsed.port or 5432,
            parsed.database or "postgres",
            parsed.username or "",
            parsed.password or "",
        )
    values = (
        os.environ.get("POSTGRES_ADMIN_HOST", ""),
        int(os.environ.get("POSTGRES_ADMIN_PORT", "5432")),
        os.environ.get("POSTGRES_ADMIN_DB", ""),
        os.environ.get("POSTGRES_ADMIN_USER", ""),
        os.environ.get("POSTGRES_ADMIN_PASSWORD", ""),
    )
    if not all((values[0], values[2], values[3], values[4])):
        pytest.fail("PostgreSQL role gate requires administration connection settings")
    return values


def _admin_connect(config: BootstrapConfig, database: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=database,
        user=config.admin_user,
        password=config.admin_password,
        connect_timeout=10,
        autocommit=True,
    )


def _new_sentinel(resource_kind: str) -> str:
    return f"fcr-test-{resource_kind}:{secrets.token_hex(32)}"


def _stamp_role_test_environment(config: BootstrapConfig) -> RoleTestEnvironment:
    databases = tuple(
        ResourceIdentity(name=name, owner=config.owner_user, sentinel=_new_sentinel("database")) for name in (config.application_database, config.test_database)
    )
    roles = tuple(ResourceIdentity(name=name, owner=config.admin_user, sentinel=_new_sentinel("role")) for name in (config.runtime_user, config.owner_user))
    with _admin_connect(config, config.admin_database) as connection:
        with connection.cursor() as cursor:
            for identity in databases:
                cursor.execute(
                    "SELECT datname, pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
                    (identity.name,),
                )
                if cursor.fetchone() != (identity.name, identity.owner):
                    pytest.fail("temporary PostgreSQL database ownership verification failed before sentinel write")
                cursor.execute(
                    sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                        sql.Identifier(identity.name),
                        sql.Literal(identity.sentinel),
                    )
                )
            for identity in roles:
                cursor.execute("SELECT rolname, current_user FROM pg_roles WHERE rolname = %s", (identity.name,))
                if cursor.fetchone() != (identity.name, identity.owner):
                    pytest.fail("temporary PostgreSQL role ownership verification failed before sentinel write")
                cursor.execute(
                    sql.SQL("COMMENT ON ROLE {} IS {}").format(
                        sql.Identifier(identity.name),
                        sql.Literal(identity.sentinel),
                    )
                )
    return RoleTestEnvironment(config=config, databases=databases, roles=roles)


def _verify_cleanup_identities(
    cursor: psycopg.Cursor[tuple[object, ...]],
    environment: RoleTestEnvironment,
) -> None:
    for identity in environment.databases:
        cursor.execute(
            "SELECT datname, pg_get_userbyid(datdba), shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
            (identity.name,),
        )
        if cursor.fetchone() != (identity.name, identity.owner, identity.sentinel):
            pytest.fail("refusing to drop temporary PostgreSQL database: identity verification failed")
    for identity in environment.roles:
        cursor.execute(
            "SELECT rolname, current_user, shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname = %s",
            (identity.name,),
        )
        if cursor.fetchone() != (identity.name, identity.owner, identity.sentinel):
            pytest.fail("refusing to drop temporary PostgreSQL role: identity verification failed")


def _cleanup_role_test_environment(environment: RoleTestEnvironment) -> None:
    config = environment.config
    with _admin_connect(config, config.admin_database) as connection:
        with connection.cursor() as cursor:
            _verify_cleanup_identities(cursor, environment)
            for identity in environment.databases:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                    (identity.name,),
                )
                cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(identity.name)))
            for identity in environment.roles:
                cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(identity.name)))


def _create_sentinel_roles(config: BootstrapConfig, *role_names: str) -> tuple[ResourceIdentity, ...]:
    identities = tuple(ResourceIdentity(name=name, owner=config.admin_user, sentinel=_new_sentinel("role")) for name in role_names)
    with _admin_connect(config, config.admin_database) as connection:
        with connection.cursor() as cursor:
            for identity in identities:
                cursor.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(identity.name)))
                cursor.execute(
                    sql.SQL("COMMENT ON ROLE {} IS {}").format(
                        sql.Identifier(identity.name),
                        sql.Literal(identity.sentinel),
                    )
                )
    return identities


def _drop_sentinel_roles(config: BootstrapConfig, identities: tuple[ResourceIdentity, ...]) -> None:
    environment = RoleTestEnvironment(config=config, databases=(), roles=identities)
    with _admin_connect(config, config.admin_database) as connection:
        with connection.cursor() as cursor:
            _verify_cleanup_identities(cursor, environment)
            for identity in identities:
                cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(identity.name)))


def test_cleanup_fails_closed_before_any_drop_when_role_identity_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BootstrapConfig(
        host="127.0.0.1",
        port=5432,
        admin_database="postgres",
        admin_user="test_admin",
        admin_password="not-used",
        owner_user="test_owner",
        owner_password="not-used",
        runtime_user="test_runtime",
        runtime_password="not-used",
        application_database="test_application",
        test_database="test_database",
    )
    database = ResourceIdentity(name=config.application_database, owner=config.owner_user, sentinel="database-sentinel")
    role = ResourceIdentity(name=config.runtime_user, owner=config.admin_user, sentinel="role-sentinel")
    environment = RoleTestEnvironment(config=config, databases=(database,), roles=(role,))

    class FakeCursor:
        def __init__(self) -> None:
            self.statements: list[object] = []
            self.results = [
                (database.name, database.owner, database.sentinel),
                (role.name, role.owner, "wrong-sentinel"),
            ]

        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: object, _parameters: object = None) -> None:
            self.statements.append(statement)

        def fetchone(self) -> tuple[object, ...]:
            return self.results.pop(0)

    class FakeConnection:
        def __init__(self, cursor: FakeCursor) -> None:
            self._cursor = cursor

        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> FakeCursor:
            return self._cursor

    cursor = FakeCursor()
    monkeypatch.setattr(sys.modules[__name__], "_admin_connect", lambda *_args: FakeConnection(cursor))

    with pytest.raises(pytest.fail.Exception, match="role: identity verification failed"):
        _cleanup_role_test_environment(environment)

    assert len(cursor.statements) == 2


@pytest.fixture
def role_test_environment() -> Iterator[RoleTestEnvironment]:
    host, port, admin_database, admin_user, admin_password = _admin_connection_config()
    suffix = uuid.uuid4().hex[:12]
    config = BootstrapConfig(
        host=host,
        port=port,
        admin_database=admin_database,
        admin_user=admin_user,
        admin_password=admin_password,
        owner_user=f"fcr_owner_{suffix}",
        owner_password=f"Owner:{uuid.uuid4().hex}!",
        runtime_user=f"fcr_runtime_{suffix}",
        runtime_password=f"Runtime:{uuid.uuid4().hex}!",
        application_database=f"fcr_roles_{suffix}",
        test_database=f"fcr_roles_test_{suffix}",
    )
    bootstrap_database_roles(config)
    environment = _stamp_role_test_environment(config)
    try:
        yield environment
    finally:
        _cleanup_role_test_environment(environment)


def test_host_backup_restore_failure_cleans_exact_temporary_resources(
    role_test_environment: RoleTestEnvironment,
    tmp_path: Path,
) -> None:
    config = role_test_environment.config
    real_pg_restore = shutil.which("pg_restore")
    if real_pg_restore is None:
        pytest.fail("PostgreSQL role gate requires pg_restore")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_pg_restore = fake_bin / "pg_restore"
    fake_pg_restore.write_text("#!/bin/sh\nexit 86\n", encoding="utf-8")
    fake_pg_restore.chmod(0o700)
    temporary_root = tmp_path / "tmp"
    temporary_root.mkdir(mode=0o700)
    storage_root = tmp_path / "storage"
    package_root = tmp_path / "packages"

    def matching_temporary_databases() -> set[str]:
        with _admin_connect(config, config.admin_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT datname FROM pg_database "
                    "WHERE datname LIKE 'fcr_host_backup%' "
                    "OR datname LIKE 'fcr_host_restore%'"
                )
                return {str(row[0]) for row in cursor.fetchall()}

    before_databases = matching_temporary_databases()
    owner_secret = config.owner_password
    runtime_secret = config.runtime_password
    result = subprocess.run(
        ["sh", "scripts/postgres-host-backup-restore-smoke.sh"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TMPDIR": str(temporary_root),
            "LC_ALL": "C",
            "LANG": "C",
            "POSTGRES_ADMIN_HOST": config.host,
            "POSTGRES_ADMIN_PORT": str(config.port),
            "POSTGRES_ADMIN_USER": config.admin_user,
            "POSTGRES_ADMIN_PASSWORD": config.admin_password,
            "POSTGRES_ADMIN_DB": config.admin_database,
            "POSTGRES_OWNER_USER": config.owner_user,
            "POSTGRES_OWNER_PASSWORD": owner_secret,
            "POSTGRES_APP_USER": config.runtime_user,
            "POSTGRES_APP_PASSWORD": runtime_secret,
            "POSTGRES_APP_DB": config.application_database,
            "POSTGRES_TEST_DB": config.test_database,
            "DATABASE_HOST": config.host,
            "DATABASE_PORT": str(config.port),
            "DATABASE_NAME": config.application_database,
            "DATABASE_USER": config.runtime_user,
            "DATABASE_PASSWORD": runtime_secret,
            "STORAGE_ROOT": str(storage_root),
            "PACKAGE_ROOT": str(package_root),
            "WRITE_GUARD_SESSION_SECRET": secrets.token_urlsafe(32),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 86
    assert matching_temporary_databases() == before_databases
    assert not list(temporary_root.glob("fcr-host-backup-restore.*"))
    assert not list((storage_root / "files").iterdir())
    combined_output = result.stdout + result.stderr
    assert owner_secret not in combined_output
    assert runtime_secret not in combined_output


def _run_owner_alembic(config: BootstrapConfig, tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": "",
            "DATABASE_HOST": config.host,
            "DATABASE_PORT": str(config.port),
            "DATABASE_NAME": config.application_database,
            "DATABASE_USER": config.owner_user,
            "DATABASE_PASSWORD": config.owner_password,
            "DATABASE_CONNECT_TIMEOUT_SECONDS": "10",
            "DATABASE_MIGRATION_STATEMENT_TIMEOUT_MS": "300000",
            "STORAGE_ROOT": str(tmp_path / "storage"),
            "PACKAGE_ROOT": str(tmp_path / "packages"),
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, "owner Alembic migration failed"


def _connect_as(
    config: BootstrapConfig,
    user: str,
    password: str,
) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.application_database,
        user=user,
        password=password,
        connect_timeout=10,
    )


def test_runtime_grants_roll_back_when_a_later_statement_fails(
    role_test_environment: RoleTestEnvironment,
) -> None:
    config = role_test_environment.config
    probe_table = f"grant_atomicity_{uuid.uuid4().hex[:12]}"
    with _connect_as(config, config.owner_user, config.owner_password) as owner_connection:
        with owner_connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE TABLE public.{} (id integer PRIMARY KEY)").format(
                    sql.Identifier(probe_table)
                )
            )
        owner_connection.commit()

    grant_config = RuntimeGrantConfig(
        host=config.host,
        port=config.port,
        database=config.application_database,
        owner_user=config.owner_user,
        owner_password=config.owner_password,
        runtime_user=config.runtime_user,
    )
    with pytest.raises(psycopg.errors.UndefinedTable):
        grant_runtime_role(grant_config)

    with _admin_connect(config, config.application_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')",
                (config.runtime_user, f"public.{probe_table}"),
            )
            assert cursor.fetchone() == (False,)


def test_first_migration_cycle_revokes_public_function_execute_and_admin_database_access(
    role_test_environment: RoleTestEnvironment,
    tmp_path: Path,
) -> None:
    config = role_test_environment.config
    bootstrap_database_roles(config)
    _run_owner_alembic(config, tmp_path)
    with _connect_as(config, config.owner_user, config.owner_password) as owner_connection:
        with owner_connection.cursor() as cursor:
            cursor.execute("CREATE FUNCTION public.owner_migration_function() RETURNS integer LANGUAGE sql AS 'SELECT 1'")
        owner_connection.commit()
    grant_runtime_role(
        RuntimeGrantConfig(
            host=config.host,
            port=config.port,
            database=config.application_database,
            owner_user=config.owner_user,
            owner_password=config.owner_password,
            runtime_user=config.runtime_user,
        )
    )

    with _admin_connect(config, config.application_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT has_function_privilege(%s, 'public.owner_migration_function()', 'EXECUTE')",
                (config.runtime_user,),
            )
            assert cursor.fetchone() == (False,)
            cursor.execute(
                "SELECT count(*) FROM pg_proc function "
                "CROSS JOIN LATERAL aclexplode(coalesce(function.proacl, acldefault('f', function.proowner))) acl "
                "WHERE function.pronamespace = 'public'::regnamespace "
                "AND acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'"
            )
            assert cursor.fetchone() == (0,)

    with _admin_connect(config, config.admin_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT has_database_privilege(%s, %s, 'CONNECT'), has_database_privilege(%s, %s, 'TEMP')",
                (config.runtime_user, config.admin_database, config.runtime_user, config.admin_database),
            )
            assert cursor.fetchone() == (False, False)


def test_runtime_default_privileges_replace_historical_owner_grants(
    role_test_environment: RoleTestEnvironment,
    tmp_path: Path,
) -> None:
    config = role_test_environment.config
    _run_owner_alembic(config, tmp_path)
    runtime = sql.Identifier(config.runtime_user)
    with _connect_as(config, config.owner_user, config.owner_password) as owner_connection:
        with owner_connection.cursor() as cursor:
            cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON TABLES TO {}").format(runtime))
            cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES TO {}").format(runtime))
            cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON FUNCTIONS TO {}").format(runtime))
        owner_connection.commit()

    grant_runtime_role(
        RuntimeGrantConfig(
            host=config.host,
            port=config.port,
            database=config.application_database,
            owner_user=config.owner_user,
            owner_password=config.owner_password,
            runtime_user=config.runtime_user,
        )
    )

    suffix = uuid.uuid4().hex[:12]
    table_name = f"owner_default_table_{suffix}"
    sequence_name = f"owner_default_sequence_{suffix}"
    function_name = f"owner_default_function_{suffix}"
    with _connect_as(config, config.owner_user, config.owner_password) as owner_connection:
        with owner_connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE TABLE public.{} (id bigint PRIMARY KEY, value text NOT NULL)").format(sql.Identifier(table_name)))
            cursor.execute(sql.SQL("CREATE SEQUENCE public.{}").format(sql.Identifier(sequence_name)))
            cursor.execute(sql.SQL("CREATE FUNCTION public.{}() RETURNS integer LANGUAGE sql AS 'SELECT 1'").format(sql.Identifier(function_name)))
        owner_connection.commit()

    table_identity = f"public.{table_name}"
    sequence_identity = f"public.{sequence_name}"
    function_identity = f"public.{function_name}()"
    with _connect_as(config, config.runtime_user, config.runtime_password) as runtime_connection:
        with runtime_connection.cursor() as cursor:
            cursor.execute(
                "SELECT "
                "has_table_privilege(current_user, %s, 'SELECT'), "
                "has_table_privilege(current_user, %s, 'INSERT'), "
                "has_table_privilege(current_user, %s, 'UPDATE'), "
                "has_table_privilege(current_user, %s, 'DELETE'), "
                "has_table_privilege(current_user, %s, 'TRUNCATE'), "
                "has_table_privilege(current_user, %s, 'REFERENCES'), "
                "has_table_privilege(current_user, %s, 'TRIGGER')",
                (table_identity,) * 7,
            )
            assert cursor.fetchone() == (True, True, True, True, False, False, False)
            cursor.execute(
                "SELECT "
                "has_sequence_privilege(current_user, %s, 'USAGE'), "
                "has_sequence_privilege(current_user, %s, 'SELECT'), "
                "has_sequence_privilege(current_user, %s, 'UPDATE')",
                (sequence_identity,) * 3,
            )
            assert cursor.fetchone() == (True, True, False)
            cursor.execute(
                "SELECT has_function_privilege(current_user, %s, 'EXECUTE')",
                (function_identity,),
            )
            assert cursor.fetchone() == (False,)

            cursor.execute(sql.SQL("INSERT INTO public.{} (id, value) VALUES (1, 'created')").format(sql.Identifier(table_name)))
            cursor.execute(sql.SQL("SELECT value FROM public.{} WHERE id = 1").format(sql.Identifier(table_name)))
            assert cursor.fetchone() == ("created",)
            cursor.execute(sql.SQL("UPDATE public.{} SET value = 'updated' WHERE id = 1").format(sql.Identifier(table_name)))
            cursor.execute(sql.SQL("DELETE FROM public.{} WHERE id = 1").format(sql.Identifier(table_name)))
            cursor.execute("SELECT nextval(%s::regclass)", (sequence_identity,))
            assert cursor.fetchone() is not None
            cursor.execute(sql.SQL("SELECT last_value FROM public.{}").format(sql.Identifier(sequence_name)))
            assert cursor.fetchone() is not None
        runtime_connection.rollback()

        with pytest.raises(psycopg.errors.InsufficientPrivilege) as error:
            with runtime_connection.cursor() as cursor:
                cursor.execute(sql.SQL("SELECT public.{}()").format(sql.Identifier(function_name)))
        assert error.value.sqlstate == "42501"
        runtime_connection.rollback()


def test_bootstrap_scram_roles_authenticate_with_raw_passwords(
    role_test_environment: RoleTestEnvironment,
) -> None:
    config = role_test_environment.config
    credentials = (
        (config.owner_user, config.owner_password),
        (config.runtime_user, config.runtime_password),
    )
    for role_name, raw_password in credentials:
        with _connect_as(config, role_name, raw_password) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_user")
                assert cursor.fetchone() == (role_name,)

    with _admin_connect(config, config.admin_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolname, rolpassword LIKE 'SCRAM-SHA-256$%%' FROM pg_authid WHERE rolname IN (%s, %s) ORDER BY rolname",
                (config.owner_user, config.runtime_user),
            )
            assert cursor.fetchall() == sorted([(config.owner_user, True), (config.runtime_user, True)])


def test_minimal_runtime_role_can_insert_attributed_operation_log(
    role_test_environment: RoleTestEnvironment,
    tmp_path: Path,
) -> None:
    config = role_test_environment.config
    _run_owner_alembic(config, tmp_path)
    grant_runtime_role(
        RuntimeGrantConfig(
            host=config.host,
            port=config.port,
            database=config.application_database,
            owner_user=config.owner_user,
            owner_password=config.owner_password,
            runtime_user=config.runtime_user,
        )
    )
    request_id = f"runtime_audit_{uuid.uuid4().hex}"
    idempotency_key_hash = "a" * 64

    with _connect_as(config, config.runtime_user, config.runtime_password) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_user, pg_get_userbyid(relation.relowner), "
                "has_table_privilege(current_user, 'public.operation_logs', 'SELECT'), "
                "has_table_privilege(current_user, 'public.operation_logs', 'INSERT'), "
                "has_table_privilege(current_user, 'public.operation_logs', 'UPDATE'), "
                "has_table_privilege(current_user, 'public.operation_logs', 'DELETE'), "
                "has_table_privilege(current_user, 'public.operation_logs', 'TRUNCATE'), "
                "has_sequence_privilege(current_user, 'public.operation_logs_id_seq', 'USAGE'), "
                "has_sequence_privilege(current_user, 'public.operation_logs_id_seq', 'UPDATE'), "
                "has_schema_privilege(current_user, 'public', 'CREATE') "
                "FROM pg_class relation WHERE relation.oid = 'public.operation_logs'::regclass"
            )
            assert cursor.fetchone() == (
                config.runtime_user,
                config.owner_user,
                True,
                True,
                False,
                False,
                False,
                True,
                False,
                False,
            )
            cursor.execute(
                """
                INSERT INTO operation_logs (
                    request_id, entry_source, command_type, capability,
                    principal_kind, principal_id, client_ip, user_agent,
                    idempotency_key_hash, resource_type, resource_id,
                    result, error_code, failure_stage, created_at
                ) VALUES (
                    %s, 'review', 'UpdateReviewIssue', 'review.issue.update',
                    'system', %s, '127.0.0.1', 'role-gate',
                    %s, 'review_issue', %s,
                    'ok', NULL, NULL, now()
                )
                RETURNING id
                """,
                (request_id, "role-gate-principal", idempotency_key_hash, "iss_role_gate"),
            )
            assert cursor.fetchone() is not None
        connection.commit()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT command_type, principal_kind, principal_id, idempotency_key_hash, "
                "resource_type, resource_id, result "
                "FROM operation_logs WHERE request_id = %s",
                (request_id,),
            )
            assert cursor.fetchone() == (
                "UpdateReviewIssue",
                "system",
                "role-gate-principal",
                idempotency_key_hash,
                "review_issue",
                "iss_role_gate",
                "ok",
            )


def test_bootstrap_removes_role_memberships_in_both_directions(
    role_test_environment: RoleTestEnvironment,
) -> None:
    config = role_test_environment.config
    parent_role = f"fcr_parent_{uuid.uuid4().hex[:12]}"
    member_role = f"fcr_member_{uuid.uuid4().hex[:12]}"
    bootstrap_database_roles(config)
    temporary_roles = _create_sentinel_roles(config, parent_role, member_role)
    try:
        with _admin_connect(config, config.admin_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(parent_role),
                        sql.Identifier(config.runtime_user),
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(config.owner_user),
                        sql.Identifier(member_role),
                    )
                )

        bootstrap_database_roles(config)

        with _admin_connect(config, config.admin_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_auth_members membership "
                    "JOIN pg_roles parent ON parent.oid = membership.roleid "
                    "JOIN pg_roles member ON member.oid = membership.member "
                    "WHERE parent.rolname IN (%s, %s) OR member.rolname IN (%s, %s)",
                    (config.owner_user, config.runtime_user, config.owner_user, config.runtime_user),
                )
                assert cursor.fetchone() == (0,)
    finally:
        _drop_sentinel_roles(config, tuple(reversed(temporary_roles)))


def test_bootstrap_quiesces_active_downstream_set_role_session(
    role_test_environment: RoleTestEnvironment,
) -> None:
    config = role_test_environment.config
    relay_role = f"fcr_relay_{uuid.uuid4().hex[:12]}"
    login_role = f"fcr_downstream_{uuid.uuid4().hex[:12]}"
    login_password = f"Downstream:{uuid.uuid4().hex}!"
    relay_identity = _create_sentinel_roles(config, relay_role)[0]
    login_identity = ResourceIdentity(login_role, config.admin_user, _new_sentinel("role"))
    downstream_connection: psycopg.Connection[tuple[object, ...]] | None = None
    with _admin_connect(config, config.admin_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(login_role),
                    sql.Literal(login_password),
                )
            )
            cursor.execute(
                sql.SQL("COMMENT ON ROLE {} IS {}").format(
                    sql.Identifier(login_role),
                    sql.Literal(login_identity.sentinel),
                )
            )
            cursor.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(config.runtime_user),
                    sql.Identifier(relay_role),
                )
            )
            cursor.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(relay_role),
                    sql.Identifier(login_role),
                )
            )
    try:
        downstream_connection = _connect_as(config, login_role, login_password)
        with downstream_connection.cursor() as cursor:
            cursor.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(config.runtime_user)))
            cursor.execute("SELECT session_user, current_user")
            assert cursor.fetchone() == (login_role, config.runtime_user)

        bootstrap_database_roles(config)

        with pytest.raises(psycopg.OperationalError):
            with downstream_connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        with _admin_connect(config, config.admin_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_auth_members membership "
                    "JOIN pg_roles parent ON parent.oid = membership.roleid "
                    "JOIN pg_roles member ON member.oid = membership.member "
                    "WHERE parent.rolname = %s AND member.rolname = %s",
                    (config.runtime_user, relay_role),
                )
                assert cursor.fetchone() == (0,)
    finally:
        if downstream_connection is not None:
            downstream_connection.close()
        with _admin_connect(config, config.admin_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE usename = %s AND pid <> pg_backend_pid()",
                    (login_role,),
                )
                cursor.execute(
                    sql.SQL("REVOKE {} FROM {}").format(
                        sql.Identifier(relay_role),
                        sql.Identifier(login_role),
                    )
                )
        _drop_sentinel_roles(config, (login_identity, relay_identity))


def test_bootstrap_can_hold_runtime_role_nologin_for_migration_window(
    role_test_environment: RoleTestEnvironment,
) -> None:
    config = role_test_environment.config
    try:
        bootstrap_database_roles(config, keep_runtime_quiesced=True)
        with _admin_connect(config, config.admin_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname IN (%s, %s)",
                    (config.owner_user, config.runtime_user),
                )
                assert {str(name): bool(can_login) for name, can_login in cursor.fetchall()} == {
                    config.owner_user: True,
                    config.runtime_user: False,
                }
    finally:
        bootstrap_database_roles(config)


def test_standalone_bootstrap_cannot_enter_active_migration_sequence(
    role_test_environment: RoleTestEnvironment,
) -> None:
    from backend.scripts.bootstrap_database_roles import MIGRATION_SEQUENCE_ADVISORY_LOCK_KEY

    config = role_test_environment.config
    with _admin_connect(config, config.admin_database) as lock_connection:
        with lock_connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_SEQUENCE_ADVISORY_LOCK_KEY,))
        try:
            with pytest.raises(RuntimeError, match="migration sequence is already running"):
                bootstrap_database_roles(config)
        finally:
            with lock_connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_SEQUENCE_ADVISORY_LOCK_KEY,))
                assert cursor.fetchone() == (True,)


def test_bootstrap_rejects_owner_name_change_on_populated_volume(
    role_test_environment: RoleTestEnvironment,
) -> None:
    config = role_test_environment.config
    previous_owner = f"fcr_previous_owner_{uuid.uuid4().hex[:12]}"
    identities = _create_sentinel_roles(config, previous_owner)
    try:
        with _admin_connect(config, config.admin_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT rolname, rolpassword FROM pg_authid WHERE rolname IN (%s, %s) ORDER BY rolname",
                    (config.owner_user, config.runtime_user),
                )
                role_passwords_before = cursor.fetchall()
                cursor.execute(
                    "SELECT parent.rolname, member.rolname FROM pg_auth_members membership "
                    "JOIN pg_roles parent ON parent.oid = membership.roleid "
                    "JOIN pg_roles member ON member.oid = membership.member "
                    "WHERE parent.rolname IN (%s, %s) OR member.rolname IN (%s, %s) "
                    "ORDER BY parent.rolname, member.rolname",
                    (config.owner_user, config.runtime_user, config.owner_user, config.runtime_user),
                )
                memberships_before = cursor.fetchall()
                cursor.execute(
                    sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                        sql.Identifier(config.application_database),
                        sql.Identifier(previous_owner),
                    )
                )

        with pytest.raises(RuntimeError, match="POSTGRES_OWNER_USER must remain stable"):
            bootstrap_database_roles(config)
        with _admin_connect(config, config.admin_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT rolname, rolpassword FROM pg_authid WHERE rolname IN (%s, %s) ORDER BY rolname",
                    (config.owner_user, config.runtime_user),
                )
                assert cursor.fetchall() == role_passwords_before
                cursor.execute(
                    "SELECT parent.rolname, member.rolname FROM pg_auth_members membership "
                    "JOIN pg_roles parent ON parent.oid = membership.roleid "
                    "JOIN pg_roles member ON member.oid = membership.member "
                    "WHERE parent.rolname IN (%s, %s) OR member.rolname IN (%s, %s) "
                    "ORDER BY parent.rolname, member.rolname",
                    (config.owner_user, config.runtime_user, config.owner_user, config.runtime_user),
                )
                assert cursor.fetchall() == memberships_before
    finally:
        with _admin_connect(config, config.admin_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                        sql.Identifier(config.application_database),
                        sql.Identifier(config.owner_user),
                    )
                )
        _drop_sentinel_roles(config, identities)


def test_bootstrap_rejects_independent_composite_type_owner_before_role_mutation(
    role_test_environment: RoleTestEnvironment,
) -> None:
    config = role_test_environment.config
    previous_owner = f"fcr_composite_owner_{uuid.uuid4().hex[:12]}"
    type_name = f"third_party_composite_{uuid.uuid4().hex[:12]}"
    identities = _create_sentinel_roles(config, previous_owner)
    try:
        with _admin_connect(config, config.application_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE TYPE public.{} AS (value text)").format(sql.Identifier(type_name)))
                cursor.execute(
                    sql.SQL("ALTER TYPE public.{} OWNER TO {}").format(
                        sql.Identifier(type_name),
                        sql.Identifier(previous_owner),
                    )
                )
        with _admin_connect(config, config.admin_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT rolname, rolpassword, rolcanlogin FROM pg_authid "
                    "WHERE rolname IN (%s, %s) ORDER BY rolname",
                    (config.owner_user, config.runtime_user),
                )
                before = cursor.fetchall()

        with pytest.raises(RuntimeError, match="public schema contains objects owned by an unsupported role"):
            bootstrap_database_roles(config)

        with _admin_connect(config, config.admin_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT rolname, rolpassword, rolcanlogin FROM pg_authid "
                    "WHERE rolname IN (%s, %s) ORDER BY rolname",
                    (config.owner_user, config.runtime_user),
                )
                assert cursor.fetchall() == before
    finally:
        with _admin_connect(config, config.application_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP TYPE IF EXISTS public.{}").format(sql.Identifier(type_name)))
        _drop_sentinel_roles(config, identities)


def test_bootstrap_rejects_public_extension_owner_before_role_mutation(
    role_test_environment: RoleTestEnvironment,
) -> None:
    config = role_test_environment.config
    previous_owner = f"fcr_extension_owner_{uuid.uuid4().hex[:12]}"
    identities = _create_sentinel_roles(config, previous_owner)
    extension_name = "hstore"
    try:
        with _admin_connect(config, config.application_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                        sql.Identifier(config.application_database),
                        sql.Identifier(previous_owner),
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT CREATE ON SCHEMA public TO {}").format(sql.Identifier(previous_owner))
                )
                cursor.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(previous_owner)))
                cursor.execute(sql.SQL("CREATE EXTENSION {} WITH SCHEMA public").format(sql.Identifier(extension_name)))
                cursor.execute("RESET ROLE")
        with _admin_connect(config, config.admin_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT rolname, rolpassword, rolcanlogin FROM pg_authid "
                    "WHERE rolname IN (%s, %s) ORDER BY rolname",
                    (config.owner_user, config.runtime_user),
                )
                before = cursor.fetchall()

        with pytest.raises(RuntimeError, match="public schema contains objects owned by an unsupported role"):
            bootstrap_database_roles(config)

        with _admin_connect(config, config.admin_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT rolname, rolpassword, rolcanlogin FROM pg_authid "
                    "WHERE rolname IN (%s, %s) ORDER BY rolname",
                    (config.owner_user, config.runtime_user),
                )
                assert cursor.fetchall() == before
    finally:
        with _admin_connect(config, config.application_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP EXTENSION IF EXISTS {}").format(sql.Identifier(extension_name)))
                cursor.execute(
                    sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(sql.Identifier(previous_owner))
                )
        with _admin_connect(config, config.admin_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(
                        sql.Identifier(config.application_database),
                        sql.Identifier(previous_owner),
                    )
                )
        _drop_sentinel_roles(config, identities)


def test_bootstrap_failure_after_quiesce_keeps_managed_roles_nologin(
    role_test_environment: RoleTestEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.scripts.bootstrap_database_roles as bootstrap_mod

    config = role_test_environment.config
    original_transfer = bootstrap_mod._transfer_database_objects
    calls = 0

    def fail_second_transfer(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("forced second database transfer failure")
        original_transfer(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(bootstrap_mod, "_transfer_database_objects", fail_second_transfer)
    try:
        with pytest.raises(RuntimeError, match="forced second database transfer failure"):
            bootstrap_mod.bootstrap_database_roles(config)
        with _admin_connect(config, config.admin_database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT rolcanlogin FROM pg_roles WHERE rolname IN (%s, %s) ORDER BY rolname",
                    (config.owner_user, config.runtime_user),
                )
                assert cursor.fetchall() == [(False,), (False,)]
    finally:
        monkeypatch.setattr(bootstrap_mod, "_transfer_database_objects", original_transfer)
        bootstrap_mod.bootstrap_database_roles(config)

    with _admin_connect(config, config.admin_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolcanlogin FROM pg_roles WHERE rolname IN (%s, %s) ORDER BY rolname",
                (config.owner_user, config.runtime_user),
            )
            assert cursor.fetchall() == [(True,), (True,)]


def test_bootstrap_is_idempotent_transfers_ownership_and_enforces_runtime_dml_only(
    role_test_environment: RoleTestEnvironment,
    tmp_path: Path,
) -> None:
    config = role_test_environment.config
    bootstrap_database_roles(config)

    with _admin_connect(config, config.admin_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                    sql.Identifier(config.application_database),
                    sql.Identifier(config.runtime_user),
                )
            )
    with _admin_connect(config, config.application_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(config.runtime_user)))
    with _connect_as(config, config.runtime_user, config.runtime_password) as runtime_connection:
        with runtime_connection.cursor() as cursor:
            cursor.execute("CREATE TABLE public.legacy_runtime_owned (id integer PRIMARY KEY, value text NOT NULL)")
            cursor.execute("CREATE SEQUENCE public.legacy_runtime_sequence")
            cursor.execute("CREATE VIEW public.legacy_runtime_view AS SELECT id, value FROM public.legacy_runtime_owned")
            cursor.execute("CREATE FUNCTION public.legacy_runtime_function() RETURNS integer LANGUAGE sql AS 'SELECT 1'")
            cursor.execute("CREATE DOMAIN public.legacy_runtime_domain AS text CHECK (VALUE <> '')")
        runtime_connection.commit()

    bootstrap_database_roles(config)
    _run_owner_alembic(config, tmp_path)
    grant_config = RuntimeGrantConfig(
        host=config.host,
        port=config.port,
        database=config.application_database,
        owner_user=config.owner_user,
        owner_password=config.owner_password,
        runtime_user=config.runtime_user,
    )
    grant_runtime_role(grant_config)
    bootstrap_database_roles(config)
    _run_owner_alembic(config, tmp_path)
    grant_runtime_role(grant_config)

    with _admin_connect(config, config.admin_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname IN (%s, %s) ORDER BY rolname",
                (config.owner_user, config.runtime_user),
            )
            assert cursor.fetchall() == [(False, False, False, False, False, False)] * 2
            cursor.execute(
                "SELECT count(*) FROM pg_auth_members membership JOIN pg_roles member ON member.oid = membership.member WHERE member.rolname IN (%s, %s)",
                (config.owner_user, config.runtime_user),
            )
            assert cursor.fetchone() == (0,)
            cursor.execute(
                "SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND pg_get_userbyid(proowner) <> %s",
                (config.owner_user,),
            )
            assert cursor.fetchone() == (0,)
            cursor.execute(
                "SELECT count(*) FROM pg_type type "
                "LEFT JOIN pg_class relation ON relation.reltype = type.oid "
                "WHERE type.typnamespace = 'public'::regnamespace AND relation.oid IS NULL "
                "AND type.typtype IN ('c', 'd', 'e', 'r') AND pg_get_userbyid(type.typowner) <> %s",
                (config.owner_user,),
            )
            assert cursor.fetchone() == (0,)
            cursor.execute(
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
                (config.application_database,),
            )
            assert cursor.fetchone() == (config.owner_user,)

    with _admin_connect(config, config.application_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'")
            assert cursor.fetchone() == (config.owner_user,)
            cursor.execute(
                "SELECT count(*) FROM pg_class "
                "WHERE relnamespace = 'public'::regnamespace "
                "AND relkind IN ('r', 'p', 'S', 'v', 'm', 'f') "
                "AND pg_get_userbyid(relowner) <> %s",
                (config.owner_user,),
            )
            assert cursor.fetchone() == (0,)

    probe_id = f"prj_role_probe_{uuid.uuid4().hex}"
    with _connect_as(config, config.runtime_user, config.runtime_password) as runtime_connection:
        with runtime_connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO project_refs "
                "(id, project_code, project_name, source, description, lifecycle_status, lock_version, created_at, updated_at) "
                "VALUES (%s, %s, %s, 'local', %s, 'active', 1, now(), now())",
                (
                    probe_id,
                    f"ROLE_{uuid.uuid4().hex[:12]}",
                    "Runtime role probe",
                    "Runtime role probe description",
                ),
            )
            cursor.execute("SELECT project_name FROM project_refs WHERE id = %s", (probe_id,))
            assert cursor.fetchone() == ("Runtime role probe",)
            cursor.execute("UPDATE project_refs SET project_name = %s WHERE id = %s", ("Updated", probe_id))
            cursor.execute("DELETE FROM project_refs WHERE id = %s", (probe_id,))
        runtime_connection.rollback()

        with runtime_connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM public.alembic_version")
            assert cursor.fetchone() is not None
            cursor.execute("SELECT nextval('public.legacy_runtime_sequence')")
            assert cursor.fetchone() is not None
        runtime_connection.rollback()

        for statement in (
            "UPDATE public.alembic_version SET version_num = version_num",
            "SELECT setval('public.legacy_runtime_sequence', 1000)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as error:
                with runtime_connection.cursor() as cursor:
                    cursor.execute(statement)
            assert error.value.sqlstate == "42501"
            runtime_connection.rollback()

        for statement in (
            "CREATE TABLE public.runtime_ddl_probe (id integer)",
            "ALTER TABLE public.project_refs ADD COLUMN runtime_ddl_probe integer",
            "DROP TABLE public.project_refs",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as error:
                with runtime_connection.cursor() as cursor:
                    cursor.execute(statement)
            assert error.value.sqlstate == "42501"
            runtime_connection.rollback()

        with runtime_connection.cursor() as cursor:
            cursor.execute(
                "SELECT has_database_privilege(current_user, current_database(), 'CONNECT'), "
                "has_database_privilege(current_user, current_database(), 'CREATE'), "
                "has_database_privilege(current_user, current_database(), 'TEMP'), "
                "has_schema_privilege(current_user, 'public', 'USAGE'), "
                "has_schema_privilege(current_user, 'public', 'CREATE')"
            )
            assert cursor.fetchone() == (True, False, False, True, False)

    with _connect_as(config, config.owner_user, config.owner_password) as owner_connection:
        with owner_connection.cursor() as cursor:
            cursor.execute("CREATE TABLE public.owner_migration_probe (id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, value text NOT NULL)")
        owner_connection.commit()
    try:
        with _connect_as(config, config.runtime_user, config.runtime_password) as runtime_connection:
            with runtime_connection.cursor() as cursor:
                cursor.execute("INSERT INTO public.owner_migration_probe (value) VALUES ('created later') RETURNING id")
                assert cursor.fetchone() is not None
                cursor.execute("SELECT value FROM public.owner_migration_probe")
                assert cursor.fetchone() == ("created later",)
                cursor.execute("UPDATE public.owner_migration_probe SET value = 'updated later'")
                cursor.execute("DELETE FROM public.owner_migration_probe")
            runtime_connection.rollback()
    finally:
        with _connect_as(config, config.owner_user, config.owner_password) as owner_connection:
            with owner_connection.cursor() as cursor:
                cursor.execute("DROP TABLE public.owner_migration_probe")
            owner_connection.commit()
