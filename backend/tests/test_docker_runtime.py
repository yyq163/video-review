from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, capture_output=True, text=True)


def _delivery_script_sandbox(tmp_path: Path, script_name: str) -> tuple[Path, Path]:
    sandbox_root = tmp_path / f"delivery-{script_name.removesuffix('.sh')}"
    scripts_dir = sandbox_root / "scripts"
    scripts_dir.mkdir(parents=True)
    script = scripts_dir / script_name
    shutil.copy2(ROOT / "scripts" / script_name, script)
    shutil.copy2(ROOT / "scripts/validate-delivery-image-ref.sh", scripts_dir / "validate-delivery-image-ref.sh")
    (sandbox_root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (sandbox_root / "docker-compose.delivery.yml").write_text("services: {}\n", encoding="utf-8")
    env_file = sandbox_root / ".env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)
    return sandbox_root, script


def test_compose_secrets_and_capabilities_drop_to_application_user() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))

    backend_check = compose["services"]["backend"]["healthcheck"]["test"]
    maintenance_check = compose["services"]["maintenance"]["healthcheck"]["test"]
    assert backend_check[:3] == ["CMD", "gosu", "app"]
    assert maintenance_check[:3] == ["CMD", "gosu", "app"]
    assert "/runtimez" in backend_check[-1]
    assert "container_name" not in compose["services"]["backend"]
    assert compose["volumes"]["fj-final-cut-review-postgres"]["name"].startswith("${POSTGRES_VOLUME_NAME:-")
    assert compose["volumes"]["fj-final-cut-review-data"]["name"].startswith("${DATA_VOLUME_NAME:-")
    assert compose["volumes"]["fj-final-cut-review-runtime-state"]["name"].startswith("${RUNTIME_STATE_VOLUME_NAME:-")
    assert set(compose["secrets"]) == {
        "postgres_admin_password",
        "postgres_owner_password",
        "postgres_app_password",
        "write_guard_code",
        "write_guard_session_secret",
    }
    assert compose["services"]["postgres"]["environment"] == {
        "POSTGRES_DB": "${POSTGRES_ADMIN_DB:-postgres}",
        "POSTGRES_USER": "${POSTGRES_ADMIN_USER:-postgres}",
        "POSTGRES_PASSWORD_FILE": "/run/secrets/postgres_admin_password",
    }
    assert compose["services"]["postgres"]["secrets"] == ["postgres_admin_password"]
    dockerfile = (root / "backend/Dockerfile").read_text(encoding="utf-8")
    assert "database_readiness; database_readiness()" in dockerfile
    assert "postgresql-client-16=16.14-1.pgdg13+1" in dockerfile
    assert "postgresql-client-common=291.pgdg13+1" in dockerfile
    assert "libpq5=18.4-1.pgdg13+1" in dockerfile
    assert "postgresql-client " not in dockerfile
    assert "apt.postgresql.org.asc" in dockerfile
    assert "sha256sum --check --strict" in dockerfile
    assert "pg_dump --version" in dockerfile
    assert "pg_restore --version" in dockerfile
    for service_name in ("migrate", "backend", "maintenance", "package-worker"):
        service = compose["services"][service_name]
        assert service["cap_drop"] == ["ALL"]
        assert set(service["cap_add"]) == {"CHOWN", "DAC_READ_SEARCH", "SETGID", "SETUID"}
        assert service["security_opt"] == ["no-new-privileges:true"]
    for service_name in ("backend", "maintenance", "package-worker"):
        environment = compose["services"][service_name]["environment"]
        assert "DATABASE_URL" not in environment
        assert "DATABASE_PASSWORD" not in environment
        assert environment["DATABASE_HOST"] == "postgres"
        assert set(("DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD_FILE")) <= set(environment)
        assert environment["DATABASE_PASSWORD_FILE"] == "/run/secrets/postgres_app_password"
        assert "postgres_app_password" in compose["services"][service_name]["secrets"]
        assert environment["PACKAGE_TTL_SECONDS"] == "${PACKAGE_TTL_SECONDS:-86400}"
    backend_environment = compose["services"]["backend"]["environment"]
    assert "WRITE_GUARD_SESSION_SECRET" not in backend_environment
    assert backend_environment["WRITE_GUARD_SESSION_SECRET_FILE"] == "/run/secrets/write_guard_session_secret"
    assert backend_environment["WRITE_GUARD_CODE_FILE"] == "/run/secrets/write_guard_code"
    assert "write_guard_code" in compose["services"]["backend"]["secrets"]
    assert "write_guard_session_secret" in compose["services"]["backend"]["secrets"]
    migrate_environment = compose["services"]["migrate"]["environment"]
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
    entrypoint = (root / "backend/docker-entrypoint.sh").read_text(encoding="utf-8")
    assert "chown -R -h -P app:app /data" in entrypoint
    assert 'exec gosu app "$@"' in entrypoint
    worker_environment = compose["services"]["package-worker"]["environment"]
    assert worker_environment["PACKAGE_WORKER_MAX_ATTEMPTS"] == "${PACKAGE_WORKER_MAX_ATTEMPTS:-3}"
    assert worker_environment["PACKAGE_WORKER_RETRY_DELAY_SECONDS"] == "${PACKAGE_WORKER_RETRY_DELAY_SECONDS:-30}"


def test_persistence_verifier_hashes_all_business_and_audit_rows() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts/verify-compose-persistence.sh").read_text(encoding="utf-8")
    assert "row_to_json(t)::text" in script
    assert "string_agg" not in script
    assert "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;" in script
    assert "hashlib.sha256()" in script
    assert "for offset in range(0, len(line), 64 * 1024)" in script
    assert script.index("stop_writers\nbefore=$(database_snapshot)") < script.index("before=$(database_snapshot)")
    assert script.count("stop_writers\nafter_") == 2
    assert "compose stop backend maintenance package-worker" in script
    assert "compose ps --format json postgres backend maintenance package-worker" in script
    assert 'expected = {"postgres", "backend", "maintenance", "package-worker"}' in script
    assert script.count("restore_four_services") >= 4
    restore_block = script[script.index("restore_four_services()") : script.index(
        "restart_four_services_in_dependency_order()"
    )]
    assert "compose up -d --wait --force-recreate --wait-timeout 180" in restore_block
    assert "--no-deps" not in restore_block
    restart_block = script[
        script.index("restart_four_services_in_dependency_order()") : script.index("stop_writers()")
    ]
    assert "compose restart postgres" in restart_block
    assert "compose restart backend" not in restart_block
    assert "compose restart maintenance package-worker" not in restart_block
    assert "restore_four_services" in restart_block
    assert "restart_four_services_in_dependency_order\nstop_writers" in script
    assert script.index("backend.app.maintenance cleanup") < script.index("before=$(database_snapshot)")
    assert "if not rows or" not in script
    assert "reserved_bytes, parts_cleanup_confirmed_at, file_id" in script
    assert "'{}', $sentinel_reserved_bytes, now(), '$sentinel_file_id'" in script
    assert "sentinel_blob_created=0" in script
    assert "sentinel_rows_created=0" in script
    assert 'sentinel_suffix="$(openssl rand -hex 16)"' in script
    assert "compose exec -T backend" not in script
    assert script.count("compose exec -T --user 10001:10001") == 4
    create_block = script[script.index("create_sentinel()") : script.index("cleanup_sentinel()")]
    cleanup_block = script[script.index("cleanup_sentinel()") : script.index("trap 'cleanup_sentinel")]
    assert "compose exec -T --user 10001:10001" in create_block
    assert "compose exec -T --user 10001:10001" in cleanup_block
    backup = (root / "scripts/postgres-backup-restore-smoke.sh").read_text(encoding="utf-8")
    assert "reserved_bytes, parts_cleanup_confirmed_at, file_id" in backup
    assert "'{}', $SENTINEL_RESERVED_BYTES, now(), '$SENTINEL_FILE_ID'" in backup
    assert 'probe_nonce="$(openssl rand -hex 16)"' in backup
    assert "source_db_created=0" in backup
    assert "restore_db_created=0" in backup
    assert "blob_created=0" in backup
    assert "EXPECTED_DB_SENTINEL" in backup
    assert "shobj_description(oid, 'pg_database')" in backup
    assert 'test "$database_owner" = "$POSTGRES_OWNER_USER"' in backup
    assert 'test "$database_sentinel" = "$EXPECTED_DB_SENTINEL"' in backup
    assert 'source_db_identity="fcr-backup-restore-smoke:$probe_nonce:source"' in backup
    assert 'restore_db_identity="fcr-backup-restore-smoke:$probe_nonce:restore"' in backup
    assert '"$OWNED_DB" "$EXPECTED_DB_SENTINEL"' in backup
    assert backup.index("\nsource_db_created=1\n") < backup.index(
        'if create_owned_database "$source_db" "$source_db_identity"; then'
    ) < backup.index('mark_owned_database "$source_db" "$source_db_identity"')
    assert backup.index("\nrestore_db_created=1\n") < backup.index(
        'if create_owned_database "$restore_db" "$restore_db_identity"; then'
    )
    for table_name in (
        "annotation_sets",
        "file_objects",
        "finalizations",
        "idempotency_records",
        "issue_revisions",
        "operation_logs",
        "outbox_consumer_receipts",
        "outbox_events",
        "package_snapshots",
        "project_refs",
        "review_decisions",
        "review_issues",
        "review_items",
        "review_versions",
        "thread_messages",
        "upload_sessions",
    ):
        assert f"FROM {table_name} t" in script


def test_fixed_docker_runtime_gate_covers_delivery_lifecycle() -> None:
    script = (ROOT / "scripts/docker-compose-runtime.sh").read_text(encoding="utf-8")
    required_steps = (
        "compose config --quiet",
        "compose build",
        "compose up -d --wait --force-recreate --wait-timeout 180",
        "\nassert_required_services_healthy\n",
        'sh "$script_dir/postgres-backup-restore-smoke.sh"',
        'sh "$script_dir/verify-compose-persistence.sh"',
        "backend_runtime_image=$(resolve_runtime_image_id backend)",
        "postgres_runtime_image=$(resolve_runtime_image_id postgres)",
        "RUN_DOCKER_RUNTIME_TESTS=1",
        'BACKEND_RUNTIME_IMAGE="$backend_runtime_image"',
        'POSTGRES_RUNTIME_IMAGE="$postgres_runtime_image"',
        "backend/.venv/bin/pytest backend/tests/test_docker_runtime.py -q",
    )
    positions = [script.index(step) for step in required_steps]
    assert positions == sorted(positions)
    assert "down -v" not in script
    assert 'sh "$script_dir/compose-delivery.sh" "$@"' in script
    assert 'expected = {"postgres", "backend", "maintenance", "package-worker"}' in script
    assert 're.fullmatch(r"(?:sha256:)?([0-9a-f]{64})", image_id)' in script
    assert 'docker image inspect "$image_id"' in script
    assert 'docker container inspect "$1"' in script
    assert "env -i" in script
    assert 'DOCKER_CONFIG="${DOCKER_CONFIG:-${HOME:-/tmp}/.docker}"' in script


def test_fixed_docker_runtime_gate_recreates_all_services_without_deleting_volumes(tmp_path: Path) -> None:
    probe_root = tmp_path / "runtime-gate"
    scripts_dir = probe_root / "scripts"
    pytest_path = probe_root / "backend" / ".venv" / "bin" / "pytest"
    scripts_dir.mkdir(parents=True)
    pytest_path.parent.mkdir(parents=True)
    runtime_script = scripts_dir / "docker-compose-runtime.sh"
    shutil.copy2(ROOT / "scripts/docker-compose-runtime.sh", runtime_script)
    compose_log = probe_root / "compose.log"
    (scripts_dir / "compose-delivery.sh").write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$FAKE_COMPOSE_LOG"
if [ "${1:-}" = ps ] && [ "${2:-}" = -q ]; then
    printf '%s-container\n' "${3:-unknown}"
elif [ "${1:-}" = ps ]; then
    printf '%s\\n' '[{"Service":"postgres","State":"running","Health":"healthy"},{"Service":"backend","State":"running","Health":"healthy"},{"Service":"maintenance","State":"running","Health":"healthy"},{"Service":"package-worker","State":"running","Health":"healthy"}]'
