#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import stat
from dataclasses import dataclass

import psycopg
from psycopg import sql

IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
SCRAM_SHA_256_VERIFIER_PATTERN = re.compile(
    r"SCRAM-SHA-256\$[1-9][0-9]*:[A-Za-z0-9+/]+={0,2}"
    r"\$[A-Za-z0-9+/]+={0,2}:[A-Za-z0-9+/]+={0,2}\Z"
)
BOOTSTRAP_ADVISORY_LOCK_KEY = 5064946991358885103
MIGRATION_SEQUENCE_ADVISORY_LOCK_KEY = 5064946991358885104
SECRET_FILE_MAX_BYTES = 64 * 1024


def require_identifier(name: str, value: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must be a valid unquoted PostgreSQL identifier")
    return value


def require_secret(name: str, value: str) -> str:
    if not value:
        raise ValueError(f"{name} is required")
    return value


def read_secret_from_environment(name: str) -> str:
    direct_value = os.environ.get(name, "")
    file_path = os.environ.get(f"{name}_FILE", "")
    if direct_value and file_path:
        raise ValueError(f"{name} and {name}_FILE are mutually exclusive")
    if not file_path:
        return require_secret(name, direct_value)

    descriptor = -1
    try:
        descriptor = os.open(file_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > SECRET_FILE_MAX_BYTES:
            raise ValueError(f"{name}_FILE must reference a non-empty bounded regular file")
        with os.fdopen(descriptor, "rb") as secret_file:
            descriptor = -1
            raw_value = secret_file.read(SECRET_FILE_MAX_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"{name}_FILE must reference a readable non-symlink regular file") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(raw_value) > SECRET_FILE_MAX_BYTES:
        raise ValueError(f"{name}_FILE exceeds the maximum supported size")
    if raw_value.endswith(b"\n"):
        raw_value = raw_value[:-1]
        if raw_value.endswith(b"\r"):
            raw_value = raw_value[:-1]
    if not raw_value or b"\x00" in raw_value or b"\r" in raw_value or b"\n" in raw_value:
        raise ValueError(f"{name}_FILE must contain exactly one non-empty secret value")
    try:
        return raw_value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name}_FILE must contain valid UTF-8") from exc


def require_port(name: str, value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return port


@dataclass(frozen=True)
class BootstrapConfig:
    host: str
    port: int
    admin_database: str
    admin_user: str
    admin_password: str
    owner_user: str
    owner_password: str
    runtime_user: str
    runtime_password: str
    application_database: str
    test_database: str

    @classmethod
    def from_environment(cls) -> BootstrapConfig:
        config = cls(
            host=require_secret("POSTGRES_ADMIN_HOST", os.environ.get("POSTGRES_ADMIN_HOST", "")),
            port=require_port("POSTGRES_ADMIN_PORT", os.environ.get("POSTGRES_ADMIN_PORT", "5432")),
            admin_database=require_identifier("POSTGRES_ADMIN_DB", os.environ.get("POSTGRES_ADMIN_DB", "")),
            admin_user=require_identifier("POSTGRES_ADMIN_USER", os.environ.get("POSTGRES_ADMIN_USER", "")),
            admin_password=read_secret_from_environment("POSTGRES_ADMIN_PASSWORD"),
            owner_user=require_identifier("POSTGRES_OWNER_USER", os.environ.get("POSTGRES_OWNER_USER", "")),
            owner_password=read_secret_from_environment("POSTGRES_OWNER_PASSWORD"),
            runtime_user=require_identifier("POSTGRES_APP_USER", os.environ.get("POSTGRES_APP_USER", "")),
            runtime_password=read_secret_from_environment("POSTGRES_APP_PASSWORD"),
            application_database=require_identifier("POSTGRES_APP_DB", os.environ.get("POSTGRES_APP_DB", "")),
            test_database=require_identifier("POSTGRES_TEST_DB", os.environ.get("POSTGRES_TEST_DB", "")),
        )
        if len({config.admin_user, config.owner_user, config.runtime_user}) != 3:
            raise ValueError("administration, owner, and runtime roles must be distinct")
        if len({config.admin_database, config.application_database, config.test_database}) != 3:
            raise ValueError("administration, application, and test databases must be distinct")
        return config


def _connect(config: BootstrapConfig, database: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=database,
        user=config.admin_user,
        password=config.admin_password,
        connect_timeout=10,
        options="-c statement_timeout=300000 -c lock_timeout=30000",
        autocommit=True,
    )


def _ensure_admin_connection(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        row = cursor.fetchone()
    if row is None or row[0] is not True:
        raise RuntimeError("database role bootstrap requires the PostgreSQL administration role")


def _ensure_login_role(
    connection: psycopg.Connection[tuple[object, ...]],
    role_name: str,
    password: str,
    *,
    login: bool = True,
) -> None:
    role = sql.Identifier(role_name)
    verifier = connection.pgconn.encrypt_password(
        password.encode(),
        role_name.encode(),
        b"scram-sha-256",
    ).decode("ascii")
    if not SCRAM_SHA_256_VERIFIER_PATTERN.fullmatch(verifier):
        raise RuntimeError("libpq did not return a PostgreSQL SCRAM-SHA-256 verifier")
    login_clause = sql.SQL("LOGIN") if login else sql.SQL("NOLOGIN")
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
        if cursor.fetchone() is None:
            cursor.execute(
                sql.SQL(
                    "CREATE ROLE {} WITH {} PASSWORD {} "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                ).format(role, login_clause, sql.Literal(verifier))
            )
        else:
            cursor.execute(
                sql.SQL(
                    "ALTER ROLE {} WITH {} PASSWORD {} "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                ).format(role, login_clause, sql.Literal(verifier))
            )
        cursor.execute(
            "SELECT parent.rolname "
            "FROM pg_auth_members membership "
            "JOIN pg_roles parent ON parent.oid = membership.roleid "
            "JOIN pg_roles member ON member.oid = membership.member "
            "WHERE member.rolname = %s",
            (role_name,),
        )
        for (parent_role,) in cursor.fetchall():
            cursor.execute(
                sql.SQL("REVOKE {} FROM {}").format(
                    sql.Identifier(str(parent_role)),
                    role,
                )
            )
        cursor.execute(
            "SELECT member.rolname "
            "FROM pg_auth_members membership "
            "JOIN pg_roles parent ON parent.oid = membership.roleid "
            "JOIN pg_roles member ON member.oid = membership.member "
            "WHERE parent.rolname = %s",
            (role_name,),
        )
        for (member_role,) in cursor.fetchall():
            cursor.execute(
                sql.SQL("REVOKE {} FROM {}").format(
                    role,
                    sql.Identifier(str(member_role)),
                )
            )


def _downstream_login_roles(
    connection: psycopg.Connection[tuple[object, ...]],
    role_names: tuple[str, str],
) -> tuple[str, ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            "WITH RECURSIVE downstream(member) AS ("
            "SELECT membership.member FROM pg_auth_members membership "
            "JOIN pg_roles parent ON parent.oid = membership.roleid "
            "WHERE parent.rolname = ANY(%s) "
            "UNION "
            "SELECT membership.member FROM pg_auth_members membership "
            "JOIN downstream parent_membership ON parent_membership.member = membership.roleid"
            ") "
            "SELECT DISTINCT member_role.rolname FROM downstream "
            "JOIN pg_roles member_role ON member_role.oid = downstream.member "
            "WHERE member_role.rolcanlogin",
            (list(role_names),),
        )
        return tuple(sorted(str(row[0]) for row in cursor.fetchall()))


def _quiesce_managed_roles(
    connection: psycopg.Connection[tuple[object, ...]],
    role_names: tuple[str, ...],
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_terminate_backend(pid, 10000) FROM pg_stat_activity "
            "WHERE usename = ANY(%s) AND pid <> pg_backend_pid()",
            (list(role_names),),
        )
        if any(row[0] is not True for row in cursor.fetchall()):
            raise RuntimeError("managed database role sessions could not be terminated")
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE usename = ANY(%s) AND pid <> pg_backend_pid()",
            (list(role_names),),
        )
        if cursor.fetchone() != (0,):
            raise RuntimeError("managed database role sessions remain active after termination")


def _activate_login_role(
    connection: psycopg.Connection[tuple[object, ...]],
    role_name: str,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("ALTER ROLE {} LOGIN").format(sql.Identifier(role_name)))


def _ensure_database(
    connection: psycopg.Connection[tuple[object, ...]],
    database_name: str,
    owner_user: str,
    runtime_user: str,
) -> None:
    database = sql.Identifier(database_name)
    owner = sql.Identifier(owner_user)
    runtime = sql.Identifier(runtime_user)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (database_name,))
        row = cursor.fetchone()
        if row is None:
            cursor.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(database, owner))
        else:
            existing_owner = str(row[0])
            if existing_owner not in {owner_user, runtime_user}:
                raise RuntimeError(
                    "existing database owner is incompatible; POSTGRES_OWNER_USER must remain stable "
                    "for a populated PostgreSQL volume"
                )
            cursor.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(database, owner))
        cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM PUBLIC").format(database))
        cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(database, runtime))
        cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, runtime))


