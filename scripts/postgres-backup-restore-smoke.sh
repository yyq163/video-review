#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd -P)
cd "$project_root"

compose_env_file=${FCR_COMPOSE_ENV_FILE:-.env}
compose_project=${FCR_COMPOSE_PROJECT:-fj-final-cut-review}
backup_workdir_python=${FCR_BACKUP_WORKDIR_PYTHON:-backend/.venv/bin/python}
backup_workdir_helper=${FCR_BACKUP_WORKDIR_HELPER:-backend/scripts/backup_workdir.py}
backup_workdir_pythonpath=${FCR_BACKUP_WORKDIR_PYTHONPATH:-.}

compose() {
    FCR_COMPOSE_ENV_FILE="$compose_env_file" FCR_COMPOSE_PROJECT="$compose_project" \
        sh "$script_dir/compose-delivery.sh" "$@"
}

compose_secret_exec_python='import os
import sys

from backend.scripts.bootstrap_database_roles import read_secret_from_environment

secret_name = sys.argv[1]
command = sys.argv[2:]
allowed_secrets = {
    "POSTGRES_ADMIN_PASSWORD",
    "POSTGRES_OWNER_PASSWORD",
    "POSTGRES_APP_PASSWORD",
}
allowed_commands = {"createdb", "dropdb", "pg_dump", "pg_restore", "psql"}
if secret_name not in allowed_secrets or not command or command[0] not in allowed_commands:
    raise SystemExit("unsupported Compose secret command")
if os.environ.get(secret_name) or not os.environ.get(f"{secret_name}_FILE"):
    raise SystemExit(f"Compose commands require {secret_name}_FILE only")
environment = os.environ.copy()
environment["PGPASSWORD"] = read_secret_from_environment(secret_name)
os.execvpe(command[0], command, environment)'

postgres_id=$(compose ps -q postgres)
if [ -z "$postgres_id" ]; then
    printf '%s\n' 'Compose PostgreSQL container is not running' >&2
    exit 1
fi

compose run --rm -T --no-deps --user 10001:10001 --entrypoint sh migrate -ceu '
for client in pg_dump pg_restore; do
    client_version=$($client --version)
    case "$client_version" in
        ("$client (PostgreSQL) 16."*) ;;
        (*)
            printf "%s\n" "backup/restore requires PostgreSQL 16 client tools" >&2
            exit 1
            ;;
    esac
done
' >/dev/null

probe_nonce="$(openssl rand -hex 16)"
source_db="fcr_backup_source_$probe_nonce"
restore_db="fcr_restore_$probe_nonce"
source_db_identity="fcr-backup-restore-smoke:$probe_nonce:source"
restore_db_identity="fcr-backup-restore-smoke:$probe_nonce:restore"
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/fcr-backup-restore.XXXXXX")"
dump_file="$work_dir/test-db.dump"
sentinel_id="prj_backup_$probe_nonce"
sentinel_code="BRS_$probe_nonce"
sentinel_file_id="file_backup_$probe_nonce"
sentinel_upload_id="upl_backup_$probe_nonce"
sentinel_storage_path="/data/storage/files/$sentinel_file_id"
sentinel_blob='fcr-backup-restore-smoke'
sentinel_blob_size=$(printf '%s' "$sentinel_blob" | wc -c | tr -d ' ')
sentinel_reserved_bytes=$((sentinel_blob_size * 2))
blob_created=0
blob_device=
blob_inode=
source_db_created=0
restore_db_created=0
backup_workdir_ready=0
backup_work_device=
backup_work_inode=
backup_work_ctime=
backup_dump_device=
backup_dump_inode=
backup_dump_ctime=
backup_dump_size=
if command -v sha256sum >/dev/null 2>&1; then
    sentinel_blob_sha=$(printf '%s' "$sentinel_blob" | sha256sum | awk '{print $1}')
else
    sentinel_blob_sha=$(printf '%s' "$sentinel_blob" | shasum -a 256 | awk '{print $1}')
fi

