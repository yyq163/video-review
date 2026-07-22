#!/bin/sh
set -eu

compose_env_file=${FCR_COMPOSE_ENV_FILE:-.env}
compose_project=${FCR_COMPOSE_PROJECT:-fj-final-cut-review}
compose() {
    FCR_COMPOSE_ENV_FILE="$compose_env_file" FCR_COMPOSE_PROJECT="$compose_project" \
        sh scripts/compose-delivery.sh "$@"
}

compose_psql_python='import os
from backend.scripts.bootstrap_database_roles import read_secret_from_environment

secret_name = "DATABASE_PASSWORD"
if os.environ.get(secret_name) or not os.environ.get(f"{secret_name}_FILE"):
    raise SystemExit("Compose database commands require DATABASE_PASSWORD_FILE only")
environment = os.environ.copy()
environment["PGPASSWORD"] = read_secret_from_environment(secret_name)
arguments = [
    "psql",
    "-h", os.environ["DATABASE_HOST"],
    "-p", os.environ["DATABASE_PORT"],
    "-U", os.environ["DATABASE_USER"],
    "-d", os.environ["DATABASE_NAME"],
    "-v", "ON_ERROR_STOP=1",
    "-Atq",
]
os.execvpe(arguments[0], arguments, environment)'

sentinel_suffix="$(openssl rand -hex 16)"
sentinel_project_id="prj_persistence_$sentinel_suffix"
sentinel_file_id="file_persistence_$sentinel_suffix"
sentinel_upload_id="upl_persistence_$sentinel_suffix"
sentinel_code="PERSIST_$sentinel_suffix"
sentinel_blob='fcr-compose-persistence-sentinel'
sentinel_blob_size=$(printf '%s' "$sentinel_blob" | wc -c | tr -d ' ')
sentinel_reserved_bytes=$((sentinel_blob_size * 2))
sentinel_blob_created=0
sentinel_rows_created=0
writers_stopped=0
if command -v sha256sum >/dev/null 2>&1; then
    sentinel_blob_sha=$(printf '%s' "$sentinel_blob" | sha256sum | awk '{print $1}')
else
    sentinel_blob_sha=$(printf '%s' "$sentinel_blob" | shasum -a 256 | awk '{print $1}')
fi

runtime_psql() {
    compose exec -T --user 10001:10001 backend python -c "$compose_psql_python"
}

create_sentinel() {
    compose exec -T --user 10001:10001 \
        -e SENTINEL_FILE_ID="$sentinel_file_id" \
        backend python -c '
import os
from pathlib import Path
from backend.app.safe_files import exclusive_file_writer
file_id = os.environ["SENTINEL_FILE_ID"]
if not file_id.startswith("file_persistence_") or not file_id.replace("_", "").isalnum():
    raise SystemExit(2)
root = Path("/data/storage")
target = root / "files" / file_id
with exclusive_file_writer(target, root) as handle:
    handle.write(b"fcr-compose-persistence-sentinel")
' >/dev/null
    sentinel_blob_created=1
    runtime_psql <<SQL
BEGIN;
INSERT INTO project_refs (
    id, project_code, project_name, source, description, lifecycle_status, lock_version, created_at, updated_at
) VALUES (
    '$sentinel_project_id', '$sentinel_code', 'Persistence sentinel', 'local',
    'Compose persistence sentinel description', 'active', 1, now(), now()
);
INSERT INTO file_objects (
    id, original_filename, mime_type, file_size, sha256, storage_path,
    owner_principal_id, owner_principal_kind, duration_ms, width, height,
    fps_num, fps_den, media_probe_version, created_at
) VALUES (
    '$sentinel_file_id', 'persistence-sentinel.mp4', 'video/mp4',
    $sentinel_blob_size, '$sentinel_blob_sha', '/data/storage/files/$sentinel_file_id',
    'persistence-smoke', 'system', 1000, 16, 16, 25, 1, 'persistence-smoke', now()
);
INSERT INTO upload_sessions (
    id, original_filename, mime_type, declared_size, expected_sha256, status,
    temp_path, owner_principal_id, owner_principal_kind, received_parts,
    reserved_bytes, parts_cleanup_confirmed_at, file_id,
    duration_ms, width, height, fps_num, fps_den, created_at, updated_at
) VALUES (
    '$sentinel_upload_id', 'persistence-sentinel.mp4', 'video/mp4',
    $sentinel_blob_size, '$sentinel_blob_sha', 'completed',
    '/data/storage/uploads/$sentinel_upload_id', 'persistence-smoke', 'system',
    '{}', $sentinel_reserved_bytes, now(), '$sentinel_file_id',
    1000, 16, 16, 25, 1, now(), now()
);
COMMIT;
SQL
    sentinel_rows_created=1
}