def _public_object_owners(
    connection: psycopg.Connection[tuple[object, ...]],
) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT owner_name FROM ("
            "SELECT pg_get_userbyid(ns.nspowner) AS owner_name "
            "FROM pg_namespace ns WHERE ns.nspname = 'public' "
            "UNION ALL "
            "SELECT pg_get_userbyid(cls.relowner) AS owner_name "
            "FROM pg_class cls JOIN pg_namespace ns ON ns.oid = cls.relnamespace "
            "WHERE ns.nspname = 'public' "
            "UNION ALL "
            "SELECT pg_get_userbyid(proc.proowner) AS owner_name "
            "FROM pg_proc proc JOIN pg_namespace ns ON ns.oid = proc.pronamespace "
            "WHERE ns.nspname = 'public' "
            "UNION ALL "
            "SELECT pg_get_userbyid(typ.typowner) AS owner_name "
            "FROM pg_type typ JOIN pg_namespace ns ON ns.oid = typ.typnamespace "
            "WHERE ns.nspname = 'public' "
            "UNION ALL "
            "SELECT pg_get_userbyid(coll.collowner) AS owner_name "
            "FROM pg_collation coll JOIN pg_namespace ns ON ns.oid = coll.collnamespace "
            "WHERE ns.nspname = 'public' "
            "UNION ALL "
            "SELECT pg_get_userbyid(conv.conowner) AS owner_name "
            "FROM pg_conversion conv JOIN pg_namespace ns ON ns.oid = conv.connamespace "
            "WHERE ns.nspname = 'public' "
            "UNION ALL "
            "SELECT pg_get_userbyid(opr.oprowner) AS owner_name "
            "FROM pg_operator opr JOIN pg_namespace ns ON ns.oid = opr.oprnamespace "
            "WHERE ns.nspname = 'public' "
            "UNION ALL "
            "SELECT pg_get_userbyid(opc.opcowner) AS owner_name "
            "FROM pg_opclass opc JOIN pg_namespace ns ON ns.oid = opc.opcnamespace "
            "WHERE ns.nspname = 'public' "
            "UNION ALL "
            "SELECT pg_get_userbyid(opf.opfowner) AS owner_name "
            "FROM pg_opfamily opf JOIN pg_namespace ns ON ns.oid = opf.opfnamespace "
            "WHERE ns.nspname = 'public' "
            "UNION ALL "
            "SELECT pg_get_userbyid(cfg.cfgowner) AS owner_name "
            "FROM pg_ts_config cfg JOIN pg_namespace ns ON ns.oid = cfg.cfgnamespace "
            "WHERE ns.nspname = 'public' "
            "UNION ALL "
            "SELECT pg_get_userbyid(dict_obj.dictowner) AS owner_name "
            "FROM pg_ts_dict dict_obj JOIN pg_namespace ns ON ns.oid = dict_obj.dictnamespace "
            "WHERE ns.nspname = 'public' "
            "UNION ALL "
            "SELECT pg_get_userbyid(stat.stxowner) AS owner_name "
            "FROM pg_statistic_ext stat JOIN pg_namespace ns ON ns.oid = stat.stxnamespace "
            "WHERE ns.nspname = 'public' "
            "UNION ALL "
            "SELECT pg_get_userbyid(ext.extowner) AS owner_name "
            "FROM pg_extension ext JOIN pg_namespace ns ON ns.oid = ext.extnamespace "
            "WHERE ns.nspname = 'public'"
            ") owned_objects WHERE owner_name IS NOT NULL"
        )
        return {str(row[0]) for row in cursor.fetchall()}