assign_backup_workdir_identity() {
    identity_value=$1
    previous_ifs=$IFS
    IFS=:
    set -- $identity_value
    IFS=$previous_ifs
    if [ "$#" -ne 7 ]; then
        printf '%s\n' 'backup work identity is malformed' >&2
        return 1
    fi
    for identity_field in "$@"; do
        case "$identity_field" in
            (''|*[!0-9]*)
                printf '%s\n' 'backup work identity is malformed' >&2
                return 1
                ;;
        esac
    done
    backup_work_device=$1
    backup_work_inode=$2
    backup_work_ctime=$3
    backup_dump_device=$4
    backup_dump_inode=$5
    backup_dump_ctime=$6
    backup_dump_size=$7
}

refresh_backup_workdir_identity() {
    refreshed_identity=$(PYTHONPATH="$backup_workdir_pythonpath" "$backup_workdir_python" \
        "$backup_workdir_helper" refresh "$work_dir" \
        "$backup_work_device" "$backup_work_inode" "$backup_work_ctime" \
        "$backup_dump_device" "$backup_dump_inode") || return $?
    assign_backup_workdir_identity "$refreshed_identity"
}

cleanup_backup_workdir() {
    refresh_backup_workdir_identity || return $?
    PYTHONPATH="$backup_workdir_pythonpath" "$backup_workdir_python" "$backup_workdir_helper" cleanup \
        "$work_dir" "$backup_work_device" "$backup_work_inode" "$backup_work_ctime" \
        "$backup_dump_device" "$backup_dump_inode" "$backup_dump_ctime" "$backup_dump_size"
    backup_workdir_ready=0
}

chmod 0700 "$work_dir"
initial_backup_identity=$(PYTHONPATH="$backup_workdir_pythonpath" "$backup_workdir_python" \
    "$backup_workdir_helper" create "$work_dir")
assign_backup_workdir_identity "$initial_backup_identity"
backup_workdir_ready=1

