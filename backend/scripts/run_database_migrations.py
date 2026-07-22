#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

if __package__:
    from backend.scripts.bootstrap_database_roles import (
        BootstrapConfig,
        MIGRATION_SEQUENCE_ADVISORY_LOCK_KEY,
        _activate_login_role,
        _connect,
        _ensure_admin_connection,
        bootstrap_database_roles,
    )
else:
    from bootstrap_database_roles import (  # type: ignore[import-not-found,no-redef]
        BootstrapConfig,
        MIGRATION_SEQUENCE_ADVISORY_LOCK_KEY,
        _activate_login_role,
        _connect,
        _ensure_admin_connection,
        bootstrap_database_roles,
    )

ROOT = Path(__file__).resolve().parents[2]
def _run(command: Sequence[str], environment: Mapping[str, str]) -> None:
    subprocess.run(command, cwd=ROOT, env=environment, check=True)


def run_database_migrations(config: BootstrapConfig) -> None:
    base_environment = os.environ.copy()
    base_environment["DATABASE_URL"] = ""
    application_environment = base_environment.copy()
    test_environment = base_environment.copy()
    test_environment["DATABASE_NAME"] = config.test_database
    python = sys.executable
    migration_commands = (
        ((python, "-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head"), application_environment),
        ((python, "backend/scripts/grant_runtime_role.py"), application_environment),
        ((python, "-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head"), test_environment),
        ((python, "backend/scripts/grant_runtime_role.py"), test_environment),
    )

    with _connect(config, config.admin_database) as lock_connection:
        _ensure_admin_connection(lock_connection)
        with lock_connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_SEQUENCE_ADVISORY_LOCK_KEY,))
            cursor.fetchone()
        command_error: BaseException | None = None
        runtime_quiesced = False
        runtime_reactivation_error: BaseException | None = None
        unlock_error: BaseException | None = None
        try:
            bootstrap_database_roles(
                config,
                keep_runtime_quiesced=True,
                migration_sequence_lock_held=True,
            )
            runtime_quiesced = True
            for command, environment in migration_commands:
                _run(command, environment)
        except BaseException as exc:
            command_error = exc
        finally:
            if runtime_quiesced and command_error is None:
                try:
                    _activate_login_role(lock_connection, config.runtime_user)
                except BaseException as exc:
                    runtime_reactivation_error = exc
            try:
                with lock_connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_SEQUENCE_ADVISORY_LOCK_KEY,))
                    if cursor.fetchone() != (True,):
                        raise RuntimeError("database migration sequence lock was lost")
            except BaseException as exc:
                unlock_error = exc

        if command_error is not None:
            if runtime_reactivation_error is not None:
                command_error.add_note(
                    "runtime role reactivation also failed: "
                    f"{type(runtime_reactivation_error).__name__}"
                )
            if unlock_error is not None:
                command_error.add_note(
                    f"database migration sequence lock release also failed: {type(unlock_error).__name__}"
                )
            raise command_error
        if runtime_reactivation_error is not None:
            if unlock_error is not None:
                runtime_reactivation_error.add_note(
                    f"database migration sequence lock release also failed: {type(unlock_error).__name__}"
                )
            raise runtime_reactivation_error
        if unlock_error is not None:
            raise unlock_error


def main() -> int:
    run_database_migrations(BootstrapConfig.from_environment())
    print("database migrations completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