def _assert_public_object_owners_compatible(
    connection: psycopg.Connection[tuple[object, ...]],
    owner_user: str,
    runtime_user: str,
) -> None:
    unexpected_owners = _public_object_owners(connection) - {
        owner_user,
        runtime_user,
        "pg_database_owner",
    }
    if unexpected_owners:
        raise RuntimeError(
            "public schema contains objects owned by an unsupported role; "
            "POSTGRES_OWNER_USER changes on populated volumes are not automatic"
        )


def _preflight_existing_databases(
    config: BootstrapConfig,
    admin_connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    existing_databases: list[str] = []
    with admin_connection.cursor() as cursor:
        for database_name in (config.application_database, config.test_database):
            cursor.execute(
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
                (database_name,),
            )
            row = cursor.fetchone()
            if row is None:
                continue
            if str(row[0]) not in {config.owner_user, config.runtime_user}:
                raise RuntimeError(
                    "existing database owner is incompatible; POSTGRES_OWNER_USER must remain stable "
                    "for a populated PostgreSQL volume"
                )
            existing_databases.append(database_name)
    for database_name in existing_databases:
        with _connect(config, database_name) as database_connection:
            _assert_public_object_owners_compatible(
                database_connection,
                config.owner_user,
                config.runtime_user,
            )


def _transfer_database_objects(
    connection: psycopg.Connection[tuple[object, ...]],
    owner_user: str,
    runtime_user: str,
) -> None:
    owner = sql.Identifier(owner_user)
    runtime = sql.Identifier(runtime_user)
    _assert_public_object_owners_compatible(connection, owner_user, runtime_user)
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("REASSIGN OWNED BY {} TO {}").format(runtime, owner))
        cursor.execute(sql.SQL("ALTER SCHEMA public OWNER TO {}").format(owner))
        cursor.execute("REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC")
        cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM {}").format(runtime))
        cursor.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(owner))
        cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(runtime))
        cursor.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC")
        cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}").format(runtime))
        cursor.execute("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC")
        cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}").format(runtime))
        cursor.execute("REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC")
        cursor.execute(sql.SQL("REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM {}").format(runtime))