elif [ "${1:-}" = images ] && [ "${2:-}" = -q ]; then
    printf 'a%.0s' $(seq 1 64)
    printf '\\n'
fi
""",
        encoding="utf-8",
    )
    for helper in ("postgres-backup-restore-smoke.sh", "verify-compose-persistence.sh"):
        (scripts_dir / helper).write_text("#!/bin/sh\nset -eu\n", encoding="utf-8")
    pytest_path.write_text(
        """#!/bin/sh
set -eu
test "$RUN_DOCKER_RUNTIME_TESTS" = 1
test "$BACKEND_RUNTIME_IMAGE" = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
test "$POSTGRES_RUNTIME_IMAGE" = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
test "$DOCKER_HOST" = "tcp://docker.example.test:2376"
test "$DOCKER_CONTEXT" = "delivery-context"
test "$DOCKER_TLS" = 1
test "$DOCKER_TLS_VERIFY" = 1
test "$DOCKER_CERT_PATH" = "/local/docker/certs"
test "$DOCKER_API_VERSION" = "1.52"
: > backend/.venv/bin/pytest.poisoned-env
for variable_name in FCR_COMPOSE_PROJECT FCR_COMPOSE_ENV_FILE POSTGRES_VOLUME_NAME DATA_VOLUME_NAME RUNTIME_STATE_VOLUME_NAME COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE DATABASE_URL DATABASE_PASSWORD WRITE_GUARD_CODE WRITE_GUARD_SESSION_SECRET; do
    if env | cut -d= -f1 | grep -Fxq "$variable_name"; then
        printf '%s\n' "$variable_name" >> backend/.venv/bin/pytest.poisoned-env
    fi
done
""",
        encoding="utf-8",
    )
    pytest_path.chmod(0o700)
    fake_docker = probe_root / "docker"
    fake_docker.write_text(
        """#!/bin/sh