create_blob() {
    trap '' HUP INT TERM
    if blob_identity=$(compose run --rm -T --no-deps --user 10001:10001 --entrypoint sh \
    -e SENTINEL_FILE_ID="$sentinel_file_id" \
    backend -seu <<'EOSH'
case "$SENTINEL_FILE_ID" in (*[!A-Za-z0-9_]*) exit 2;; esac
python - <<'PY'
import os
import signal
from pathlib import Path

from backend.app.safe_files import ensure_private_directory, exclusive_file_writer

root = Path("/data/storage")
file_id = os.environ["SENTINEL_FILE_ID"]
def interrupt(signum, _frame):
    raise SystemExit(128 + signum)

for signal_name in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(signal_name, interrupt)

ensure_private_directory(root, "files")
with exclusive_file_writer(root / "files" / file_id, root) as handle:
    handle.write(b"fcr-backup-restore-smoke")
    metadata = os.fstat(handle.fileno())
print(f"{metadata.st_dev}:{metadata.st_ino}")
PY
EOSH
    ); then
        blob_created=1
    else
        create_status=$?
        trap 'exit 129' HUP
        trap 'exit 130' INT
        trap 'exit 143' TERM
        return "$create_status"
    fi
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    blob_device=${blob_identity%%:*}
    blob_inode=${blob_identity#*:}
    case "$blob_device" in (''|*[!0-9]*) return 2;; esac
    case "$blob_inode" in (''|*[!0-9]*) return 2;; esac
    test "$blob_identity" = "$blob_device:$blob_inode"
}

create_owned_database() {
    owned_db=$1
    expected_db_sentinel=$2
    compose run --rm -T --no-deps --user 10001:10001 --entrypoint sh \
        -e OWNED_DB="$owned_db" \
        -e EXPECTED_DB_SENTINEL="$expected_db_sentinel" \
        -e FCR_SECRET_EXEC_PY="$compose_secret_exec_python" \
        migrate -seu <<'EOSH' >/dev/null
secret_exec() {
    secret_name=$1
    shift
    python -c "$FCR_SECRET_EXEC_PY" "$secret_name" "$@"
}
case "$OWNED_DB" in (*[!A-Za-z0-9_]*) exit 2;; esac
case "$EXPECTED_DB_SENTINEL" in
    (fcr-backup-restore-smoke:????????????????????????????????:source)
        db_nonce=${EXPECTED_DB_SENTINEL#fcr-backup-restore-smoke:}
        db_nonce=${db_nonce%:source}
        expected_db_name="fcr_backup_source_$db_nonce"
        ;;
    (fcr-backup-restore-smoke:????????????????????????????????:restore)
        db_nonce=${EXPECTED_DB_SENTINEL#fcr-backup-restore-smoke:}
        db_nonce=${db_nonce%:restore}
        expected_db_name="fcr_restore_$db_nonce"
        ;;
    (*) exit 2;;
esac
case "$db_nonce" in (*[!0-9a-f]*) exit 2;; esac
test "$OWNED_DB" = "$expected_db_name"
case "$POSTGRES_OWNER_USER:$POSTGRES_APP_USER" in (*[!A-Za-z0-9_:]*) exit 2;; esac
if [ "$OWNED_DB" = "$POSTGRES_ADMIN_DB" ] || [ "$OWNED_DB" = "$POSTGRES_APP_DB" ] \
    || [ "$OWNED_DB" = "$POSTGRES_TEST_DB" ]; then
    printf '%s\n' 'refusing backup smoke against a configured database' >&2
    exit 2
fi
existing_identity=$(secret_exec POSTGRES_ADMIN_PASSWORD psql \
    -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
    -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_ADMIN_DB" -tAc \
    "SELECT pg_get_userbyid(datdba) || ':' || COALESCE(shobj_description(oid, 'pg_database'), '') FROM pg_database WHERE datname = '$OWNED_DB'")
test -z "$existing_identity"
create_status=0
secret_exec POSTGRES_ADMIN_PASSWORD createdb \
    -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_ADMIN_USER" \
    -O "$POSTGRES_OWNER_USER" "$OWNED_DB" "$EXPECTED_DB_SENTINEL" || create_status=$?
if [ "$create_status" -ne 0 ]; then
    exit "$create_status"
fi
EOSH
}

drop_owned_database() {
    owned_db=$1
    expected_db_sentinel=$2
    compose run --rm -T --no-deps --user 10001:10001 --entrypoint sh \
        -e OWNED_DB="$owned_db" \
        -e EXPECTED_DB_SENTINEL="$expected_db_sentinel" \
        -e FCR_SECRET_EXEC_PY="$compose_secret_exec_python" \
        migrate -seu <<'EOSH' >/dev/null 2>&1
secret_exec() {
    secret_name=$1
    shift
    python -c "$FCR_SECRET_EXEC_PY" "$secret_name" "$@"
}
case "$OWNED_DB" in (*[!A-Za-z0-9_]*) exit 2;; esac
case "$EXPECTED_DB_SENTINEL" in
    (fcr-backup-restore-smoke:????????????????????????????????:source)
        db_nonce=${EXPECTED_DB_SENTINEL#fcr-backup-restore-smoke:}
        db_nonce=${db_nonce%:source}
        expected_db_name="fcr_backup_source_$db_nonce"
        ;;
    (fcr-backup-restore-smoke:????????????????????????????????:restore)
        db_nonce=${EXPECTED_DB_SENTINEL#fcr-backup-restore-smoke:}
        db_nonce=${db_nonce%:restore}
        expected_db_name="fcr_restore_$db_nonce"
        ;;
    (*) exit 2;;
esac
case "$db_nonce" in (*[!0-9a-f]*) exit 2;; esac
case "$POSTGRES_OWNER_USER" in (''|*[!A-Za-z0-9_]*) exit 2;; esac
test "$OWNED_DB" = "$expected_db_name"
if ! database_identity=$(secret_exec POSTGRES_ADMIN_PASSWORD psql \
    -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
    -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_ADMIN_DB" -tAc \
    "SELECT pg_get_userbyid(datdba) || ':' || COALESCE(shobj_description(oid, 'pg_database'), '') FROM pg_database WHERE datname = '$OWNED_DB'"); then
    printf '%s\n' 'temporary database cleanup identity query failed' >&2
    exit 1
fi
if [ -z "$database_identity" ]; then
    exit 0
fi
case "$database_identity" in
    ("$POSTGRES_OWNER_USER:$EXPECTED_DB_SENTINEL") ;;
    (*)
        printf '%s\n' 'refusing to drop temporary database with mismatched identity' >&2
        exit 1
        ;;
esac
secret_exec POSTGRES_ADMIN_PASSWORD psql \
    -v ON_ERROR_STOP=1 -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
    -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_ADMIN_DB" \
    -c "REVOKE ALL PRIVILEGES ON DATABASE \"$OWNED_DB\" FROM PUBLIC" >/dev/null
secret_exec POSTGRES_ADMIN_PASSWORD dropdb --force \
    -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_ADMIN_USER" "$OWNED_DB"
EOSH
}

mark_owned_database() {
    owned_db=$1
    expected_db_sentinel=$2
    compose run --rm -T --no-deps --user 10001:10001 --entrypoint sh \
        -e OWNED_DB="$owned_db" \
        -e EXPECTED_DB_SENTINEL="$expected_db_sentinel" \
        -e FCR_SECRET_EXEC_PY="$compose_secret_exec_python" \
        migrate -seu <<'EOSH' >/dev/null
secret_exec() {
    secret_name=$1
    shift
    python -c "$FCR_SECRET_EXEC_PY" "$secret_name" "$@"
}
case "$OWNED_DB" in (*[!A-Za-z0-9_]*) exit 2;; esac
case "$EXPECTED_DB_SENTINEL" in (fcr-backup-restore-smoke:????????????????????????????????:source|fcr-backup-restore-smoke:????????????????????????????????:restore) ;; (*) exit 2;; esac
database_owner=$(secret_exec POSTGRES_ADMIN_PASSWORD psql \
    -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
    -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_ADMIN_DB" -tAc \
    "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '$OWNED_DB'")
database_sentinel=$(secret_exec POSTGRES_ADMIN_PASSWORD psql \
    -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
    -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_ADMIN_DB" -tAc \
    "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = '$OWNED_DB'")
test "$database_owner" = "$POSTGRES_OWNER_USER"
test "$database_sentinel" = "$EXPECTED_DB_SENTINEL"
secret_exec POSTGRES_ADMIN_PASSWORD psql \
    -v ON_ERROR_STOP=1 -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
    -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_ADMIN_DB" \
    -c "REVOKE ALL PRIVILEGES ON DATABASE \"$OWNED_DB\" FROM PUBLIC" >/dev/null
EOSH
}

cleanup() {
    cleanup_mode=${1:-best_effort}
    cleanup_failed=0
    if [ "$blob_created" -eq 1 ]; then
        if compose run --rm -T --no-deps --user 10001:10001 --entrypoint sh \
        -e SENTINEL_FILE_ID="$sentinel_file_id" \
        -e SENTINEL_BLOB_SIZE="$sentinel_blob_size" \
        -e SENTINEL_BLOB_SHA256="$sentinel_blob_sha" \
        -e SENTINEL_BLOB_DEVICE="$blob_device" \
        -e SENTINEL_BLOB_INODE="$blob_inode" \
        backend -seu <<'EOSH' >/dev/null 2>&1
case "$SENTINEL_FILE_ID" in (*[!A-Za-z0-9_]*) exit 2;; esac
python - <<'PY'
import hashlib
import os
from pathlib import Path
from backend.app.safe_files import pin_regular_file, unlink_regular_file_if_identity
from backend.scripts.verify_storage_blob import verify_storage_blob

root = Path("/data/storage")
file_id = os.environ["SENTINEL_FILE_ID"]
target = root / "files" / file_id
expected_size = int(os.environ["SENTINEL_BLOB_SIZE"])
expected_sha256 = os.environ["SENTINEL_BLOB_SHA256"]
device = os.environ.get("SENTINEL_BLOB_DEVICE", "")
inode = os.environ.get("SENTINEL_BLOB_INODE", "")
if device and inode:
    verify_storage_blob(root, file_id, expected_size, expected_sha256)
    unlink_regular_file_if_identity(target, root, device=int(device), inode=int(inode))
else:
    with pin_regular_file(target, root) as pinned:
        if pinned is not None and pinned.exists:
            if pinned.size != expected_size:
                raise RuntimeError("temporary storage blob size mismatch")
            digest = hashlib.sha256()
            with pinned.open_readonly() as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                raise RuntimeError("temporary storage blob digest mismatch")
            pinned.unlink()
PY
EOSH
        then
            blob_created=0
        else
            cleanup_failed=1
        fi
    fi
    if [ "$restore_db_created" -eq 1 ]; then
        if drop_owned_database "$restore_db" "$restore_db_identity"; then
            restore_db_created=0
        else
            cleanup_failed=1
        fi
    fi
    if [ "$source_db_created" -eq 1 ]; then
        if drop_owned_database "$source_db" "$source_db_identity"; then
            source_db_created=0
        else
            cleanup_failed=1
        fi
    fi
    if [ "$backup_workdir_ready" -eq 1 ]; then
        if cleanup_backup_workdir; then
            :
        else
            cleanup_failed=1
        fi
    fi
    if [ "$cleanup_failed" -ne 0 ]; then
        printf '%s\n' 'backup/restore smoke could not clean every owned temporary resource' >&2
        if [ "$cleanup_mode" = strict ]; then
            return 1
        fi
    fi
}
trap 'cleanup best_effort' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

create_blob

trap '' HUP INT TERM
source_db_created=1
if create_owned_database "$source_db" "$source_db_identity"; then
    :
else
    create_status=$?
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    exit "$create_status"
fi
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
mark_owned_database "$source_db" "$source_db_identity"

compose run --rm -T --no-deps \
    -e DATABASE_NAME="$source_db" \
    migrate alembic -c backend/alembic.ini upgrade head >/dev/null
compose run --rm -T --no-deps \
    -e DATABASE_NAME="$source_db" \
    migrate python backend/scripts/grant_runtime_role.py >/dev/null

compose run --rm -T --no-deps --user 10001:10001 --entrypoint sh \
    -e SOURCE_DB="$source_db" \
    -e SENTINEL_ID="$sentinel_id" \
    -e SENTINEL_CODE="$sentinel_code" \
    -e SENTINEL_FILE_ID="$sentinel_file_id" \
    -e SENTINEL_UPLOAD_ID="$sentinel_upload_id" \
    -e SENTINEL_STORAGE_PATH="$sentinel_storage_path" \
    -e SENTINEL_BLOB_SIZE="$sentinel_blob_size" \
    -e SENTINEL_RESERVED_BYTES="$sentinel_reserved_bytes" \
    -e SENTINEL_BLOB_SHA="$sentinel_blob_sha" \
    -e FCR_SECRET_EXEC_PY="$compose_secret_exec_python" \
    migrate -seu <<'EOSH'
secret_exec() {
    secret_name=$1
    shift
    python -c "$FCR_SECRET_EXEC_PY" "$secret_name" "$@"
}
case "$SOURCE_DB:$SENTINEL_ID:$SENTINEL_CODE:$SENTINEL_FILE_ID:$SENTINEL_UPLOAD_ID" in
    (*[!A-Za-z0-9_:]*) exit 2;;
esac
case "$SENTINEL_STORAGE_PATH" in (/data/storage/files/file_backup_*) ;; (*) exit 2;; esac
case "$SENTINEL_BLOB_SIZE" in (''|*[!0-9]*) exit 2;; esac
case "$SENTINEL_RESERVED_BYTES" in (''|*[!0-9]*) exit 2;; esac
case "$SENTINEL_BLOB_SHA" in (????????????????????????????????????????????????????????????????) ;; (*) exit 2;; esac
secret_exec POSTGRES_APP_PASSWORD psql \
    -v ON_ERROR_STOP=1 -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
    -U "$POSTGRES_APP_USER" -d "$SOURCE_DB" >/dev/null <<EOSQL
INSERT INTO project_refs (
    id, project_code, project_name, source, description, lifecycle_status, lock_version, created_at, updated_at
) VALUES (
    '$SENTINEL_ID', '$SENTINEL_CODE', 'Backup restore smoke sentinel', 'local',
    'Backup restore smoke sentinel description', 'active', 1, now(), now()
);
INSERT INTO file_objects (
    id, original_filename, mime_type, file_size, sha256, storage_path,
    owner_principal_id, owner_principal_kind, duration_ms, width, height,
    fps_num, fps_den, media_probe_version, created_at
) VALUES (
    '$SENTINEL_FILE_ID', 'backup-restore-smoke.mp4', 'video/mp4',
    $SENTINEL_BLOB_SIZE, '$SENTINEL_BLOB_SHA', '$SENTINEL_STORAGE_PATH',
    'backup-restore-smoke', 'system', 1000, 16, 16, 25, 1, 'backup-smoke', now()
);
INSERT INTO upload_sessions (
    id, original_filename, mime_type, declared_size, expected_sha256, status,
    temp_path, owner_principal_id, owner_principal_kind, received_parts,
    reserved_bytes, parts_cleanup_confirmed_at, file_id,
    duration_ms, width, height, fps_num, fps_den, created_at, updated_at
) VALUES (
    '$SENTINEL_UPLOAD_ID', 'backup-restore-smoke.mp4', 'video/mp4',
    $SENTINEL_BLOB_SIZE, '$SENTINEL_BLOB_SHA', 'completed',
    '/data/storage/uploads/$SENTINEL_UPLOAD_ID', 'backup-restore-smoke', 'system',
    '{}', $SENTINEL_RESERVED_BYTES, now(), '$SENTINEL_FILE_ID',
    1000, 16, 16, 25, 1, now(), now()
);
EOSQL
EOSH

dump_status=0
if FCR_COMPOSE_ENV_FILE="$compose_env_file" FCR_COMPOSE_PROJECT="$compose_project" \
    PYTHONPATH="$backup_workdir_pythonpath" \
    "$backup_workdir_python" "$backup_workdir_helper" write "$work_dir" \
    "$backup_work_device" "$backup_work_inode" "$backup_work_ctime" \
    "$backup_dump_device" "$backup_dump_inode" -- \
    sh "$script_dir/compose-delivery.sh" run --rm -T --no-deps --user 10001:10001 --entrypoint sh \
    -e DATABASE_NAME="$source_db" \
    -e FCR_SECRET_EXEC_PY="$compose_secret_exec_python" \
    migrate -ceu 'exec python -c "$FCR_SECRET_EXEC_PY" POSTGRES_OWNER_PASSWORD pg_dump -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_OWNER_USER" -d "$DATABASE_NAME" -Fc'
then
    :
else
    dump_status=$?
fi
refresh_backup_workdir_identity
if [ "$dump_status" -ne 0 ]; then
    exit "$dump_status"
fi
if [ "$backup_dump_size" -le 0 ]; then
    printf '%s\n' 'logical backup dump is empty' >&2
    exit 1
fi

trap '' HUP INT TERM
restore_db_created=1
if create_owned_database "$restore_db" "$restore_db_identity"; then
    :
else
    create_status=$?
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    exit "$create_status"
fi
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
mark_owned_database "$restore_db" "$restore_db_identity"

refresh_backup_workdir_identity
FCR_COMPOSE_ENV_FILE="$compose_env_file" FCR_COMPOSE_PROJECT="$compose_project" \
    PYTHONPATH="$backup_workdir_pythonpath" \
    "$backup_workdir_python" "$backup_workdir_helper" read "$work_dir" \
    "$backup_work_device" "$backup_work_inode" "$backup_work_ctime" \
    "$backup_dump_device" "$backup_dump_inode" "$backup_dump_ctime" "$backup_dump_size" -- \
    sh "$script_dir/compose-delivery.sh" run --rm -T --no-deps --user 10001:10001 --entrypoint sh \
    -e DATABASE_NAME="$restore_db" \
    -e FCR_SECRET_EXEC_PY="$compose_secret_exec_python" \
    migrate -ceu 'exec python -c "$FCR_SECRET_EXEC_PY" POSTGRES_OWNER_PASSWORD pg_restore -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_OWNER_USER" -d "$DATABASE_NAME" --no-owner --no-privileges'
compose run --rm -T --no-deps \
    -e DATABASE_NAME="$restore_db" \
    migrate python backend/scripts/grant_runtime_role.py >/dev/null

compose run --rm -T --no-deps --user 10001:10001 --entrypoint sh \
    -e SOURCE_DB="$source_db" \
    -e RESTORE_DB="$restore_db" \
    -e SENTINEL_ID="$sentinel_id" \
    -e SENTINEL_FILE_ID="$sentinel_file_id" \
    -e SENTINEL_UPLOAD_ID="$sentinel_upload_id" \
    -e FCR_SECRET_EXEC_PY="$compose_secret_exec_python" \
    migrate -seu <<'EOSH'
secret_exec() {
    secret_name=$1
    shift
    python -c "$FCR_SECRET_EXEC_PY" "$secret_name" "$@"
}
verify_or_fail() {
    check_name=$1
    shift
    if ! "$@"; then
        printf "%s\n" "backup/restore verification failed: $check_name" >&2
        exit 1
    fi
}
case "$SOURCE_DB:$RESTORE_DB:$SENTINEL_ID:$SENTINEL_FILE_ID:$SENTINEL_UPLOAD_ID" in
    (*[!A-Za-z0-9_:]*) exit 2;;
esac
source_revision=$(secret_exec POSTGRES_OWNER_PASSWORD psql -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_OWNER_USER" -d "$SOURCE_DB" -tAc 'SELECT version_num FROM alembic_version')
restored_revision=$(secret_exec POSTGRES_OWNER_PASSWORD psql -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_OWNER_USER" -d "$RESTORE_DB" -tAc 'SELECT version_num FROM alembic_version')
source_tables=$(secret_exec POSTGRES_OWNER_PASSWORD psql -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_OWNER_USER" -d "$SOURCE_DB" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
restored_tables=$(secret_exec POSTGRES_OWNER_PASSWORD psql -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_OWNER_USER" -d "$RESTORE_DB" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
verify_or_fail source_revision_present test -n "$source_revision"
verify_or_fail migration_revision_match test "$source_revision" = "$restored_revision"
verify_or_fail source_tables_present test "$source_tables" -gt 0
verify_or_fail public_table_count_match test "$source_tables" = "$restored_tables"
database_owner=$(secret_exec POSTGRES_OWNER_PASSWORD psql -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_OWNER_USER" -d "$RESTORE_DB" -tAc "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '$RESTORE_DB'")
schema_owner_valid=$(secret_exec POSTGRES_OWNER_PASSWORD psql -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_OWNER_USER" -d "$RESTORE_DB" -tAc "SELECT CASE WHEN pg_get_userbyid(n.nspowner) = '$POSTGRES_OWNER_USER' OR pg_get_userbyid(n.nspowner) = 'pg_database_owner' THEN 1 ELSE 0 END FROM pg_namespace n WHERE n.nspname = 'public'")
foreign_owned=$(secret_exec POSTGRES_OWNER_PASSWORD psql -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_OWNER_USER" -d "$RESTORE_DB" -tAc "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r','p','S','v','m','f') AND pg_get_userbyid(relowner) <> '$POSTGRES_OWNER_USER'")
verify_or_fail restored_database_owner test "$database_owner" = "$POSTGRES_OWNER_USER"
verify_or_fail restored_schema_owner test "$schema_owner_valid" -eq 1
verify_or_fail restored_relation_ownership test "$foreign_owned" -eq 0
source_sentinel=$(secret_exec POSTGRES_APP_PASSWORD psql -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_APP_USER" -d "$SOURCE_DB" -tAc "SELECT id || ':' || project_code || ':' || project_name FROM project_refs WHERE id = '$SENTINEL_ID'")
restored_sentinel=$(secret_exec POSTGRES_APP_PASSWORD psql -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_APP_USER" -d "$RESTORE_DB" -tAc "SELECT id || ':' || project_code || ':' || project_name FROM project_refs WHERE id = '$SENTINEL_ID'")
verify_or_fail source_project_sentinel_present test -n "$source_sentinel"
verify_or_fail project_sentinel_match test "$source_sentinel" = "$restored_sentinel"
source_binding=$(secret_exec POSTGRES_APP_PASSWORD psql -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_APP_USER" -d "$SOURCE_DB" -tAc "SELECT f.id || ':' || f.storage_path || ':' || f.sha256 || ':' || u.id || ':' || u.file_id FROM file_objects f JOIN upload_sessions u ON u.file_id = f.id WHERE f.id = '$SENTINEL_FILE_ID' AND u.id = '$SENTINEL_UPLOAD_ID'")
restored_binding=$(secret_exec POSTGRES_APP_PASSWORD psql -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_APP_USER" -d "$RESTORE_DB" -tAc "SELECT f.id || ':' || f.storage_path || ':' || f.sha256 || ':' || u.id || ':' || u.file_id FROM file_objects f JOIN upload_sessions u ON u.file_id = f.id WHERE f.id = '$SENTINEL_FILE_ID' AND u.id = '$SENTINEL_UPLOAD_ID'")
verify_or_fail source_file_binding_present test -n "$source_binding"
verify_or_fail file_binding_match test "$source_binding" = "$restored_binding"
secret_exec POSTGRES_APP_PASSWORD psql \
    -v ON_ERROR_STOP=1 -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
    -U "$POSTGRES_APP_USER" -d "$RESTORE_DB" >/dev/null <<EOSQL
BEGIN;
INSERT INTO project_refs (
    id, project_code, project_name, source, description, lifecycle_status, lock_version, created_at, updated_at
) VALUES (
    '${SENTINEL_ID}_probe', 'RUNTIME_PROBE', 'Runtime DML probe', 'local',
    'Runtime DML probe description', 'active', 1, now(), now()
);
SELECT id FROM project_refs WHERE id = '${SENTINEL_ID}_probe';
UPDATE project_refs SET project_name = 'Runtime DML updated' WHERE id = '${SENTINEL_ID}_probe';
DELETE FROM project_refs WHERE id = '${SENTINEL_ID}_probe';
ROLLBACK;
EOSQL
runtime_privileges=$(secret_exec POSTGRES_APP_PASSWORD psql -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" -U "$POSTGRES_APP_USER" -d "$RESTORE_DB" -tAc "SELECT has_database_privilege(current_user, current_database(), 'CONNECT')::int || ':' || has_database_privilege(current_user, current_database(), 'CREATE')::int || ':' || has_database_privilege(current_user, current_database(), 'TEMP')::int || ':' || has_schema_privilege(current_user, 'public', 'USAGE')::int || ':' || has_schema_privilege(current_user, 'public', 'CREATE')::int")
verify_or_fail runtime_least_privilege test "$runtime_privileges" = '1:0:0:1:0'
EOSH

compose run --rm -T --no-deps \
    -e DATABASE_NAME="$restore_db" \
    backend python -c 'from backend.app.main import database_readiness; result = database_readiness(); assert result["database"] == "ok"; assert result["storage"] == "ok"; assert int(result["file_associations"]) >= 1' >/dev/null
compose run --rm -T --no-deps \
    -e VERIFY_FILE_ID="$sentinel_file_id" \
    -e VERIFY_BLOB_SIZE="$sentinel_blob_size" \
    -e VERIFY_BLOB_SHA256="$sentinel_blob_sha" \
    backend python backend/scripts/verify_storage_blob.py >/dev/null

cleanup strict
trap - EXIT HUP INT TERM
printf '%s\n' 'owner-managed PostgreSQL logical backup/restore and existing-volume blob integrity smoke passed'