cleanup_sentinel() {
    cleanup_mode=${1:-best_effort}
    cleanup_failed=0
    if [ "$writers_stopped" -eq 1 ]; then
        if restore_four_services >/dev/null 2>&1; then
            writers_stopped=0
        else
            cleanup_failed=1
        fi
    fi
    if [ "$sentinel_rows_created" -eq 1 ]; then
        if runtime_psql <<SQL >/dev/null 2>&1
BEGIN;
DELETE FROM upload_sessions
WHERE id = '$sentinel_upload_id'
  AND owner_principal_id = 'persistence-smoke'
  AND owner_principal_kind = 'system';
DELETE FROM file_objects
WHERE id = '$sentinel_file_id'
  AND owner_principal_id = 'persistence-smoke'
  AND owner_principal_kind = 'system'
  AND sha256 = '$sentinel_blob_sha';
DELETE FROM project_refs
WHERE id = '$sentinel_project_id'
  AND project_code = '$sentinel_code'
  AND project_name = 'Persistence sentinel';
COMMIT;
SQL
        then
            sentinel_rows_created=0
        else
            cleanup_failed=1
        fi
    fi
    if [ "$sentinel_blob_created" -eq 1 ]; then
        if compose exec -T --user 10001:10001 \
        -e SENTINEL_FILE_ID="$sentinel_file_id" \
        -e SENTINEL_BLOB_SIZE="$sentinel_blob_size" \
        -e SENTINEL_BLOB_SHA256="$sentinel_blob_sha" \
        backend python -c '
import os
from pathlib import Path
from backend.app.safe_files import unlink_regular_file
from backend.scripts.verify_storage_blob import verify_storage_blob
file_id = os.environ["SENTINEL_FILE_ID"]
if file_id.startswith("file_persistence_") and file_id.replace("_", "").isalnum():
    root = Path("/data/storage")
    verify_storage_blob(
        root,
        file_id,
        int(os.environ["SENTINEL_BLOB_SIZE"]),
        os.environ["SENTINEL_BLOB_SHA256"],
    )
    unlink_regular_file(root / "files" / file_id, root)
' >/dev/null 2>&1
        then
            sentinel_blob_created=0
        else
            cleanup_failed=1
        fi
    fi
    if [ "$cleanup_mode" = strict ] && [ "$cleanup_failed" -ne 0 ]; then
        printf '%s\n' 'persistence smoke could not clean every owned sentinel resource' >&2
        return 1
    fi
}

trap 'cleanup_sentinel best_effort' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