set -eu
case "$1:$2" in
  image:inspect|container:inspect)
    printf 'sha256:'
    printf 'a%.0s' $(seq 1 64)
    printf '\n'
    ;;
  *) exit 2;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)

    result = subprocess.run(
        ["sh", str(runtime_script)],
        cwd=probe_root,
        env={
            **os.environ,
            "PATH": f"{probe_root}:{os.environ['PATH']}",
            "FAKE_COMPOSE_LOG": str(compose_log),
            "DOCKER_HOST": "tcp://docker.example.test:2376",
            "DOCKER_CONTEXT": "delivery-context",
            "DOCKER_TLS": "1",
            "DOCKER_TLS_VERIFY": "1",
            "DOCKER_CERT_PATH": "/local/docker/certs",
            "DOCKER_API_VERSION": "1.52",
            "FCR_COMPOSE_PROJECT": "must-not-leak",
            "FCR_COMPOSE_ENV_FILE": "/must/not/leak.env",
            "POSTGRES_VOLUME_NAME": "must-not-leak-postgres",
            "DATA_VOLUME_NAME": "must-not-leak-data",
            "RUNTIME_STATE_VOLUME_NAME": "must-not-leak-state",
            "COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE": "/must/not/leak-secret",
            "DATABASE_URL": "must-not-leak-database-url",
            "DATABASE_PASSWORD": "must-not-leak-database-password",
            "WRITE_GUARD_CODE": "must-not-leak-code",
            "WRITE_GUARD_SESSION_SECRET": "must-not-leak-session",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (pytest_path.parent / "pytest.poisoned-env").read_text(encoding="utf-8") == ""
    commands = compose_log.read_text(encoding="utf-8").splitlines()
    assert "up -d --wait --force-recreate --wait-timeout 180" in commands
    assert all("down" not in command for command in commands)
    assert all("--volumes" not in command and "-v" not in shlex.split(command) for command in commands)
    assert all("volume rm" not in command for command in commands)


@pytest.mark.parametrize(
    ("compose_ids", "inspect_ids", "container_image_ids", "expected_success"),
    (
        ("a" * 64, f"sha256:{'a' * 64}", f"sha256:{'a' * 64}", True),
        (f"sha256:{'a' * 64}", f"sha256:{'a' * 64}", f"sha256:{'a' * 64}", True),
        ("A" * 64, f"sha256:{'a' * 64}", f"sha256:{'a' * 64}", False),
        (f"{'a' * 64}\n{'b' * 64}", f"sha256:{'a' * 64}", f"sha256:{'a' * 64}", False),
        ("sha512:" + "a" * 64, f"sha256:{'a' * 64}", f"sha256:{'a' * 64}", False),
        ("a" * 64, f"sha256:{'b' * 64}", f"sha256:{'a' * 64}", False),
        ("a" * 64, f"sha256:{'a' * 64}", f"sha256:{'b' * 64}", False),
    ),
)
def test_runtime_image_id_normalization_is_strict_and_identity_bound(
    tmp_path: Path,
    compose_ids: str,
    inspect_ids: str,
    container_image_ids: str,
    expected_success: bool,
) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script_source = (ROOT / "scripts/docker-compose-runtime.sh").read_text(encoding="utf-8")
    function_source = script_source.split("\ncompose config --quiet", 1)[0]
    probe = scripts_dir / "docker-compose-runtime.sh"
    probe.write_text(f"{function_source}\nresolve_runtime_image_id backend\n", encoding="utf-8")
    (scripts_dir / "compose-delivery.sh").write_text(
        """#!/bin/sh
set -eu
if [ "$1:$2" = images:-q ]; then
    printf '%s\n' "$FAKE_COMPOSE_IMAGE_IDS"
elif [ "$1:$2" = ps:-q ]; then
    printf '%s\n' backend-container
else
    exit 2
fi
""",
        encoding="utf-8",
    )
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
set -eu
case "$1:$2" in
  image:inspect) printf '%s\n' "$FAKE_INSPECT_IMAGE_IDS";;
  container:inspect) printf '%s\n' "$FAKE_CONTAINER_IMAGE_IDS";;
  *) exit 2;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)

    result = subprocess.run(
        ["sh", str(probe)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FAKE_COMPOSE_IMAGE_IDS": compose_ids,
            "FAKE_INSPECT_IMAGE_IDS": inspect_ids,
            "FAKE_CONTAINER_IMAGE_IDS": container_image_ids,
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert (result.returncode == 0) is expected_success
    if expected_success:
        assert result.stdout.strip() == f"sha256:{'a' * 64}"


def test_compose_runtime_scripts_resolve_passwords_from_secret_files_only() -> None:
    persistence = (ROOT / "scripts/verify-compose-persistence.sh").read_text(encoding="utf-8")
    backup = (ROOT / "scripts/postgres-backup-restore-smoke.sh").read_text(encoding="utf-8")
    delivery = (ROOT / "scripts/compose-delivery.sh").read_text(encoding="utf-8")
    upgrade = (ROOT / "scripts/verify-postgres-existing-volume-upgrade.sh").read_text(encoding="utf-8")

    for script in (persistence, backup, upgrade):
        assert "from backend.scripts.bootstrap_database_roles import read_secret_from_environment" in script
        assert 'os.environ.get(secret_name) or not os.environ.get(f"{secret_name}_FILE")' in script
    for script, assignment_name in (
        (persistence, "compose_psql_python"),
        (backup, "compose_secret_exec_python"),
        (upgrade, "legacy_probe_python"),
    ):
        source = script.split(f"{assignment_name}='", 1)[1].split("'\n", 1)[0]
        compile(source, f"<{assignment_name}>", "exec")
    for plaintext_expansion in (
        "$DATABASE_PASSWORD",
        "$POSTGRES_ADMIN_PASSWORD",
        "$POSTGRES_OWNER_PASSWORD",
        "$POSTGRES_APP_PASSWORD",
        "$WRITE_GUARD_SESSION_SECRET",
    ):
        assert plaintext_expansion not in persistence
        assert plaintext_expansion not in backup
    assert 'environment["PGPASSWORD"] = read_secret_from_environment(secret_name)' in persistence
    assert 'environment["PGPASSWORD"] = read_secret_from_environment(secret_name)' in backup
    assert 'allowed_commands = {"createdb", "dropdb", "pg_dump", "pg_restore", "psql"}' in backup

    assert "os.O_NOFOLLOW" in delivery
    assert "must reference a readable non-symlink regular file" in delivery
    assert "must contain exactly one non-empty secret value" in delivery
    assert "must not grant group or other permissions" in delivery
    assert "must not be provided directly to Compose" in delivery

    assert "COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE={admin_path}" in upgrade
    assert "COMPOSE_POSTGRES_OWNER_PASSWORD_FILE={owner_path}" in upgrade
    assert "COMPOSE_POSTGRES_APP_PASSWORD_FILE={app_path}" in upgrade
    assert "COMPOSE_WRITE_GUARD_SESSION_SECRET_FILE={session_path}" in upgrade
    assert "COMPOSE_WRITE_GUARD_CODE_FILE={code_path}" in upgrade
    assert "POSTGRES_ADMIN_PASSWORD=$admin_password" not in upgrade
    assert "POSTGRES_OWNER_PASSWORD=$owner_password" not in upgrade
    assert "POSTGRES_APP_PASSWORD=$app_password" not in upgrade
    assert "WRITE_GUARD_SESSION_SECRET=$session_secret" not in upgrade

    host_backup = (ROOT / "scripts/postgres-host-backup-restore-smoke.sh").read_text(encoding="utf-8")
    alembic_drift = (ROOT / "scripts/alembic-current-head-drift.sh").read_text(encoding="utf-8")
    assert "POSTGRES_ADMIN_PASSWORD POSTGRES_ADMIN_DB" in host_backup
    assert 'PGPASSWORD="$POSTGRES_ADMIN_PASSWORD"' in host_backup
    assert ': "${DATABASE_URL:?DATABASE_URL is required}"' in alembic_drift
    assert ': "${POSTGRES_OWNER_DATABASE_URL:?POSTGRES_OWNER_DATABASE_URL is required}"' in alembic_drift


def test_backup_smoke_scripts_pin_repository_root_and_avoid_secret_argv() -> None:
    compose_backup = (ROOT / "scripts/postgres-backup-restore-smoke.sh").read_text(encoding="utf-8")
    host_backup = (ROOT / "scripts/postgres-host-backup-restore-smoke.sh").read_text(encoding="utf-8")

    for script in (compose_backup, host_backup):
        assert 'project_root=$(CDPATH= cd -- "$script_dir/.." && pwd -P)' in script
    assert compose_backup.index('cd "$project_root"') < compose_backup.index("compose()")
    assert host_backup.index('cd "$project_root"') < host_backup.index("backend/.venv/bin/")
    assert 'sh "$script_dir/compose-delivery.sh" "$@"' in compose_backup
    assert 'exec "$@"' in host_backup
    assert "env -u " not in host_backup
    assert "/usr/bin/env " not in host_backup


def test_compose_backup_smoke_fails_closed_without_pg16_client_tools() -> None:
    compose_backup = (ROOT / "scripts/postgres-backup-restore-smoke.sh").read_text(encoding="utf-8")

    version_gate = compose_backup.index("for client in pg_dump pg_restore")
    first_database_name = compose_backup.index('source_db="fcr_backup_source_')
    assert version_gate < first_database_name
    assert '"$client (PostgreSQL) 16."*' in compose_backup
    assert "backup/restore requires PostgreSQL 16 client tools" in compose_backup


def test_compose_backup_smoke_labels_non_sensitive_verification_failures() -> None:
    compose_backup = (ROOT / "scripts/postgres-backup-restore-smoke.sh").read_text(encoding="utf-8")

    assert 'printf "%s\\n" "backup/restore verification failed: $check_name" >&2' in compose_backup
    for check_name in (
        "source_revision_present",
        "migration_revision_match",
        "public_table_count_match",
        "restored_database_owner",
        "restored_schema_owner",
        "restored_relation_ownership",
        "project_sentinel_match",
        "file_binding_match",
        "runtime_least_privilege",
    ):
        assert f"verify_or_fail {check_name}" in compose_backup

    assert "pg_get_userbyid(n.nspowner) = 'pg_database_owner'" in compose_backup
    assert 'verify_or_fail restored_schema_owner test "$schema_owner_valid" -eq 1' in compose_backup


def test_host_backup_owner_runner_keeps_canary_secrets_out_of_process_arguments(
    tmp_path: Path,
) -> None:
    host_backup = (ROOT / "scripts/postgres-host-backup-restore-smoke.sh").read_text(encoding="utf-8")
    run_owner_block = host_backup[host_backup.index("run_owner() (") : host_backup.index("trap '' HUP INT TERM")]
    argv_log = tmp_path / "argv.log"
    probe = tmp_path / "probe.sh"
    probe.write_text(
        """#!/bin/sh
set -eu
test "$DATABASE_PASSWORD" = "$EXPECTED_OWNER_SECRET"
test "$POSTGRES_ADMIN_PASSWORD" = "$EXPECTED_ADMIN_SECRET"
test "$POSTGRES_APP_PASSWORD" = "$EXPECTED_APP_SECRET"
printf '%s\n' "$*" >"$ARGV_LOG"
""",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    harness = tmp_path / "run-owner-harness.sh"
    harness.write_text(
        """#!/bin/sh
set -eu
POSTGRES_ADMIN_HOST=127.0.0.1
POSTGRES_ADMIN_PORT=5432
POSTGRES_ADMIN_DB=postgres
POSTGRES_ADMIN_USER=test_admin
POSTGRES_ADMIN_PASSWORD=$EXPECTED_ADMIN_SECRET
POSTGRES_OWNER_USER=test_owner
POSTGRES_OWNER_PASSWORD=$EXPECTED_OWNER_SECRET
POSTGRES_APP_USER=test_app
POSTGRES_APP_PASSWORD=$EXPECTED_APP_SECRET
"""
        + run_owner_block
        + '\nrun_owner test_database "$PROBE" --mode verify\n',
        encoding="utf-8",
    )
    harness.chmod(0o700)
    canaries = {
        "EXPECTED_ADMIN_SECRET": "canary-admin-secret-value",
        "EXPECTED_OWNER_SECRET": "canary-owner-secret-value",
        "EXPECTED_APP_SECRET": "canary-app-secret-value",
    }

    result = subprocess.run(
        ["sh", str(harness)],
        env={
            **os.environ,
            **canaries,
            "ARGV_LOG": str(argv_log),
            "PROBE": str(probe),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    arguments = argv_log.read_text(encoding="utf-8")
    assert arguments.strip() == "--mode verify"
    assert all(canary not in arguments for canary in canaries.values())
    assert all(canary not in result.stdout + result.stderr for canary in canaries.values())


def test_delivery_wrapper_uses_project_relative_immutable_secret_snapshots_from_any_cwd(
    tmp_path: Path,
) -> None:
    root, wrapper = _delivery_script_sandbox(tmp_path, "compose-delivery.sh")
    caller = tmp_path / "caller"
    caller.mkdir()
    secret_dir = root / "secrets"
    secret_dir.mkdir(mode=0o700)
    for name in ("admin", "owner", "app", "code", "session"):
        secret = secret_dir / name
        secret.write_text(f"original-{name}\n", encoding="utf-8")
        secret.chmod(0o600)
    alternate_env = root / "alternate.env"
    alternate_env.write_text("", encoding="utf-8")
    alternate_env.chmod(0o600)

    report = tmp_path / "snapshot-report"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        "\n".join(
            (
                "#!/bin/sh",
                'case "$*" in',
                '  *"config --environment"*)',
                "    printf '%s\\n' 'BACKEND_IMAGE_REPOSITORY=fj-review'",
                "    printf '%s\\n' 'BACKEND_IMAGE_DIGEST=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'",
                "    printf '%s\\n' 'COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE=secrets/admin'",
                "    printf '%s\\n' 'COMPOSE_POSTGRES_OWNER_PASSWORD_FILE=secrets/owner'",
                "    printf '%s\\n' 'COMPOSE_POSTGRES_APP_PASSWORD_FILE=secrets/app'",
                "    printf '%s\\n' 'COMPOSE_WRITE_GUARD_CODE_FILE=secrets/code'",
                "    printf '%s\\n' 'COMPOSE_WRITE_GUARD_SESSION_SECRET_FILE=secrets/session'",
                "    exit 0",
                "    ;;",
                "esac",
                "for original in \"$ORIGINAL_SECRET_DIR\"/*; do printf '%s\\n' 'replaced-after-validation' >\"$original\"; done",
                "env_file=",
                "previous=",
                'for argument in "$@"; do',
                '    if [ "$previous" = "--env-file" ]; then env_file=$argument; break; fi',
                "    previous=$argument",
                "done",
                'test -n "$env_file"',
                ": >\"$SNAPSHOT_REPORT\"",
                "for mapping in COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE:admin COMPOSE_POSTGRES_OWNER_PASSWORD_FILE:owner COMPOSE_POSTGRES_APP_PASSWORD_FILE:app COMPOSE_WRITE_GUARD_CODE_FILE:code COMPOSE_WRITE_GUARD_SESSION_SECRET_FILE:session; do",
                "    variable=${mapping%%:*}",
                "    label=${mapping#*:}",
                "    snapshot=$(sed -n \"s/^${variable}=//p\" \"$env_file\" | tail -n 1)",
                '    test -n "$snapshot"',
                '    test "$snapshot" != "$ORIGINAL_SECRET_DIR/$label"',
                '    test "$(cat "$snapshot")" = "original-$label"',
                "    printf '%s|%s\\n' \"$variable\" \"$snapshot\" >>\"$SNAPSHOT_REPORT\"",
                "done",
                "exit 0",
                "",
            )
        ),
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    result = subprocess.run(
        ["sh", str(wrapper), "config", "--quiet"],
        cwd=caller,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FCR_COMPOSE_ENV_FILE": "alternate.env",
            "ORIGINAL_SECRET_DIR": str(secret_dir),
            "SNAPSHOT_REPORT": str(report),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report_lines = report.read_text(encoding="utf-8").splitlines()
    assert len(report_lines) == 5
    expected_labels = {
        "COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE": "admin",
        "COMPOSE_POSTGRES_OWNER_PASSWORD_FILE": "owner",
        "COMPOSE_POSTGRES_APP_PASSWORD_FILE": "app",
        "COMPOSE_WRITE_GUARD_CODE_FILE": "code",
        "COMPOSE_WRITE_GUARD_SESSION_SECRET_FILE": "session",
    }
    for line in report_lines:
        variable, raw_snapshot = line.split("|", 1)
        persistent_snapshot = Path(raw_snapshot)
        assert variable in expected_labels
        assert persistent_snapshot.is_file()
        assert not persistent_snapshot.is_symlink()
        assert persistent_snapshot.parent.name == ".compose-delivery-fj-final-cut-review"
        assert persistent_snapshot.stat().st_mode & 0o077 == 0
        assert persistent_snapshot.read_text(encoding="utf-8") == f"original-{expected_labels[variable]}"
    assert all(f"original-{label}" not in result.stdout + result.stderr for label in expected_labels.values())


@pytest.mark.parametrize(
    ("unsafe_kind", "expected_error"),
    (
        ("symlink", "must reference a readable non-symlink regular file"),
        ("empty", "must reference a non-empty bounded regular file"),
        ("multiline", "must contain exactly one non-empty secret value"),
        ("permissions", "must not grant group or other permissions"),
    ),
)
def test_delivery_wrapper_rejects_unsafe_compose_secret_files(
    tmp_path: Path,
    unsafe_kind: str,
    expected_error: str,
) -> None:
    root, wrapper = _delivery_script_sandbox(tmp_path, "compose-delivery.sh")
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir(mode=0o700)
    secret_paths = {
        "COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE": secret_dir / "admin",
        "COMPOSE_POSTGRES_OWNER_PASSWORD_FILE": secret_dir / "owner",
        "COMPOSE_POSTGRES_APP_PASSWORD_FILE": secret_dir / "app",
        "COMPOSE_WRITE_GUARD_CODE_FILE": secret_dir / "code",
        "COMPOSE_WRITE_GUARD_SESSION_SECRET_FILE": secret_dir / "session",
    }
    for secret_path in secret_paths.values():
        secret_path.write_text("test-only-secret\n", encoding="utf-8")
        secret_path.chmod(0o600)

    unsafe_path = secret_paths["COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE"]
    if unsafe_kind == "symlink":
        target = secret_dir / "admin-target"
        target.write_text("test-only-secret\n", encoding="utf-8")
        target.chmod(0o600)
        unsafe_path.unlink()
        unsafe_path.symlink_to(target.name)
    elif unsafe_kind == "empty":
        unsafe_path.write_text("", encoding="utf-8")
    elif unsafe_kind == "multiline":
        unsafe_path.write_text("test-only-secret\nsecond-line\n", encoding="utf-8")
    else:
        unsafe_path.chmod(0o644)

    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
case "$*" in
  *"config --environment"*)
    printf '%s\n' 'BACKEND_IMAGE_REPOSITORY=fj-review'
    printf '%s\n' 'BACKEND_IMAGE_DIGEST=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    printf 'COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE=%s\n' "$FAKE_ADMIN_SECRET"
    printf 'COMPOSE_POSTGRES_OWNER_PASSWORD_FILE=%s\n' "$FAKE_OWNER_SECRET"
    printf 'COMPOSE_POSTGRES_APP_PASSWORD_FILE=%s\n' "$FAKE_APP_SECRET"
    printf 'COMPOSE_WRITE_GUARD_CODE_FILE=%s\n' "$FAKE_CODE_SECRET"
    printf 'COMPOSE_WRITE_GUARD_SESSION_SECRET_FILE=%s\n' "$FAKE_SESSION_SECRET"
    ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    result = subprocess.run(
        ["sh", str(wrapper), "config", "--quiet"],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FAKE_ADMIN_SECRET": str(secret_paths["COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE"]),
            "FAKE_OWNER_SECRET": str(secret_paths["COMPOSE_POSTGRES_OWNER_PASSWORD_FILE"]),
            "FAKE_APP_SECRET": str(secret_paths["COMPOSE_POSTGRES_APP_PASSWORD_FILE"]),
            "FAKE_CODE_SECRET": str(secret_paths["COMPOSE_WRITE_GUARD_CODE_FILE"]),
            "FAKE_SESSION_SECRET": str(secret_paths["COMPOSE_WRITE_GUARD_SESSION_SECRET_FILE"]),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert expected_error in result.stderr
    assert "test-only-secret" not in result.stdout
    assert "test-only-secret" not in result.stderr


def test_backup_restore_smokes_use_exclusive_identity_bound_blob_lifecycle() -> None:
    root = Path(__file__).resolve().parents[2]
    for script_name in (
        "postgres-backup-restore-smoke.sh",
        "postgres-host-backup-restore-smoke.sh",
    ):
        script = (root / "scripts" / script_name).read_text(encoding="utf-8")
        assert "ensure_private_directory" in script
        assert "exclusive_file_writer" in script
        assert "unlink_regular_file_if_identity" in script
        assert "st_dev" in script
        assert "st_ino" in script
        assert "> \"$blob_path\"" not in script
        assert "from backend.app.safe_files import unlink_regular_file\n" not in script

    compose_smoke = (root / "scripts/postgres-backup-restore-smoke.sh").read_text(encoding="utf-8")
    assert compose_smoke.index("trap 'cleanup best_effort' EXIT") < compose_smoke.index("create_blob\n")
    assert "); then\n        blob_created=1\n    else" in compose_smoke
    assert compose_smoke.index("trap '' HUP INT TERM") < compose_smoke.index("if blob_identity=$(compose run")
    assert compose_smoke.index("blob_created=1") < compose_smoke.index("blob_device=${blob_identity%%:*}")
    assert "device=int(device)" in compose_smoke
    assert "inode=int(inode)" in compose_smoke
    assert "with pin_regular_file(target, root) as pinned" in compose_smoke

    host_smoke = (root / "scripts/postgres-host-backup-restore-smoke.sh").read_text(encoding="utf-8")
    assert host_smoke.index("trap cleanup EXIT") < host_smoke.index("blob_identity=$(")
    assert "); then\n    blob_created=1\nelse" in host_smoke
    assert host_smoke.index("trap '' HUP INT TERM") < host_smoke.index("if blob_identity=$(")
    assert host_smoke.index("blob_created=1") < host_smoke.index("blob_device=${blob_identity%%:*}")
    assert "device=int(device)" in host_smoke
    assert "inode=int(inode)" in host_smoke
    assert "with pin_regular_file(target, root) as pinned" in host_smoke


def test_backup_restore_smoke_arms_cleanup_before_create_function_failure(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    probe_root = tmp_path / "probe"
    scripts_dir = probe_root / "scripts"
    scripts_dir.mkdir(parents=True)
    smoke = (root / "scripts/postgres-backup-restore-smoke.sh").read_text(encoding="utf-8")
    (scripts_dir / "postgres-backup-restore-smoke.sh").write_text(smoke, encoding="utf-8")
    compose_log = probe_root / "compose.log"
    fake_compose = scripts_dir / "compose-delivery.sh"
    fake_compose.write_text(
        """#!/bin/sh
set -eu
if [ "${1:-}" = ps ] && [ "${2:-}" = -q ] && [ "${3:-}" = postgres ]; then
    printf '%s\n' fake-postgres-container
    exit 0
fi
owned_db=
expected_db_sentinel=
sentinel_file_id=
while [ "$#" -gt 0 ]; do
    if [ "$1" = -e ] && [ "$#" -ge 2 ]; then
        case "$2" in
            OWNED_DB=*) owned_db=${2#OWNED_DB=} ;;
            EXPECTED_DB_SENTINEL=*) expected_db_sentinel=${2#EXPECTED_DB_SENTINEL=} ;;
            SENTINEL_FILE_ID=*) sentinel_file_id=${2#SENTINEL_FILE_ID=} ;;
        esac
        shift 2
    else
        shift
    fi
done
if [ -n "$owned_db" ]; then
    printf '%s|%s\n' "$owned_db" "$expected_db_sentinel" >>"$FAKE_COMPOSE_LOG"
fi
cat >/dev/null
if [ -n "$owned_db" ]; then
    operation_count=$(wc -l <"$FAKE_COMPOSE_LOG" | tr -d ' ')
    if [ "$operation_count" -eq 1 ]; then
        exit 97
    fi
    exit 88
fi
if [ -n "$sentinel_file_id" ] && [ -z "$owned_db" ]; then
    printf '%s\n' '1:2'
fi
""",
        encoding="utf-8",
    )
    fake_compose.chmod(0o700)

    result = subprocess.run(
        ["sh", "scripts/postgres-backup-restore-smoke.sh"],
        cwd=probe_root,
        env={
            **os.environ,
            "FAKE_COMPOSE_LOG": str(compose_log),
            "FCR_BACKUP_WORKDIR_PYTHON": sys.executable,
            "FCR_BACKUP_WORKDIR_HELPER": str(root / "backend/scripts/backup_workdir.py"),
            "FCR_BACKUP_WORKDIR_PYTHONPATH": str(root),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "backup/restore smoke could not clean every owned temporary resource" in result.stderr
    database_operations = compose_log.read_text(encoding="utf-8").splitlines()
    assert len(database_operations) == 2
    assert database_operations[0] == database_operations[1]
    assert database_operations[0].endswith(":source")


def test_host_backup_cleanup_database_identity_gate(tmp_path: Path) -> None:
    host_smoke = (ROOT / "scripts/postgres-host-backup-restore-smoke.sh").read_text(encoding="utf-8")
    function_block = host_smoke[
        host_smoke.index("validate_owned_database_identity()") : host_smoke.index("cleanup()")
    ]
    harness = tmp_path / "cleanup-harness.sh"
    harness.write_text(
        """#!/bin/sh
set -eu
POSTGRES_OWNER_USER=fcr_owner
POSTGRES_ADMIN_PASSWORD=test-only-admin-secret
POSTGRES_ADMIN_HOST=127.0.0.1
POSTGRES_ADMIN_PORT=5432
POSTGRES_ADMIN_USER=fcr_admin
"""
        + function_block
        + """
database_identity() {
    if [ "${FAKE_IDENTITY_FAILURE:-0}" -eq 1 ]; then
        return 71
    fi
    printf '%s' "${FAKE_DATABASE_IDENTITY:-}"
}
drop_owned_database "$TEST_DATABASE_NAME" "$TEST_EXPECTED_IDENTITY"
""",
        encoding="utf-8",
    )
    harness.chmod(0o700)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_dropdb = fake_bin / "dropdb"
    fake_dropdb.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$DROP_LOG\"\n", encoding="utf-8")
    fake_dropdb.chmod(0o700)

    nonce = "a" * 32
    database_name = f"fcr_host_backup_{nonce}"
    expected_identity = f"fcr-host-backup-restore:{nonce}:source"
    cases = (
        ("database-absent", database_name, "", False, 0, False),
        ("expected-sentinel", database_name, f"fcr_owner:{expected_identity}", False, 0, True),
        ("empty-sentinel", database_name, "fcr_owner:", False, 1, False),
        ("wrong-owner", database_name, f"other_owner:{expected_identity}", False, 1, False),
        ("wrong-sentinel", database_name, "fcr_owner:unexpected", False, 1, False),
        ("wrong-random-name", f"fcr_host_backup_{'b' * 32}", f"fcr_owner:{expected_identity}", False, 2, False),
        ("identity-query-failed", database_name, "", True, 1, False),
    )
    for case_name, candidate_database, actual_identity, identity_failure, expected_status, expect_drop in cases:
        drop_log = tmp_path / f"{case_name}.log"
        result = subprocess.run(
            ["sh", str(harness)],
            env={
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "DROP_LOG": str(drop_log),
                "FAKE_DATABASE_IDENTITY": actual_identity,
                "FAKE_IDENTITY_FAILURE": "1" if identity_failure else "0",
                "TEST_DATABASE_NAME": candidate_database,
                "TEST_EXPECTED_IDENTITY": expected_identity,
            },
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == expected_status, case_name
        assert drop_log.exists() is expect_drop, case_name
        assert "test-only-admin-secret" not in result.stdout
        assert "test-only-admin-secret" not in result.stderr
        if identity_failure:
            assert "could not verify temporary database identity during cleanup" in result.stderr


def test_delivery_image_reference_validator_is_strict() -> None:
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts/validate-delivery-image-ref.sh"
    digest = "a" * 64

    for valid_repository in ("final-cut-review", "registry.example/final-cut-review", "registry.example:5000/final-cut-review"):
        result = subprocess.run(
            ["sh", str(script)],
            env={
                **os.environ,
                "BACKEND_IMAGE_REPOSITORY": valid_repository,
                "BACKEND_IMAGE_DIGEST": digest,
            },
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    for invalid_repository, invalid_digest in (
        ("final-cut-review", digest.upper()),
        ("", digest),
        ("final-cut-review", "latest"),
        ("final-cut-review", "abc"),
        ("registry.example/BadName", digest),
        ("final-cut-review:latest", digest),
        ("registry.example:5000/final-cut-review:latest", digest),
    ):
        result = subprocess.run(
            ["sh", str(script)],
            env={
                **os.environ,
                "BACKEND_IMAGE_REPOSITORY": invalid_repository,
                "BACKEND_IMAGE_DIGEST": invalid_digest,
            },
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0


def test_delivery_overlay_constructs_digest_only_image_reference() -> None:
    root = Path(__file__).resolve().parents[2]
    overlay = (root / "docker-compose.delivery.yml").read_text(encoding="utf-8")
    expected = "${BACKEND_IMAGE_REPOSITORY:?BACKEND_IMAGE_REPOSITORY is required}@sha256:${BACKEND_IMAGE_DIGEST:?BACKEND_IMAGE_DIGEST is required}"
    assert overlay.count(expected) == 4
    for service_name in ("migrate", "backend", "maintenance", "package-worker"):
        assert f"  {service_name}:\n    image: {expected}\n    build: !reset null" in overlay
    persistence = (root / "scripts/verify-compose-persistence.sh").read_text(encoding="utf-8")
    assert "sh scripts/compose-delivery.sh" in persistence
    wrapper = (root / "scripts/compose-delivery.sh").read_text(encoding="utf-8")
    assert 'sh "$script_dir/validate-delivery-image-ref.sh"' in wrapper
    assert "stat.S_IMODE(metadata.st_mode) & 0o077" in wrapper
    assert "stat.S_ISLNK(os.lstat(source).st_mode)" in wrapper
    assert "config --environment" in wrapper
    assert "source " not in wrapper
    entrypoint = (root / "backend/docker-entrypoint.sh").read_text(encoding="utf-8")
    assert "DELIVERY_IMAGE_REPOSITORY" in entrypoint
    assert "DELIVERY_IMAGE_DIGEST" in entrypoint
    postgres_entrypoint = (root / "ops/postgres/postgres-entrypoint.sh").read_text(encoding="utf-8")
    assert '[ "$version" != "16" ]' in postgres_entrypoint
    assert "exec docker-entrypoint.sh" in postgres_entrypoint
    postgres_dockerfile = (root / "ops/postgres/Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["postgres"]' in postgres_dockerfile


def test_delivery_wrapper_fails_closed_on_unmapped_legacy_project_volumes(
    tmp_path: Path,
) -> None:
    root, wrapper = _delivery_script_sandbox(tmp_path, "compose-delivery.sh")
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
case "$*" in
  *"config --environment"*)
    printf '%s\n' 'BACKEND_IMAGE_REPOSITORY=fj-review'
    printf '%s\n' 'BACKEND_IMAGE_DIGEST=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    if [ -n "${POSTGRES_VOLUME_NAME:-}" ]; then
      printf 'POSTGRES_VOLUME_NAME=%s\n' "$POSTGRES_VOLUME_NAME"
    fi
    ;;
  *"volume inspect fj-final-cut-review_fj-final-cut-review-postgres"*) exit 0 ;;
  *"volume inspect"*) exit 1 ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "POSTGRES_VOLUME_NAME": "renamed-postgres-volume",
    }

    blocked = subprocess.run(
        ["sh", str(wrapper), "up", "-d"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert blocked.returncode == 2
    assert "existing legacy Compose volume detected" in blocked.stderr

    selected = subprocess.run(
        ["sh", str(wrapper), "up", "-d"],
        cwd=root,
        env={
            **environment,
            "POSTGRES_VOLUME_NAME": "fj-final-cut-review_fj-final-cut-review-postgres",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert selected.returncode == 0, selected.stderr


@pytest.mark.parametrize(
    ("inspect_error", "expected_returncode"),
    (
        (
            "Error response from daemon: get fj-final-cut-review_fj-final-cut-review-postgres: no such volume",
            0,
        ),
        ("permission denied while trying to connect to the Docker daemon socket", 41),
        ("unable to resolve the current Docker context", 41),
        ("unexpected engine failure", 41),
    ),
)
def test_delivery_wrapper_only_allows_explicit_missing_legacy_volume(
    tmp_path: Path,
    inspect_error: str,
    expected_returncode: int,
) -> None:
    root, wrapper = _delivery_script_sandbox(tmp_path, "compose-delivery.sh")
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
case "$*" in
  *"config --environment"*)
    printf '%s\n' 'BACKEND_IMAGE_REPOSITORY=fj-review'
    printf '%s\n' 'BACKEND_IMAGE_DIGEST=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    printf '%s\n' 'POSTGRES_VOLUME_NAME=renamed-postgres-volume'
    exit 0
    ;;
esac
if [ "${1:-}" = info ]; then exit 0; fi
if [ "${1:-}" = volume ] && [ "${2:-}" = inspect ]; then
  printf '%s\n' "$INSPECT_ERROR" >&2
  exit 41
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    result = subprocess.run(
        ["sh", str(wrapper), "up", "-d"],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "INSPECT_ERROR": inspect_error,
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_returncode
    if expected_returncode == 0:
        assert "absence could not be verified" not in result.stderr
    else:
        assert result.stderr.strip() == "legacy Compose volume absence could not be verified for POSTGRES_VOLUME_NAME"
        assert inspect_error not in result.stdout + result.stderr


def test_delivery_wrapper_config_does_not_probe_docker_daemon(tmp_path: Path) -> None:
    root, wrapper = _delivery_script_sandbox(tmp_path, "compose-delivery.sh")
    fake_docker = tmp_path / "docker"
    docker_log = tmp_path / "docker.log"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >>"$DOCKER_LOG"
case "$*" in
  *"config --environment"*)
    printf '%s\n' 'BACKEND_IMAGE_REPOSITORY=fj-review'
    printf '%s\n' 'BACKEND_IMAGE_DIGEST=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    ;;
esac
if [ "${1:-}" = info ]; then
  sleep 5
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    result = subprocess.run(
        ["sh", str(wrapper), "config"],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "DOCKER_LOG": str(docker_log),
            "FCR_DOCKER_DAEMON_TIMEOUT_SECONDS": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=4,
    )

    assert result.returncode == 0, result.stderr
    assert not any(line == "info" for line in docker_log.read_text(encoding="utf-8").splitlines())


def test_delivery_wrapper_daemon_probe_timeout_has_dedicated_status(tmp_path: Path) -> None:
    root, wrapper = _delivery_script_sandbox(tmp_path, "compose-delivery.sh")
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
case "$*" in
  *"config --environment"*)
    printf '%s\n' 'BACKEND_IMAGE_REPOSITORY=fj-review'
    printf '%s\n' 'BACKEND_IMAGE_DIGEST=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    exit 0
    ;;
esac
if [ "${1:-}" = info ]; then
  sleep 5
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    result = subprocess.run(
        ["sh", str(wrapper), "ps"],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FCR_DOCKER_DAEMON_TIMEOUT_SECONDS": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=6,
    )

    assert result.returncode == 75
    assert result.stderr.strip() == "BLOCKED_DOCKER_DAEMON_TIMEOUT"


def test_delivery_wrapper_forwards_stdin_to_bounded_compose_command(tmp_path: Path) -> None:
    root, wrapper = _delivery_script_sandbox(tmp_path, "compose-delivery.sh")
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
case "$*" in
  *"config --environment"*)
    printf '%s\n' 'BACKEND_IMAGE_REPOSITORY=fj-review'
    printf '%s\n' 'BACKEND_IMAGE_DIGEST=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    exit 0
    ;;
esac
if [ "${1:-}" = info ]; then
  exit 0
fi
case "$*" in
  *" run "*) cat ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    payload = "bounded-compose-stdin-line-one\nbounded-compose-stdin-line-two\n"

    result = subprocess.run(
        ["sh", str(wrapper), "run", "--rm", "-T", "backend", "sh", "-seu"],
        cwd=root,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        input=payload,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == payload


def test_delivery_wrapper_runtime_command_timeout_is_bounded(tmp_path: Path) -> None:
    root, wrapper = _delivery_script_sandbox(tmp_path, "compose-delivery.sh")
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
case "$*" in
  *"config --environment"*)
    printf '%s\n' 'BACKEND_IMAGE_REPOSITORY=fj-review'
    printf '%s\n' 'BACKEND_IMAGE_DIGEST=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    exit 0
    ;;
esac
if [ "${1:-}" = info ]; then
  exit 0
fi
sleep 5
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    result = subprocess.run(
        ["sh", str(wrapper), "ps"],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FCR_DOCKER_COMMAND_TIMEOUT_SECONDS": "1",
        },
        input="timeout-stdin-must-not-disable-the-timeout\n",
        check=False,
        capture_output=True,
        text=True,
        timeout=6,
    )

    assert result.returncode == 75
    assert result.stderr.strip() == "BLOCKED_DOCKER_COMMAND_TIMEOUT"


def test_digest_resolver_daemon_probe_timeout_has_dedicated_status(tmp_path: Path) -> None:
    root, resolver = _delivery_script_sandbox(tmp_path, "resolve-delivery-image-digest.sh")
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
case "$*" in
  *"config --format json"*)
    printf '%s\n' '{"services":{"backend":{"image":"fj-review:local"}}}'
    exit 0
    ;;
esac
if [ "${1:-}" = info ]; then
  sleep 5
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    result = subprocess.run(
        ["sh", str(resolver), "--digest"],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FCR_DOCKER_DAEMON_TIMEOUT_SECONDS": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=6,
    )

    assert result.returncode == 75
    assert result.stderr.strip() == "BLOCKED_DOCKER_DAEMON_TIMEOUT"


def test_digest_resolver_selects_the_unique_source_repository_digest(tmp_path: Path) -> None:
    root, resolver = _delivery_script_sandbox(tmp_path, "resolve-delivery-image-digest.sh")
    fake_docker = tmp_path / "docker"
    source_repository = "registry.example/namespace/fj-review"
    source_digest = "a" * 64
    other_digest = "b" * 64
    fake_docker.write_text(
        """#!/bin/sh
case "$*" in
  *"config --format json"*)
    printf '%s\n' '{"services":{"backend":{"image":"registry.example/namespace/fj-review:release-1"}}}'
    exit 0
    ;;
esac
if [ "${1:-}" = info ]; then exit 0; fi
if [ "${1:-}" = image ] && [ "${2:-}" = inspect ]; then
  printf '%s\n' "registry.example/other/fj-review@sha256:$OTHER_DIGEST"
  printf '%s\n' "registry.example/namespace/fj-review@sha256:$SOURCE_DIGEST"
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "SOURCE_DIGEST": source_digest,
        "OTHER_DIGEST": other_digest,
    }

    repository_result = subprocess.run(
        ["sh", str(resolver), "--repository"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    digest_result = subprocess.run(
        ["sh", str(resolver), "--digest"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert repository_result.returncode == 0, repository_result.stderr
    assert repository_result.stdout.strip() == source_repository
    assert digest_result.returncode == 0, digest_result.stderr
    assert digest_result.stdout.strip() == source_digest
    resolver_source = resolver.read_text(encoding="utf-8")
    assert "{{index .RepoDigests 0}}" not in resolver_source
    assert "{{range .RepoDigests}}{{println .}}{{end}}" in resolver_source


@pytest.mark.parametrize(
    ("backend_image", "repo_digests"),
    (
        ("registry.example/namespace/fj-review:release-1", ""),
        (
            "registry.example/namespace/fj-review:release-1",
            f"registry.example/wrong/fj-review@sha256:{'a' * 64}\n",
        ),
        (
            "registry.example/namespace/fj-review:release-1",
            "".join(
                (
                    f"registry.example/namespace/fj-review@sha256:{'a' * 64}\n",
                    f"registry.example/namespace/fj-review@sha256:{'b' * 64}\n",
                )
            ),
        ),
        ("fj-review:release-1", f"fj-review@sha256:{'a' * 64}\n"),
        ("registry.example/namespace/fj-review:release-1", "not-a-repo-digest\n"),
    ),
)
def test_digest_resolver_fails_closed_without_one_registry_backed_source_match(
    tmp_path: Path,
    backend_image: str,
    repo_digests: str,
) -> None:
    root, resolver = _delivery_script_sandbox(tmp_path, "resolve-delivery-image-digest.sh")
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
case "$*" in
  *"config --format json"*)
    printf '{"services":{"backend":{"image":"%s"}}}\n' "$BACKEND_IMAGE_REF"
    exit 0
    ;;
esac
if [ "${1:-}" = info ]; then exit 0; fi
if [ "${1:-}" = image ] && [ "${2:-}" = inspect ]; then
  printf '%s' "$REPO_DIGESTS"
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    result = subprocess.run(
        ["sh", str(resolver), "--digest"],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "BACKEND_IMAGE_REF": backend_image,
            "REPO_DIGESTS": repo_digests,
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout.strip() == ""


@pytest.mark.parametrize(
    ("script_name", "arguments"),
    (
        ("compose-delivery.sh", ("config", "--quiet")),
        ("resolve-delivery-image-digest.sh", ("--digest",)),
    ),
)
def test_delivery_scripts_reject_symlinked_environment_files(
    tmp_path: Path,
    script_name: str,
    arguments: tuple[str, ...],
) -> None:
    root, script = _delivery_script_sandbox(tmp_path, script_name)
    private_env = tmp_path / "private.env"
    private_env.write_text("", encoding="utf-8")
    private_env.chmod(0o600)
    linked_env = root / ".env"
    linked_env.unlink()
    linked_env.symlink_to(private_env)

    result = subprocess.run(
        ["sh", str(script), *arguments],
        cwd=root,
        env=os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "regular non-symlink file" in result.stderr


def test_delivery_wrapper_global_options_preserve_config_as_daemonless(tmp_path: Path) -> None:
    root, wrapper = _delivery_script_sandbox(tmp_path, "compose-delivery.sh")
    fake_docker = tmp_path / "docker"
    docker_log = tmp_path / "docker.log"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >>"$DOCKER_LOG"
case "$*" in
  *"config --environment"*)
    printf '%s\n' 'BACKEND_IMAGE_REPOSITORY=fj-review'
    printf '%s\n' 'BACKEND_IMAGE_DIGEST=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    ;;
esac
if [ "${1:-}" = info ]; then
  sleep 5
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    result = subprocess.run(
        [
            "sh",
            str(wrapper),
            "--ansi",
            "never",
            "--all-resources",
            "--dry-run=false",
            "--parallel=2",
            "--profile",
            "delivery",
            "--progress",
            "plain",
            "config",
            "--quiet",
        ],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "DOCKER_LOG": str(docker_log),
            "FCR_DOCKER_DAEMON_TIMEOUT_SECONDS": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=4,
    )

    assert result.returncode == 0, result.stderr
    assert not any(line == "info" for line in docker_log.read_text(encoding="utf-8").splitlines())


def test_delivery_wrapper_config_timeout_has_command_status(tmp_path: Path) -> None:
    root, wrapper = _delivery_script_sandbox(tmp_path, "compose-delivery.sh")
    fake_docker = tmp_path / "docker"
    fake_docker.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    fake_docker.chmod(0o755)
    result = subprocess.run(
        ["sh", str(wrapper), "config", "--quiet"],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FCR_DOCKER_DAEMON_TIMEOUT_SECONDS": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=4,
    )

    assert result.returncode == 75
    assert result.stderr.strip() == "BLOCKED_DOCKER_COMMAND_TIMEOUT"


@pytest.mark.parametrize(
    "arguments",
    (
        ("-f", "override.yml", "config"),
        ("-foverride.yml", "config"),
        ("--file", "override.yml", "config"),
        ("--file=override.yml", "config"),
        ("--env-file", "override.env", "config"),
        ("--env-file=override.env", "config"),
        ("--project-directory", "/tmp/override", "config"),
        ("--project-directory=/tmp/override", "config"),
        ("-p", "override", "config"),
        ("-poverride", "config"),
        ("--project-name", "override", "config"),
        ("--project-name=override", "config"),
    ),
)
def test_delivery_wrapper_rejects_all_compose_control_overrides(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    root, wrapper = _delivery_script_sandbox(tmp_path, "compose-delivery.sh")

    result = subprocess.run(
        ["sh", str(wrapper), *arguments],
        cwd=root,
        env=os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "delivery Compose files, environment, project directory, and project name are fixed"


@pytest.mark.parametrize(
    ("script_name", "arguments"),
    (
        ("compose-delivery.sh", ("config", "--quiet")),
        ("resolve-delivery-image-digest.sh", ("--digest",)),
    ),
)
def test_delivery_scripts_pin_compose_files_and_honor_validated_project_overrides(
    tmp_path: Path,
    script_name: str,
    arguments: tuple[str, ...],
) -> None:
    root, script = _delivery_script_sandbox(tmp_path, script_name)
    fake_docker = tmp_path / "docker"
    docker_log = tmp_path / "docker.log"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >>"$DOCKER_LOG"
case "$*" in
  *"config --environment"*)
    printf '%s\n' 'BACKEND_IMAGE_REPOSITORY=fj-review'
    printf '%s\n' 'BACKEND_IMAGE_DIGEST=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    ;;
  *"config --format json"*)
    printf '%s\n' '{"services":{"backend":{"image":"registry.example/fj-review:release-1"}}}'
    ;;
esac
if [ "${1:-}" = info ]; then exit 0; fi
if [ "${1:-}" = image ]; then
  printf '%s\n' 'registry.example/fj-review@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    alternate_env = root / "alternate.env"
    alternate_env.write_text((root / ".env").read_text(encoding="utf-8"), encoding="utf-8")
    alternate_env.chmod(0o600)
    result = subprocess.run(
        ["sh", str(script), *arguments],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "DOCKER_LOG": str(docker_log),
            "FCR_COMPOSE_ENV_FILE": "alternate.env",
            "FCR_COMPOSE_PROJECT": "validated-project",
            "COMPOSE_FILE": "/tmp/attacker.yml",
            "COMPOSE_PROJECT_NAME": "attacker-project",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    compose_lines = [line for line in docker_log.read_text(encoding="utf-8").splitlines() if line.startswith("compose ")]
    assert compose_lines
    for line in compose_lines:
        tokens = shlex.split(line)
        env_snapshot = Path(tokens[tokens.index("--env-file") + 1])
        assert env_snapshot.name == "environment"
        assert env_snapshot.parent.name.startswith(("fcr-compose-env.", "fcr-digest-env."))
        assert Path(tokens[tokens.index("--project-directory") + 1]).samefile(root)
        assert tokens[tokens.index("-p") + 1] == "validated-project"
        compose_files = [Path(tokens[index + 1]) for index, token in enumerate(tokens) if token == "-f"]
        assert compose_files[0].samefile(root / "docker-compose.yml")
        if script_name == "compose-delivery.sh":
            assert len(compose_files) == 2
            assert compose_files[1].samefile(root / "docker-compose.delivery.yml")
        else:
            assert len(compose_files) == 1
        assert "attacker" not in line


@pytest.mark.parametrize(
    ("script_name", "arguments"),
    (
        ("compose-delivery.sh", ("ps",)),
        ("resolve-delivery-image-digest.sh", ("--digest",)),
    ),
)
def test_delivery_scripts_classify_nonzero_daemon_probe_without_leaking_stderr(
    tmp_path: Path,
    script_name: str,
    arguments: tuple[str, ...],
) -> None:
    root, script = _delivery_script_sandbox(tmp_path, script_name)
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
case "$*" in
  *"config --environment"*)
    printf '%s\n' 'BACKEND_IMAGE_REPOSITORY=fj-review'
    printf '%s\n' 'BACKEND_IMAGE_DIGEST=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    exit 0
    ;;
  *"config --format json"*)
    printf '%s\n' '{"services":{"backend":{"image":"fj-review:local"}}}'
    exit 0
    ;;
esac
if [ "${1:-}" = info ]; then
  printf '%s\n' 'sensitive daemon endpoint and transport details' >&2
  exit 42
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    result = subprocess.run(
        ["sh", str(script), *arguments],
        cwd=root,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 42
    assert result.stderr.strip() == "DOCKER_DAEMON_UNAVAILABLE"
    assert "endpoint" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("script_name", "arguments"),
    (
        ("compose-delivery.sh", ("ps",)),
        ("resolve-delivery-image-digest.sh", ("--digest",)),
    ),
)
def test_delivery_scripts_preserve_real_command_exit_124_without_timeout_label(
    tmp_path: Path,
    script_name: str,
    arguments: tuple[str, ...],
) -> None:
    root, script = _delivery_script_sandbox(tmp_path, script_name)
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
case "$*" in
  *"config --environment"*)
    printf '%s\n' 'BACKEND_IMAGE_REPOSITORY=fj-review'
    printf '%s\n' 'BACKEND_IMAGE_DIGEST=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    exit 0
    ;;
  *"config --format json"*)
    printf '%s\n' '{"services":{"backend":{"image":"registry.example/fj-review:release-1"}}}'
    exit 0
    ;;
esac
if [ "${1:-}" = info ]; then exit 0; fi
exit 124
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    result = subprocess.run(
        ["sh", str(script), *arguments],
        cwd=root,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 124
    assert "BLOCKED_DOCKER" not in result.stderr


def test_backend_image_pins_postgres_16_backup_client_runtime() -> None:
    if os.environ.get("RUN_DOCKER_RUNTIME_TESTS") != "1":
        pytest.skip("Docker runtime gate requires RUN_DOCKER_RUNTIME_TESTS=1")
    image = os.environ.get("BACKEND_RUNTIME_IMAGE", "")
    if not image:
        pytest.fail("BACKEND_RUNTIME_IMAGE is required for the Docker runtime gate")

    versions = _run(
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        image,
        "-ceu",
        "pg_dump --version; pg_restore --version",
    ).stdout.splitlines()
    assert len(versions) == 2
    assert versions[0].startswith("pg_dump (PostgreSQL) 16.")
    assert versions[1].startswith("pg_restore (PostgreSQL) 16.")


def test_root_without_dac_override_cannot_write_app_owned_data_but_app_user_can() -> None:
    if os.environ.get("RUN_DOCKER_RUNTIME_TESTS") != "1":
        pytest.skip("Docker runtime gate requires RUN_DOCKER_RUNTIME_TESTS=1")
    image = os.environ.get("BACKEND_RUNTIME_IMAGE", "")
    if not image:
        pytest.fail("BACKEND_RUNTIME_IMAGE is required for the Docker runtime gate")

    root_write = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--cap-drop",
            "ALL",
            "--entrypoint",
            "sh",
            image,
            "-ceu",
            "touch /data/root-write-must-fail",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert root_write.returncode != 0

    _run(
        "docker",
        "run",
        "--rm",
        "--cap-drop",
        "ALL",
        "--user",
        "10001:10001",
        "--entrypoint",
        "sh",
        image,
        "-ceu",
        "touch /data/app-write-succeeds",
    )


def test_backend_pg16_client_dump_restores_into_pg16_server() -> None:
    if os.environ.get("RUN_DOCKER_RUNTIME_TESTS") != "1":
        pytest.skip("Docker runtime gate requires RUN_DOCKER_RUNTIME_TESTS=1")
    backend_image = os.environ.get("BACKEND_RUNTIME_IMAGE", "")
    postgres_image = os.environ.get("POSTGRES_RUNTIME_IMAGE", "")
    if not backend_image:
        pytest.fail("BACKEND_RUNTIME_IMAGE is required for the Docker runtime gate")
    if not postgres_image:
        pytest.fail("POSTGRES_RUNTIME_IMAGE is required for the Docker runtime gate")

    suffix = uuid.uuid4().hex[:12]
    network = f"fcr-pg16-restore-{suffix}"
    server = f"fcr-pg16-restore-server-{suffix}"
    _run("docker", "network", "create", network)
    try:
        _run(
            "docker",
            "run",
            "-d",
            "--name",
            server,
            "--network",
            network,
            "--network-alias",
            "postgres",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,noexec,nosuid,size=256m",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "-e",
            "POSTGRES_USER=postgres",
            "-e",
            "POSTGRES_DB=postgres",
            postgres_image,
        )
        _run(
            "docker",
            "exec",
            server,
            "sh",
            "-ceu",
            "for attempt in $(seq 1 60); do pg_isready -U postgres -d postgres >/dev/null && exit 0; sleep 1; done; exit 1",
        )
        restored = _run(
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "--entrypoint",
            "sh",
            backend_image,
            "-ceu",
            "createdb -h postgres -U postgres source_db; "
            "psql -h postgres -U postgres -d source_db -v ON_ERROR_STOP=1 "
            "-c 'CREATE TABLE sentinel (value text NOT NULL)' "
            "-c \"INSERT INTO sentinel VALUES ('one'), ('two')\" >/dev/null; "
            "pg_dump -h postgres -U postgres -d source_db -Fc -f /tmp/source.dump; "
            "createdb -h postgres -U postgres restore_db; "
            "pg_restore --exit-on-error --no-owner --no-privileges "
            "-h postgres -U postgres -d restore_db /tmp/source.dump; "
            "psql -h postgres -U postgres -d restore_db -tAc 'SELECT count(*) FROM sentinel'",
        )
        assert restored.stdout.strip() == "2"
    finally:
        subprocess.run(["docker", "rm", "-f", server], check=False, capture_output=True, text=True)
        subprocess.run(["docker", "network", "rm", network], check=False, capture_output=True, text=True)


def test_entrypoint_migrates_legacy_volume_ownership_without_following_symlinks() -> None:
    if os.environ.get("RUN_DOCKER_RUNTIME_TESTS") != "1":
        pytest.skip("Docker runtime gate requires RUN_DOCKER_RUNTIME_TESTS=1")
    image = os.environ.get("BACKEND_RUNTIME_IMAGE", "")
    if not image:
        pytest.fail("BACKEND_RUNTIME_IMAGE is required for the Docker runtime gate")

    suffix = uuid.uuid4().hex[:12]
    data_volume = f"fcr_entrypoint_data_{suffix}"
    outside_volume = f"fcr_entrypoint_outside_{suffix}"
    state_volume = f"fcr_entrypoint_state_{suffix}"
    _run("docker", "volume", "create", data_volume)
    _run("docker", "volume", "create", outside_volume)
    _run("docker", "volume", "create", state_volume)
    try:
        _run(
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{data_volume}:/data",
            "-v",
            f"{outside_volume}:/outside",
            "-v",
            f"{state_volume}:/var/lib/fj-runtime-state",
            image,
            "-ceu",
            "touch /data/legacy /outside/sentinel; chown 999:998 /data/legacy; chown 997:996 /outside/sentinel; ln -s /outside/sentinel /data/outside-link",
        )
        first = _run(
            "docker",
            "run",
            "--rm",
            "-v",
            f"{data_volume}:/data",
            "-v",
            f"{outside_volume}:/outside",
            "-v",
            f"{state_volume}:/var/lib/fj-runtime-state",
            image,
            "sh",
            "-ceu",
            'test "$(id -u):$(id -g)" = 10001:10001; '
            'test "$(umask)" = 0077; '
            'test "$(stat -c %u:%g /data/legacy)" = 10001:10001; '
            'test "$(stat -c %u:%g /outside/sentinel)" = 997:996; '
            'test "$(readlink /data/outside-link)" = /outside/sentinel; '
            "test ! -w /var/lib/fj-runtime-state",
        )
        assert "data ownership migration required" in first.stderr

        second = _run(
            "docker",
            "run",
            "--rm",
            "-v",
            f"{data_volume}:/data",
            "-v",
            f"{outside_volume}:/outside",
            "-v",
            f"{state_volume}:/var/lib/fj-runtime-state",
            image,
            "sh",
            "-ceu",
            'test "$(id -u):$(id -g)" = 10001:10001; '
            'test "$(umask)" = 0077; '
            'test "$(stat -c %u:%g /data/legacy)" = 10001:10001; '
            'test "$(stat -c %u:%g /outside/sentinel)" = 997:996',
        )
        assert "data ownership migration required" not in second.stderr

        _run(
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{data_volume}:/data",
            image,
            "-ceu",
            "chown 995:994 /data/legacy",
        )
        forced = _run(
            "docker",
            "run",
            "--rm",
            "-e",
            "FORCE_DATA_OWNERSHIP_MIGRATION=1",
            "-v",
            f"{data_volume}:/data",
            "-v",
            f"{outside_volume}:/outside",
            "-v",
            f"{state_volume}:/var/lib/fj-runtime-state",
            image,
            "sh",
            "-ceu",
            'test "$(stat -c %u:%g /data/legacy)" = 10001:10001; test "$(stat -c %u:%g /outside/sentinel)" = 997:996',
        )
        assert "data ownership migration required" in forced.stderr
    finally:
        subprocess.run(
            ["docker", "volume", "rm", data_volume, outside_volume, state_volume],
            check=False,
            capture_output=True,
            text=True,
        )


def test_entrypoint_fails_closed_when_ownership_scan_fails(tmp_path: Path) -> None:
    if os.environ.get("RUN_DOCKER_RUNTIME_TESTS") != "1":
        pytest.skip("Docker runtime gate requires RUN_DOCKER_RUNTIME_TESTS=1")
    image = os.environ.get("BACKEND_RUNTIME_IMAGE", "")
    if not image:
        pytest.fail("BACKEND_RUNTIME_IMAGE is required for the Docker runtime gate")

    fake_find = tmp_path / "find"
    fake_find.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
    fake_find.chmod(0o700)
    suffix = uuid.uuid4().hex[:12]
    data_volume = f"fcr_entrypoint_scan_data_{suffix}"
    state_volume = f"fcr_entrypoint_scan_state_{suffix}"
    _run("docker", "volume", "create", data_volume)
    _run("docker", "volume", "create", state_volume)
    try:
        failed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--mount",
                f"type=bind,src={fake_find},dst=/usr/local/bin/find,readonly",
                "-v",
                f"{data_volume}:/data",
                "-v",
                f"{state_volume}:/var/lib/fj-runtime-state",
                image,
                "true",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert failed.returncode != 0
        assert "data ownership scan failed" in failed.stderr
        _run(
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{state_volume}:/state",
            image,
            "-ceu",
            'test -z "$(ls -A /state)"',
        )
    finally:
        subprocess.run(
            ["docker", "volume", "rm", data_volume, state_volume],
            check=False,
            capture_output=True,
            text=True,
        )


def test_entrypoint_serializes_parallel_ownership_migrations() -> None:
    if os.environ.get("RUN_DOCKER_RUNTIME_TESTS") != "1":
        pytest.skip("Docker runtime gate requires RUN_DOCKER_RUNTIME_TESTS=1")
    image = os.environ.get("BACKEND_RUNTIME_IMAGE", "")
    if not image:
        pytest.fail("BACKEND_RUNTIME_IMAGE is required for the Docker runtime gate")

    suffix = uuid.uuid4().hex[:12]
    data_volume = f"fcr_entrypoint_parallel_data_{suffix}"
    state_volume = f"fcr_entrypoint_parallel_state_{suffix}"
    containers = [f"fcr-entrypoint-parallel-{suffix}-a", f"fcr-entrypoint-parallel-{suffix}-b"]
    _run("docker", "volume", "create", data_volume)
    _run("docker", "volume", "create", state_volume)
    try:
        _run(
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{data_volume}:/data",
            image,
            "-ceu",
            "touch /data/legacy; chown 999:998 /data/legacy",
        )
        for name in containers:
            _run(
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "-v",
                f"{data_volume}:/data",
                "-v",
                f"{state_volume}:/var/lib/fj-runtime-state",
                image,
                "sh",
                "-ceu",
                'test "$(stat -c %u:%g /data/legacy)" = 10001:10001; sleep 1',
            )
        for name in containers:
            waited = _run("docker", "wait", name)
            assert waited.stdout.strip() == "0", _run("docker", "logs", name).stderr
    finally:
        subprocess.run(["docker", "rm", "-f", *containers], check=False, capture_output=True, text=True)
        subprocess.run(
            ["docker", "volume", "rm", data_volume, state_volume],
            check=False,
            capture_output=True,
            text=True,
        )


def test_entrypoint_recovers_an_abandoned_ownership_lock() -> None:
    if os.environ.get("RUN_DOCKER_RUNTIME_TESTS") != "1":
        pytest.skip("Docker runtime gate requires RUN_DOCKER_RUNTIME_TESTS=1")
    image = os.environ.get("BACKEND_RUNTIME_IMAGE", "")
    if not image:
        pytest.fail("BACKEND_RUNTIME_IMAGE is required for the Docker runtime gate")

    suffix = uuid.uuid4().hex[:12]
    data_volume = f"fcr_entrypoint_stale_data_{suffix}"
    state_volume = f"fcr_entrypoint_stale_state_{suffix}"
    _run("docker", "volume", "create", data_volume)
    _run("docker", "volume", "create", state_volume)
    try:
        _run(
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{state_volume}:/state",
            image,
            "-ceu",
            "mkdir /state/.ownership-lock; "
            "printf abandoned > /state/.ownership-lock/owner; "
            "touch /state/.ownership-lock/heartbeat; "
            "touch -d @1 /state/.ownership-lock /state/.ownership-lock/heartbeat",
        )
        _run(
            "docker",
            "run",
            "--rm",
            "-e",
            "DATA_OWNERSHIP_LOCK_STALE_SECONDS=6",
            "-v",
            f"{data_volume}:/data",
            "-v",
            f"{state_volume}:/var/lib/fj-runtime-state",
            image,
            "true",
        )
        _run(
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{state_volume}:/state",
            image,
            "-ceu",
            "test ! -e /state/.ownership-lock; test -z \"$(find /state -maxdepth 1 -name '.ownership-lock-stale-*' -print -quit)\"",
        )
    finally:
        subprocess.run(
            ["docker", "volume", "rm", data_volume, state_volume],
            check=False,
            capture_output=True,
            text=True,
        )


def test_entrypoint_rejects_stale_threshold_at_or_below_heartbeat_interval() -> None:
    if os.environ.get("RUN_DOCKER_RUNTIME_TESTS") != "1":
        pytest.skip("Docker runtime gate requires RUN_DOCKER_RUNTIME_TESTS=1")
    image = os.environ.get("BACKEND_RUNTIME_IMAGE", "")
    if not image:
        pytest.fail("BACKEND_RUNTIME_IMAGE is required for the Docker runtime gate")

    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            "DATA_OWNERSHIP_LOCK_STALE_SECONDS=5",
            image,
            "true",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "must exceed the heartbeat interval" in result.stderr


def test_postgres_image_rejects_incompatible_existing_data_volume() -> None:
    if os.environ.get("RUN_DOCKER_RUNTIME_TESTS") != "1":
        pytest.skip("Docker runtime gate requires RUN_DOCKER_RUNTIME_TESTS=1")
    image = os.environ.get("POSTGRES_RUNTIME_IMAGE", "")
    if not image:
        pytest.fail("POSTGRES_RUNTIME_IMAGE is required for the Docker runtime gate")

    volume = f"fcr_postgres_wrong_major_{uuid.uuid4().hex[:12]}"
    _run("docker", "volume", "create", volume)
    try:
        _run(
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{volume}:/var/lib/postgresql/data",
            image,
            "-ceu",
            "printf '17\\n' > /var/lib/postgresql/data/PG_VERSION",
        )
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-v",
                f"{volume}:/var/lib/postgresql/data",
                image,
                "postgres",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "major version mismatch" in result.stderr
    finally:
        subprocess.run(
            ["docker", "volume", "rm", volume],
            check=False,
            capture_output=True,
            text=True,
        )


def test_postgres_image_and_created_container_default_to_postgres_command() -> None:
    if os.environ.get("RUN_DOCKER_RUNTIME_TESTS") != "1":
        pytest.skip("Docker runtime gate requires RUN_DOCKER_RUNTIME_TESTS=1")
    image = os.environ.get("POSTGRES_RUNTIME_IMAGE", "")
    if not image:
        pytest.fail("POSTGRES_RUNTIME_IMAGE is required for the Docker runtime gate")

    image_command = json.loads(_run("docker", "image", "inspect", image, "--format", "{{json .Config.Cmd}}").stdout)
    assert image_command == ["postgres"]

    container = f"fcr-postgres-command-{uuid.uuid4().hex[:12]}"
    try:
        _run("docker", "create", "--name", container, image)
        container_command = json.loads(
            _run("docker", "container", "inspect", container, "--format", "{{json .Config.Cmd}}").stdout
        )
        assert container_command == ["postgres"]
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container],
            check=False,
            capture_output=True,
            text=True,
        )


def test_existing_volume_upgrade_probe_is_isolated_and_preserves_probe_volumes() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts" / "verify-postgres-existing-volume-upgrade.sh").read_text()
    helper = (root / "backend" / "scripts" / "legacy_volume_upgrade_probe.py").read_text()

    assert "fj-pg-upgrade-legacy-" in script
    assert "fj-pg-upgrade-current-" in script
    assert "legacy_compose=(" in script
    assert "current_compose=(" in script
    assert "POSTGRES_VOLUME_NAME={postgres_volume}" in script
    assert "--project-name" in script
    assert script.count("down --remove-orphans --rmi local") == 2
    assert "--volumes" not in script
    assert "down -v" not in script
    assert "docker volume rm" not in script
    assert "retained-volume: role=%s status=preserved" in script
    assert "retained-volume check failed: role=%s status=missing" in script
    assert "local -a expected_volume_roles=(postgres data runtime-state)" in script
    assert "retained_volume_names" not in script
    assert "--entrypoint /bin/true backend" in script
    assert 'probe_id="$(openssl rand -hex 16)"' in script
    assert "probe_resources_owned=0" in script
    assert 'docker volume inspect "$volume"' in script
    assert 'docker ps -aq --filter "label=com.docker.compose.project=$project"' in script
    assert "ALLOW_DESTRUCTIVE_LEGACY_VOLUME_PROBE=1" in script
    assert "fj_probe_" in helper
    assert "legacy sentinel was not preserved" in helper
    assert "runtime role retained DDL privileges" in helper
    assert "legacy table ownership was not transferred" in helper


def _run_existing_volume_upgrade_probe_with_fake_docker(
    tmp_path: Path,
    *,
    missing_before_cleanup_role: str | None = None,
    delete_on_down_role: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    docker_state_dir = tmp_path / "probe-volumes"
    docker = fake_bin / "docker"
    docker.write_text(
        r"""#!/bin/sh
set -eu
printf '%s\n' "$*" >>"$DOCKER_LOG"
volume_role() {
    case "$1" in
        *runtime-state) printf '%s\n' runtime-state ;;
        *postgres) printf '%s\n' postgres ;;
        *data) printf '%s\n' data ;;
        *) return 1 ;;
    esac
}
materialize_probe_volumes() {
    mkdir -p "$DOCKER_STATE_DIR"
    for role in postgres data runtime-state; do
        if [ "$role" != "$MISSING_BEFORE_CLEANUP_ROLE" ]; then
            : >"$DOCKER_STATE_DIR/$role"
        fi
    done
}
if [ "$1" = ps ] && [ "$2" = -aq ]; then
    exit 0
fi
if [ "$1" = network ] || [ "$1" = image ]; then
    exit 1
fi
if [ "$1" = volume ] && [ "$2" = inspect ]; then
    role=$(volume_role "$3") || exit 1
    [ -f "$DOCKER_STATE_DIR/$role" ]
    exit $?
fi
if [ "$1" = compose ]; then
    case " $* " in
        *" run "*|*" up "*) materialize_probe_volumes ;;
        *" down "*)
            if [ -n "$DELETE_ON_DOWN_ROLE" ]; then
                rm -f "$DOCKER_STATE_DIR/$DELETE_ON_DOWN_ROLE"
            fi
            ;;
    esac
    exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    result = subprocess.run(
        ["bash", "scripts/verify-postgres-existing-volume-upgrade.sh"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DOCKER_LOG": str(docker_log),
            "DOCKER_STATE_DIR": str(docker_state_dir),
            "MISSING_BEFORE_CLEANUP_ROLE": missing_before_cleanup_role or "",
            "DELETE_ON_DOWN_ROLE": delete_on_down_role or "",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    return result, docker_log.read_text(encoding="utf-8").splitlines()


def test_existing_volume_upgrade_probe_cleanup_retains_volumes(tmp_path: Path) -> None:
    result, calls = _run_existing_volume_upgrade_probe_with_fake_docker(
        tmp_path,
    )

    assert result.returncode == 0
    down_calls = [line for line in calls if line.startswith("compose ") and " down " in line]
    assert len(down_calls) == 2
    assert all("--volumes" not in line for line in down_calls)
    assert all(" down -v " not in f" {line} " for line in down_calls)
    assert any("--project-name fj-pg-upgrade-current-" in line for line in down_calls)
    assert any("--project-name fj-pg-upgrade-legacy-" in line for line in down_calls)
    assert not any(line.startswith("volume rm") for line in calls)
    assert result.stdout.count("retained-volume: role=") == 3
    assert "retained-volume: role=postgres status=preserved" in result.stdout
    assert "retained-volume: role=data status=preserved" in result.stdout
    assert "retained-volume: role=runtime-state status=preserved" in result.stdout
    assert result.stdout.rstrip().endswith(
        "Existing PostgreSQL 16 named-volume cross-project upgrade: PASS"
    )


@pytest.mark.parametrize("missing_role", ("postgres", "data", "runtime-state"))
def test_existing_volume_upgrade_probe_fails_if_expected_volume_was_not_materialized(
    tmp_path: Path,
    missing_role: str,
) -> None:
    result, calls = _run_existing_volume_upgrade_probe_with_fake_docker(
        tmp_path,
        missing_before_cleanup_role=missing_role,
    )

    assert result.returncode == 2
    assert (
        f"retained-volume precondition failed: role={missing_role} status=missing"
        in result.stderr
    )
    assert f"retained-volume check failed: role={missing_role} status=missing" in result.stderr
    assert "named-volume cross-project upgrade: PASS" not in result.stdout
    assert not any("--volumes" in line for line in calls)
    assert not any(" down -v " in f" {line} " for line in calls)
    assert not any(line.startswith("volume rm") for line in calls)


@pytest.mark.parametrize("missing_role", ("postgres", "data", "runtime-state"))
def test_existing_volume_upgrade_probe_cleanup_fails_if_volume_disappears(
    tmp_path: Path,
    missing_role: str,
) -> None:
    result, calls = _run_existing_volume_upgrade_probe_with_fake_docker(
        tmp_path,
        delete_on_down_role=missing_role,
    )

    assert result.returncode == 2
    assert f"retained-volume check failed: role={missing_role} status=missing" in result.stderr
    assert "named-volume cross-project upgrade: PASS" not in result.stdout
    assert not any("--volumes" in line for line in calls)
    assert not any(" down -v " in f" {line} " for line in calls)
    assert not any(line.startswith("volume rm") for line in calls)


def test_existing_volume_upgrade_probe_materializes_private_secret_files(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    report = tmp_path / "secret-report"
    docker = fake_bin / "docker"
    docker.write_text(
        r"""#!/bin/sh
set -eu
if [ "$1" = network ] || [ "$1" = volume ] || [ "$1" = image ]; then
    exit 1
fi
if [ "$1" = compose ]; then
    case " $* " in (*" down "*) exit 0;; esac
    env_file=
    while [ "$#" -gt 0 ]; do
        if [ "$1" = --env-file ] && [ "$#" -ge 2 ]; then
            env_file=$2
            break
        fi
        shift
    done
    python3 - "$env_file" "$UPGRADE_SECRET_REPORT" <<'PY'
import os
import stat
import sys
from pathlib import Path

environment_path = Path(sys.argv[1])
values = dict(
    line.split("=", 1)
    for line in environment_path.read_text(encoding="utf-8").splitlines()
    if line
)
direct_names = {
    "POSTGRES_ADMIN_PASSWORD",
    "POSTGRES_OWNER_PASSWORD",
    "POSTGRES_APP_PASSWORD",
    "WRITE_GUARD_CODE",
    "WRITE_GUARD_SESSION_SECRET",
}
if direct_names & values.keys():
    raise SystemExit("plaintext secret found in probe environment")
file_names = {
    "COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE",
    "COMPOSE_POSTGRES_OWNER_PASSWORD_FILE",
    "COMPOSE_POSTGRES_APP_PASSWORD_FILE",
    "COMPOSE_WRITE_GUARD_CODE_FILE",
    "COMPOSE_WRITE_GUARD_SESSION_SECRET_FILE",
}
if not file_names <= values.keys():
    raise SystemExit("probe secret-file mapping is incomplete")
for name in file_names:
    path = Path(values[name])
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise SystemExit("probe secret file is not a private regular file")
    payload = path.read_bytes()
    if not payload.endswith(b"\n") or not payload[:-1] or b"\n" in payload[:-1] or b"\r" in payload:
        raise SystemExit("probe secret file is not one non-empty line")
Path(sys.argv[2]).write_text("pass\n", encoding="utf-8")
PY
    exit 91
fi
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    result = subprocess.run(
        ["bash", "scripts/verify-postgres-existing-volume-upgrade.sh"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "UPGRADE_SECRET_REPORT": str(report),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 91
    assert report.read_text(encoding="utf-8") == "pass\n"
    assert "password" not in result.stdout.lower()


def test_existing_volume_upgrade_probe_refuses_preexisting_project_resources(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >>"$DOCKER_LOG"
if [ "$1" = ps ] && [ "$2" = -aq ]; then
  printf '%s\n' existing-container
fi
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", "scripts/verify-postgres-existing-volume-upgrade.sh"],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DOCKER_LOG": str(docker_log),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "refusing to reuse existing Compose project resources" in result.stderr
    calls = docker_log.read_text(encoding="utf-8")
    assert not any(line.startswith("compose ") for line in calls.splitlines())
    assert "volume rm" not in calls


def test_security_gate_scans_untracked_candidates_without_printing_secret(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    probe = tmp_path / "probe"
    (probe / "scripts").mkdir(parents=True)
    (probe / "backend/.venv/bin").mkdir(parents=True)
    shutil.copy2(root / "scripts/run-security-gate.sh", probe / "scripts/run-security-gate.sh")
    (probe / ".env.example").write_text("DATABASE_URL=\n", encoding="utf-8")
    (probe / "safe.txt").write_text("safe\n", encoding="utf-8")
    leaked = probe / "candidate.txt"
    leaked.write_text(
        "postgresql://" + "candidate:" + "do-not-print" + "@localhost/test\n",
        encoding="utf-8",
    )
    for executable in ("pip", "pip-audit", "pytest"):
        path = probe / "backend/.venv/bin" / executable
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    npm = fake_bin / "npm"
    npm.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    npm.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=probe, check=True)
    subprocess.run(
        ["git", "add", ".env.example", "safe.txt", "scripts/run-security-gate.sh"],
        cwd=probe,
        check=True,
    )
    environment = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

    blocked = subprocess.run(
        ["bash", "scripts/run-security-gate.sh"],
        cwd=probe,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert blocked.returncode != 0
    assert "security gate: blocked; violations=1" in blocked.stderr
    assert "candidate.txt" not in blocked.stderr
    assert str(leaked) not in blocked.stderr
    assert "do-not-print" not in blocked.stderr
    assert "do-not-print" not in blocked.stdout

    leaked.write_text("safe candidate\n", encoding="utf-8")
    passed = subprocess.run(
        ["bash", "scripts/run-security-gate.sh"],
        cwd=probe,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert passed.returncode == 0, passed.stderr
    assert "security gate: PASS" in passed.stdout

    tracked_env = probe / ".env.private"
    tracked_env.write_text("TOKEN=tracked-env-secret\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env.private"], cwd=probe, check=True)
    tracked_env_result = subprocess.run(
        ["bash", "scripts/run-security-gate.sh"],
        cwd=probe,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert tracked_env_result.returncode != 0
    assert (
        "security gate: blocked; tracked environment files=1"
        in tracked_env_result.stderr
    )
    assert ".env.private" not in tracked_env_result.stderr
    assert "tracked-env-secret" not in tracked_env_result.stderr
    assert "tracked-env-secret" not in tracked_env_result.stdout


def test_security_gate_fails_closed_when_python_dependency_audit_fails(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    probe = tmp_path / "probe"
    (probe / "scripts").mkdir(parents=True)
    (probe / "backend/.venv/bin").mkdir(parents=True)
    shutil.copy2(root / "scripts/run-security-gate.sh", probe / "scripts/run-security-gate.sh")
    (probe / ".env.example").write_text("DATABASE_URL=\n", encoding="utf-8")
    for executable in ("pip", "pytest"):
        path = probe / "backend/.venv/bin" / executable
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    audit_log = tmp_path / "pip-audit.log"
    pip_audit = probe / "backend/.venv/bin/pip-audit"
    pip_audit.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >\"$PIP_AUDIT_LOG\"\nexit 73\n",
        encoding="utf-8",
    )
    pip_audit.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    npm = fake_bin / "npm"
    npm.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    npm.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=probe, check=True)
    subprocess.run(
        ["git", "add", ".env.example", "scripts/run-security-gate.sh"],
        cwd=probe,
        check=True,
    )

    result = subprocess.run(
        ["bash", "scripts/run-security-gate.sh"],
        cwd=probe,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PIP_AUDIT_LOG": str(audit_log),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 73
    assert "security gate: PASS" not in result.stdout
    arguments = audit_log.read_text(encoding="utf-8")
    assert "--requirement backend/requirements-dev.txt" in arguments
    assert "--strict" in arguments
    assert "--progress-spinner off" in arguments


def test_security_gate_structurally_scans_lockfile_credentials(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    probe = tmp_path / "probe"
    (probe / "scripts").mkdir(parents=True)
    (probe / "backend/.venv/bin").mkdir(parents=True)
    shutil.copy2(root / "scripts/run-security-gate.sh", probe / "scripts/run-security-gate.sh")
    (probe / ".env.example").write_text("DATABASE_URL=\n", encoding="utf-8")
    lockfile = probe / "package-lock.json"
    lockfile.write_text(
        """{
  "name": "lockfile-security-probe",
  "lockfileVersion": 3,
  "packages": {
    "": {"name": "lockfile-security-probe"},
    "node_modules/example": {
      "version": "1.0.0",
      "resolved": "https://registry.npmjs.org/example/-/example-1.0.0.tgz",
      "integrity": "sha512-YWJjZGVmZw==",
      "dependencies": {"token": "^1.0.0"}
    }
  },
  "dependencies": {"token": {"version": "1.0.0"}}
}
""",
        encoding="utf-8",
    )
    for executable in ("pip", "pip-audit", "pytest"):
        path = probe / "backend/.venv/bin" / executable
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    npm = fake_bin / "npm"
    npm.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    npm.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=probe, check=True)
    subprocess.run(
        ["git", "add", ".env.example", "package-lock.json", "scripts/run-security-gate.sh"],
        cwd=probe,
        check=True,
    )
    environment = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

    ordinary = subprocess.run(
        ["bash", "scripts/run-security-gate.sh"],
        cwd=probe,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert ordinary.returncode == 0, ordinary.stderr

    credential_urls = (
        "git+" + "http://" + "registry-user:git-http-secret@registry.example/example.tgz",
        "git+" + "https://" + "git-https-secret@registry.example/example.tgz",
    )
    for credential_url in credential_urls:
        lockfile.write_text(
            json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {"node_modules/example": {"resolved": credential_url}},
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", "scripts/run-security-gate.sh"],
            cwd=probe,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "security gate: blocked; violations=1" in result.stderr
        assert "package-lock.json" not in result.stderr
        assert credential_url not in result.stderr
        assert credential_url not in result.stdout

    sensitive_keys = (
        "_authToken",
        "authToken",
        "npmAuthToken",
        "accessToken",
        "bearerToken",
        "registry auth",
        "//registry.example/:_authToken",
    )
    for index, sensitive_key in enumerate(sensitive_keys):
        credential_value = f"credential-value-{index}"
        lockfile.write_text(
            json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {},
                    sensitive_key: credential_value,
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", "scripts/run-security-gate.sh"],
            cwd=probe,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "security gate: blocked; violations=1" in result.stderr
        assert "package-lock.json" not in result.stderr
        assert credential_value not in result.stderr
        assert credential_value not in result.stdout

    lockfile_target = probe / "safe-lock-target.json"
    lockfile_target.write_text(
        '{"lockfileVersion":3,"packages":{}}',
        encoding="utf-8",
    )
    lockfile.unlink()
    lockfile.symlink_to(lockfile_target.name)
    symlink_result = subprocess.run(
        ["bash", "scripts/run-security-gate.sh"],
        cwd=probe,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert symlink_result.returncode != 0
    assert "security gate: blocked; violations=1" in symlink_result.stderr
    assert "package-lock.json" not in symlink_result.stderr
    assert "safe-lock-target.json" not in symlink_result.stderr


def test_existing_volume_upgrade_helper_rejects_unscoped_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.scripts import legacy_volume_upgrade_probe

    values = {
        "ALLOW_DESTRUCTIVE_LEGACY_VOLUME_PROBE": "1",
        "POSTGRES_ADMIN_HOST": "postgres",
        "POSTGRES_ADMIN_PORT": "5432",
        "POSTGRES_ADMIN_DB": "postgres",
        "POSTGRES_ADMIN_USER": "postgres",
        "POSTGRES_ADMIN_PASSWORD": "test-only-admin",
        "POSTGRES_APP_DB": "production_database",
        "POSTGRES_APP_USER": "fj_probe_scoped_app",
        "POSTGRES_APP_PASSWORD": "test-only-app",
        "POSTGRES_OWNER_USER": "fj_probe_scoped_owner",
        "LEGACY_VOLUME_PROBE_SENTINEL": "test-only-sentinel",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match="fj_probe_ namespace"):
        legacy_volume_upgrade_probe._config()
