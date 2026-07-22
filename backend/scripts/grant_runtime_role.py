#!/usr/bin/env python3
from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg
from psycopg import sql

if __package__:
    from backend.scripts.bootstrap_database_roles import (
        require_identifier,
        require_port,
        require_secret,
        read_secret_from_environment,
    )
else:
    from bootstrap_database_roles import (  # type: ignore[import-not-found,no-redef]
        require_identifier,
        require_port,
        require_secret,
        read_secret_from_environment,
    )


@dataclass(frozen=True)
class RuntimeGrantConfig:
    host: str
    port: int
    database: str
    owner_user: str
    owner_password: str
    runtime_user: str

    @classmethod
    def from_environment(cls) -> RuntimeGrantConfig:
        config = cls(
            host=require_secret("DATABASE_HOST", os.environ.get("DATABASE_HOST", "")),
            port=require_port("DATABASE_PORT", os.environ.get("DATABASE_PORT", "5432")),
            database=require_identifier("DATABASE_NAME", os.environ.get("DATABASE_NAME", "")),
            owner_user=require_identifier("DATABASE_USER", os.environ.get("DATABASE_USER", "")),
            owner_password=read_secret_from_environment("DATABASE_PASSWORD"),
            runtime_user=require_identifier("POSTGRES_APP_USER", os.environ.get("POSTGRES_APP_USER", "")),
        )
        if config.owner_user == config.runtime_user:
            raise ValueError("owner and runtime roles must be distinct")
        return config


def grant_runtime_role(config: RuntimeGrantConfig) -> None:
    owner = sql.Identifier(config.owner_user)
    runtime = sql.Identifier(config.runtime_user)
    database = sql.Identifier(config.database)
    with psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.database,
        user=config.owner_user,
        password=config.owner_password,
        connect_timeout=10,
        options="-c statement_timeout=300000 -c lock_timeout=30000",
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(database, runtime))
            cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, runtime))
            cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM {}").format(runtime))
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(runtime))
            cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}").format(runtime))
            cursor.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC")
            cursor.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(runtime))
            cursor.execute(sql.SQL("REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE public.alembic_version FROM {}").format(runtime))
            cursor.execute(sql.SQL("GRANT SELECT ON TABLE public.alembic_version TO {}").format(runtime))
            cursor.execute(
                sql.SQL(
                    "REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
                    "ON TABLE public.operation_logs FROM {}"
                ).format(runtime)
            )
            cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}").format(runtime))
            cursor.execute("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC")
            cursor.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(runtime))
            cursor.execute(sql.SQL("REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM {}").format(runtime))
            cursor.execute("REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC")
            cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public REVOKE ALL PRIVILEGES ON TABLES FROM {}").format(owner, runtime))
            cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public REVOKE ALL PRIVILEGES ON SEQUENCES FROM {}").format(owner, runtime))
            cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public REVOKE ALL PRIVILEGES ON FUNCTIONS FROM {}").format(owner, runtime))
            cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC").format(owner))
            cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC").format(owner))
            cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC").format(owner))
            cursor.execute(
                sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(owner, runtime)
            )
            cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {}").format(owner, runtime))


def main() -> int:
    grant_runtime_role(RuntimeGrantConfig.from_environment())
    print("runtime database grants completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