database_snapshot() {
    {
        if snapshot_psql <<'SQL'
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
\echo __FCR_TABLE__ annotation_sets
COPY (SELECT row_to_json(t)::text FROM annotation_sets t ORDER BY row_to_json(t)::text) TO STDOUT;
\echo __FCR_TABLE__ file_objects
COPY (SELECT row_to_json(t)::text FROM file_objects t ORDER BY row_to_json(t)::text) TO STDOUT;
\echo __FCR_TABLE__ finalizations
COPY (SELECT row_to_json(t)::text FROM finalizations t ORDER BY row_to_json(t)::text) TO STDOUT;
\echo __FCR_TABLE__ idempotency_records
COPY (SELECT row_to_json(t)::text FROM idempotency_records t ORDER BY row_to_json(t)::text) TO STDOUT;
\echo __FCR_TABLE__ issue_revisions
COPY (SELECT row_to_json(t)::text FROM issue_revisions t ORDER BY row_to_json(t)::text) TO STDOUT;
\echo __FCR_TABLE__ operation_logs
COPY (SELECT row_to_json(t)::text FROM operation_logs t ORDER BY row_to_json(t)::text) TO STDOUT;
\echo __FCR_TABLE__ outbox_consumer_receipts
COPY (SELECT row_to_json(t)::text FROM outbox_consumer_receipts t ORDER BY row_to_json(t)::text) TO STDOUT;
\echo __FCR_TABLE__ outbox_events
COPY (SELECT row_to_json(t)::text FROM outbox_events t ORDER BY row_to_json(t)::text) TO STDOUT;
\echo __FCR_TABLE__ package_snapshots
COPY (SELECT row_to_json(t)::text FROM package_snapshots t ORDER BY row_to_json(t)::text) TO STDOUT;
\echo __FCR_TABLE__ project_refs
COPY (SELECT row_to_json(t)::text FROM project_refs t ORDER BY row_to_json(t)::text) TO STDOUT;
\echo __FCR_TABLE__ review_decisions
COPY (SELECT row_to_json(t)::text FROM review_decisions t ORDER BY row_to_json(t)::text) TO STDOUT;
\echo __FCR_TABLE__ review_issues
COPY (SELECT row_to_json(t)::text FROM review_issues t ORDER BY row_to_json(t)::text) TO STDOUT;
\echo __FCR_TABLE__ review_items
COPY (SELECT row_to_json(t)::text FROM review_items t ORDER BY row_to_json(t)::text) TO STDOUT;
\echo __FCR_TABLE__ review_versions
COPY (SELECT row_to_json(t)::text FROM review_versions t ORDER BY row_to_json(t)::text) TO STDOUT;
\echo __FCR_TABLE__ thread_messages
COPY (SELECT row_to_json(t)::text FROM thread_messages t ORDER BY row_to_json(t)::text) TO STDOUT;
\echo __FCR_TABLE__ upload_sessions
COPY (SELECT row_to_json(t)::text FROM upload_sessions t ORDER BY row_to_json(t)::text) TO STDOUT;
COMMIT;
\echo __FCR_END__
SQL
        then
            printf '%s\n' '__FCR_PSQL_STATUS__ 0'
        else
            snapshot_status=$?
            printf '__FCR_PSQL_STATUS__ %s\n' "$snapshot_status"
        fi
    } | python3 -c '
import hashlib
import sys

table_prefix = b"__FCR_TABLE__ "
status_prefix = b"__FCR_PSQL_STATUS__ "
expected_tables = [
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
]
snapshots = []
current_table = None
current_count = 0
current_digest = None
end_seen = False
psql_status = None

def finish_table():
    if current_table is not None:
        snapshots.append((current_table, current_count, current_digest.hexdigest()))

for raw_line in sys.stdin.buffer:
    line = raw_line.rstrip(b"\r\n")
    if line.startswith(table_prefix):
        if end_seen or psql_status is not None:
            raise SystemExit("invalid persistence snapshot stream ordering")
        finish_table()
        current_table = line[len(table_prefix):].decode("ascii", errors="strict")
        current_count = 0
        current_digest = hashlib.sha256()
        continue
    if line == b"__FCR_END__":
        finish_table()
        current_table = None
        current_digest = None
        end_seen = True
        continue
    if line.startswith(status_prefix):
        psql_status = int(line[len(status_prefix):])
        continue
    if current_table is None or current_digest is None or end_seen:
        raise SystemExit("invalid persistence snapshot stream")
    current_digest.update(len(line).to_bytes(8, byteorder="big"))
    for offset in range(0, len(line), 64 * 1024):
        current_digest.update(line[offset : offset + 64 * 1024])
    current_count += 1

if not end_seen or psql_status != 0:
    raise SystemExit("persistence snapshot transaction failed")
if [name for name, _count, _digest in snapshots] != expected_tables:
    raise SystemExit("persistence snapshot table sequence is incomplete")
for name, count, digest in snapshots:
    print(f"{name}:{count}:{digest}")
'
}