def bootstrap_database_roles(
    config: BootstrapConfig,
    *,
    keep_runtime_quiesced: bool = False,
    migration_sequence_lock_held: bool = False,
) -> None:
    with _connect(config, config.admin_database) as admin_connection:
        _ensure_admin_connection(admin_connection)
        sequence_lock_acquired = False
        bootstrap_lock_acquired = False
        bootstrap_error: BaseException | None = None
        try:
            with admin_connection.cursor() as cursor:
                if not migration_sequence_lock_held:
                    cursor.execute(
                        "SELECT pg_try_advisory_lock(%s)",
                        (MIGRATION_SEQUENCE_ADVISORY_LOCK_KEY,),
                    )
                    if cursor.fetchone() != (True,):
                        raise RuntimeError("a database migration sequence is already running")
                    sequence_lock_acquired = True
                cursor.execute("SELECT pg_try_advisory_lock(%s)", (BOOTSTRAP_ADVISORY_LOCK_KEY,))
                if cursor.fetchone() != (True,):
                    raise RuntimeError("another database role bootstrap is already running")
                bootstrap_lock_acquired = True
            _preflight_existing_databases(config, admin_connection)
            downstream_login_roles = _downstream_login_roles(
                admin_connection,
                (config.owner_user, config.runtime_user),
            )
            _ensure_login_role(admin_connection, config.owner_user, config.owner_password, login=False)
            _ensure_login_role(admin_connection, config.runtime_user, config.runtime_password, login=False)
            _quiesce_managed_roles(
                admin_connection,
                tuple(
                    sorted(
                        {
                            config.owner_user,
                            config.runtime_user,
                            *downstream_login_roles,
                        }
                    )
                ),
            )
            with admin_connection.cursor() as cursor:
                admin_database = sql.Identifier(config.admin_database)
                owner = sql.Identifier(config.owner_user)
                runtime = sql.Identifier(config.runtime_user)
                cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM PUBLIC").format(admin_database))
                cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(admin_database, owner))
                cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(admin_database, runtime))
            for database_name in (config.application_database, config.test_database):
                _ensure_database(
                    admin_connection,
                    database_name,
                    config.owner_user,
                    config.runtime_user,
                )
            for database_name in (config.application_database, config.test_database):
                with _connect(config, database_name) as database_connection:
                    _transfer_database_objects(
                        database_connection,
                        config.owner_user,
                        config.runtime_user,
                    )
            _activate_login_role(admin_connection, config.owner_user)
            if not keep_runtime_quiesced:
                _activate_login_role(admin_connection, config.runtime_user)
        except BaseException as exc:
            bootstrap_error = exc
            raise
        finally:
            unlock_errors: list[BaseException] = []
            for lock_acquired, lock_key, error_message in (
                (
                    bootstrap_lock_acquired,
                    BOOTSTRAP_ADVISORY_LOCK_KEY,
                    "database role bootstrap lock was lost",
                ),
                (
                    sequence_lock_acquired,
                    MIGRATION_SEQUENCE_ADVISORY_LOCK_KEY,
                    "database migration sequence lock was lost",
                ),
            ):
                if not lock_acquired:
                    continue
                try:
                    with admin_connection.cursor() as cursor:
                        cursor.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
                        if cursor.fetchone() != (True,):
                            raise RuntimeError(error_message)
                except BaseException as unlock_error:
                    unlock_errors.append(unlock_error)
            if bootstrap_error is not None:
                for release_error in unlock_errors:
                    bootstrap_error.add_note(
                        "database bootstrap lock release also failed: "
                        f"{type(release_error).__name__}"
                    )
            elif unlock_errors:
                primary_unlock_error = unlock_errors[0]
                for release_error in unlock_errors[1:]:
                    primary_unlock_error.add_note(
                        "another database bootstrap lock release also failed: "
                        f"{type(release_error).__name__}"
                    )
                raise primary_unlock_error


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap least-privilege PostgreSQL roles and databases")
    parser.add_argument(
        "--keep-runtime-quiesced",
        action="store_true",
        help="leave the runtime role NOLOGIN for a caller-managed migration window",
    )
    arguments = parser.parse_args()
    bootstrap_database_roles(
        BootstrapConfig.from_environment(),
        keep_runtime_quiesced=arguments.keep_runtime_quiesced,
    )
    print("database role bootstrap completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
