from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass

import psycopg
from psycopg import sql


_PROBE_NAME = re.compile(r"fj_probe_[a-z0-9_]{6,48}\Z")


@dataclass(frozen=True)
class ProbeConfig:
    host: str
    port: int
    admin_database: str
    admin_user: str
    admin_password: str
    app_database: str
    app_user: str
    app_password: str
    owner_user: str
    sentinel: str


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _probe_identifier(name: str) -> str:
    value = _required(name)
    if not _PROBE_NAME.fullmatch(value):
        raise RuntimeError(f"{name} must use the isolated fj_probe_ namespace")
    return value


def _config() -> ProbeConfig:
    if os.environ.get("ALLOW_DESTRUCTIVE_LEGACY_VOLUME_PROBE") != "1":
        raise RuntimeError("legacy volume probe requires an explicit safety opt-in")
    return ProbeConfig(
        host=_required("POSTGRES_ADMIN_HOST"),
        port=int(_required("POSTGRES_ADMIN_PORT")),
        admin_database=_required("POSTGRES_ADMIN_DB"),
        admin_user=_required("POSTGRES_ADMIN_USER"),
        admin_password=_required("POSTGRES_ADMIN_PASSWORD"),
        app_database=_probe_identifier("POSTGRES_APP_DB"),
        app_user=_probe_identifier("POSTGRES_APP_USER"),
        app_password=_required("POSTGRES_APP_PASSWORD"),
        owner_user=_probe_identifier("POSTGRES_OWNER_USER"),
        sentinel=_required("LEGACY_VOLUME_PROBE_SENTINEL"),
    )


def _connect(
    config: ProbeConfig,
    *,
    database: str,
    user: str,
    password: str,
) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=database,
        user=user,
        password=password,
        connect_timeout=5,
    )


def seed(config: ProbeConfig) -> None:
    with _connect(
        config,
        database=config.admin_database,
        user=config.admin_user,
        password=config.admin_password,
    ) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (config.app_database,),
            )
            if cursor.fetchone() is not None:
                raise RuntimeError("probe database already exists")
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (config.app_user,))
            if cursor.fetchone() is not None:
                raise RuntimeError("probe application role already exists")
            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(config.app_user),
                    sql.Literal(config.app_password),
                )
            )
            cursor.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(config.app_database),
                    sql.Identifier(config.app_user),
                )
            )

    with _connect(
        config,
        database=config.app_database,
        user=config.app_user,
        password=config.app_password,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE legacy_volume_probe "
                "(id integer PRIMARY KEY, sentinel text NOT NULL)"
            )
            cursor.execute(
                "INSERT INTO legacy_volume_probe (id, sentinel) VALUES (1, %s)",
                (config.sentinel,),
            )


def verify(config: ProbeConfig) -> None:
    with _connect(
        config,
        database=config.app_database,
        user=config.app_user,
        password=config.app_password,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT sentinel FROM legacy_volume_probe WHERE id = 1")
            row = cursor.fetchone()
            if row != (config.sentinel,):
                raise RuntimeError("legacy sentinel was not preserved")
            cursor.execute(
                "UPDATE legacy_volume_probe SET sentinel = %s WHERE id = 1",
                (config.sentinel,),
            )

    try:
        with _connect(
            config,
            database=config.app_database,
            user=config.app_user,
            password=config.app_password,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE TABLE runtime_role_must_not_create (id integer)")
    except psycopg.errors.InsufficientPrivilege:
        pass
    else:
        raise RuntimeError("runtime role retained DDL privileges after migration")

    with _connect(
        config,
        database=config.app_database,
        user=config.admin_user,
        password=config.admin_password,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tableowner FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename = 'legacy_volume_probe'"
            )
            owner = cursor.fetchone()
            if owner != (config.owner_user,):
                raise RuntimeError("legacy table ownership was not transferred")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("seed", "verify"))
    action = parser.parse_args().action
    config = _config()
    if action == "seed":
        seed(config)
    else:
        verify(config)
    print(f"legacy volume probe {action}: PASS")


if __name__ == "__main__":
    main()