snapshot_psql() {
    compose run --rm --no-deps -T --user 10001:10001 --entrypoint python \
        backend -c "$compose_psql_python"
}

file_association_count() {
    compose run --rm --no-deps -T --user 10001:10001 --entrypoint python \
        -e VERIFY_FILE_ID="$sentinel_file_id" \
        -e VERIFY_BLOB_SIZE="$sentinel_blob_size" \
        -e VERIFY_BLOB_SHA256="$sentinel_blob_sha" \
        backend -c '
import os
from pathlib import Path
from backend.app.main import database_readiness
from backend.scripts.verify_storage_blob import verify_storage_blob
result = database_readiness()
verify_storage_blob(
    Path("/data/storage"),
    os.environ["VERIFY_FILE_ID"],
    int(os.environ["VERIFY_BLOB_SIZE"]),
    os.environ["VERIFY_BLOB_SHA256"],
)
print(int(result["file_associations"]))
'
}

fingerprint() {
    printf '%s' "$1" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
}

assert_four_services_healthy() {
    compose ps --format json postgres backend maintenance package-worker | python3 -c '
import json
import sys

expected = {"postgres", "backend", "maintenance", "package-worker"}
payload = sys.stdin.read().strip()
if not payload:
    raise SystemExit("Compose returned no service health records")
try:
    parsed = json.loads(payload)
    records = parsed if isinstance(parsed, list) else [parsed]
except json.JSONDecodeError:
    records = [json.loads(line) for line in payload.splitlines() if line.strip()]

by_service = {record.get("Service"): record for record in records}
if set(by_service) != expected:
    raise SystemExit("Compose did not return exactly the four required services")
unhealthy = [
    service
    for service in sorted(expected)
    if str(by_service[service].get("State", "")).lower() != "running"
    or str(by_service[service].get("Health", "")).lower() != "healthy"
]
if unhealthy:
    raise SystemExit("required services are not healthy: " + ", ".join(unhealthy))
'
}

restore_four_services() {
    compose up -d --wait --force-recreate --wait-timeout 180 >/dev/null
    assert_four_services_healthy
    writers_stopped=0
}

restart_four_services_in_dependency_order() {
    compose restart postgres >/dev/null
    restore_four_services
}

stop_writers() {
    writers_stopped=1
    compose stop backend maintenance package-worker >/dev/null
}

create_sentinel
compose exec -T --user 10001:10001 backend python -m backend.app.maintenance cleanup >/dev/null

stop_writers
before=$(database_snapshot)
before_files=$(file_association_count)
before_fingerprint=$(fingerprint "$before")

restart_four_services_in_dependency_order
stop_writers
after_restart=$(database_snapshot)
after_restart_files=$(file_association_count)
test "$after_restart" = "$before"
test "$after_restart_files" = "$before_files"

compose down >/dev/null
restore_four_services
stop_writers
after_down_up=$(database_snapshot)
after_down_up_files=$(file_association_count)
test "$after_down_up" = "$before"
test "$after_down_up_files" = "$before_files"

restore_four_services

printf 'snapshot_before_sha256=%s\n' "$before_fingerprint"
printf 'snapshot_after_restart_sha256=%s\n' "$(fingerprint "$after_restart")"
printf 'snapshot_after_down_up_sha256=%s\n' "$(fingerprint "$after_down_up")"
printf 'file_associations_before=%s\n' "$before_files"
printf 'file_associations_after_restart=%s\n' "$after_restart_files"
printf 'file_associations_after_down_up=%s\n' "$after_down_up_files"
printf '%s\n' 'restart_snapshot_match=pass'
printf '%s\n' 'down_up_snapshot_match=pass'
cleanup_sentinel strict
assert_four_services_healthy
trap - EXIT HUP INT TERM
