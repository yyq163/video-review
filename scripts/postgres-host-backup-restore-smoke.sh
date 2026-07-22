#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd -P)
cd "$project_root"
backup_workdir_python=${FCR_BACKUP_WORKDIR_PYTHON:-backend/.venv/bin/python}
backup_workdir_helper=${FCR_BACKUP_WORKDIR_HELPER:-backend/scripts/backup_workdir.py}
backup_workdir_pythonpath=${FCR_BACKUP_WORKDIR_PYTHONPATH:-.}

require_var() {
    name=$1
    eval "value=\${$name:-}"
    if [ -z "$value" ]; then
        printf '%s\n' "$name is required" >&2
        exit 2
    fi
}

require_identifier() {
    name=$1
    value=$2
    case "$value" in
        (''|*[!A-Za-z0-9_]*)
            printf '%s\n' "$name must be a PostgreSQL identifier" >&2
            exit 2
            ;;
    esac
}

for command_name in createdb dropdb openssl pg_dump pg_restore psql; do
    command -v "$command_name" >/dev/null 2>&1 || {
        printf '%s\n' "$command_name is required" >&2
        exit 2
    }
done

for required_name in \
    POSTGRES_ADMIN_HOST POSTGRES_ADMIN_PORT POSTGRES_ADMIN_USER \
    POSTGRES_ADMIN_PASSWORD POSTGRES_ADMIN_DB POSTGRES_OWNER_USER \
    POSTGRES_OWNER_PASSWORD POSTGRES_APP_USER POSTGRES_APP_PASSWORD \
    POSTGRES_APP_DB POSTGRES_TEST_DB STORAGE_ROOT PACKAGE_ROOT \
    WRITE_GUARD_SESSION_SECRET
do
    require_var "$required_name"
done

require_identifier POSTGRES_ADMIN_USER "$POSTGRES_ADMIN_USER"
require_identifier POSTGRES_ADMIN_DB "$POSTGRES_ADMIN_DB"
require_identifier POSTGRES_OWNER_USER "$POSTGRES_OWNER_USER"
require_identifier POSTGRES_APP_USER "$POSTGRES_APP_USER"
require_identifier POSTGRES_APP_DB "$POSTGRES_APP_DB"
require_identifier POSTGRES_TEST_DB "$POSTGRES_TEST_DB"
case "$POSTGRES_ADMIN_PORT" in
    (''|*[!0-9]*)
        printf '%s\n' 'POSTGRES_ADMIN_PORT must be numeric' >&2
        exit 2
        ;;
esac

nonce=$(openssl rand -hex 16)
source_db="fcr_host_backup_$nonce"
restore_db="fcr_host_restore_$nonce"
source_identity="fcr-host-backup-restore:$nonce:source"
restore_identity="fcr-host-backup-restore:$nonce:restore"
sentinel_id="prj_host_backup_$nonce"
file_id="file_host_backup_$nonce"
upload_id="upl_host_backup_$nonce"
blob='fcr-host-backup-restore-smoke'
blob_size=$(printf '%s' "$blob" | wc -c | tr -d ' ')
blob_sha=$(printf '%s' "$blob" | shasum -a 256 | awk '{print $1}')
storage_root=$(PYTHONPATH=. backend/.venv/bin/python -c \
    'from backend.app.settings import get_settings; print(get_settings().storage_root)')
blob_path="$storage_root/files/$file_id"
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/fcr-host-backup-restore.XXXXXX")
dump_file="$work_dir/test-db.dump"
source_created=0
restore_created=0
blob_created=0
blob_device=
blob_inode=
cleanup_failed=0
backup_workdir_ready=0
backup_work_device=
backup_work_inode=
backup_work_ctime=
backup_dump_device=
backup_dump_inode=
backup_dump_ctime=
backup_dump_size=

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

admin_psql() {
    PGPASSWORD="$POSTGRES_ADMIN_PASSWORD" psql -v ON_ERROR_STOP=1 \
        -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
        -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_ADMIN_DB" "$@"
}

database_identity() {
    database_name=$1
    admin_psql -tAc \
        "SELECT pg_get_userbyid(datdba) || ':' || COALESCE(shobj_description(oid, 'pg_database'), '') FROM pg_database WHERE datname = '$database_name'"
}

validate_owned_database_identity() {
    database_name=$1
    expected_identity=$2
    case "$expected_identity" in
        (fcr-host-backup-restore:????????????????????????????????:source)
            identity_nonce=${expected_identity#fcr-host-backup-restore:}
            identity_nonce=${identity_nonce%:source}
            expected_database_name="fcr_host_backup_$identity_nonce"
            ;;
        (fcr-host-backup-restore:????????????????????????????????:restore)
            identity_nonce=${expected_identity#fcr-host-backup-restore:}
            identity_nonce=${identity_nonce%:restore}
            expected_database_name="fcr_host_restore_$identity_nonce"
            ;;
        (*)
            printf '%s\n' 'temporary database identity is invalid' >&2
            return 2
            ;;
    esac
    case "$identity_nonce" in
        (*[!0-9a-f]*)
            printf '%s\n' 'temporary database nonce is invalid' >&2
            return 2
            ;;
    esac
    if [ "$database_name" != "$expected_database_name" ]; then
        printf '%s\n' 'temporary database name does not match its identity' >&2
        return 2
    fi
}

drop_owned_database() {
    database_name=$1
    expected_identity=$2
    validate_owned_database_identity "$database_name" "$expected_identity" || return $?
    if ! actual_identity=$(database_identity "$database_name"); then
        printf '%s\n' "could not verify temporary database identity during cleanup: $database_name" >&2
        return 1
    fi
    if [ -z "$actual_identity" ]; then
        return 0
    fi
    case "$actual_identity" in
        ("$POSTGRES_OWNER_USER:$expected_identity") ;;
        (*)
        printf '%s\n' "refusing to drop temporary database with mismatched identity: $database_name" >&2
        return 1
        ;;
    esac
    PGPASSWORD="$POSTGRES_ADMIN_PASSWORD" dropdb --force \
        -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
        -U "$POSTGRES_ADMIN_USER" "$database_name"
}

cleanup() {
    if [ "$blob_created" -eq 1 ]; then
        if STORAGE_ROOT="$storage_root" VERIFY_FILE_ID="$file_id" \
            VERIFY_BLOB_SIZE="$blob_size" VERIFY_BLOB_SHA256="$blob_sha" \
            VERIFY_BLOB_DEVICE="$blob_device" VERIFY_BLOB_INODE="$blob_inode" \
            PYTHONPATH=. backend/.venv/bin/python - <<'PY' >/dev/null 2>&1
import os
from pathlib import Path

import hashlib

from backend.app.safe_files import pin_regular_file, unlink_regular_file_if_identity
from backend.scripts.verify_storage_blob import verify_storage_blob

root = Path(os.environ["STORAGE_ROOT"])
file_id = os.environ["VERIFY_FILE_ID"]
target = root / "files" / file_id
expected_size = int(os.environ["VERIFY_BLOB_SIZE"])
expected_sha256 = os.environ["VERIFY_BLOB_SHA256"]
device = os.environ.get("VERIFY_BLOB_DEVICE", "")
inode = os.environ.get("VERIFY_BLOB_INODE", "")
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
        then
            blob_created=0
        else
            cleanup_failed=1
        fi
    fi
    if [ "$restore_created" -eq 1 ]; then
        if drop_owned_database "$restore_db" "$restore_identity"; then
            restore_created=0
        else
            cleanup_failed=1
        fi
    fi
    if [ "$source_created" -eq 1 ]; then
        if drop_owned_database "$source_db" "$source_identity"; then
            source_created=0
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
        printf '%s\n' 'host backup/restore smoke could not clean every owned temporary resource' >&2
        return 1
    fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

create_owned_database() {
    database_name=$1
    expected_identity=$2
    validate_owned_database_identity "$database_name" "$expected_identity" || return $?
    case "$database_name" in
        ("$POSTGRES_ADMIN_DB"|"$POSTGRES_APP_DB"|"$POSTGRES_TEST_DB")
            printf '%s\n' 'refusing backup smoke against a configured database' >&2
            exit 2
            ;;
    esac
    if ! existing_identity=$(database_identity "$database_name"); then
        printf '%s\n' 'could not verify temporary database absence before createdb' >&2
        return 1
    fi
    if [ -n "$existing_identity" ]; then
        printf '%s\n' 'refusing to create an already existing temporary database' >&2
        return 1
    fi
    create_status=0
    PGPASSWORD="$POSTGRES_ADMIN_PASSWORD" createdb \
        -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
        -U "$POSTGRES_ADMIN_USER" -O "$POSTGRES_OWNER_USER" \
        "$database_name" "$expected_identity" || create_status=$?
    if [ "$create_status" -eq 0 ]; then
        return 0
    fi
    return "$create_status"
}

secure_owned_database() {
    database_name=$1
    expected_identity=$2
    test "$(database_identity "$database_name")" = "$POSTGRES_OWNER_USER:$expected_identity"
    admin_psql -c "REVOKE ALL PRIVILEGES ON DATABASE \"$database_name\" FROM PUBLIC" >/dev/null
}

run_owner() (
    database_name=$1
    shift
    unset DATABASE_URL POSTGRES_OWNER_DATABASE_URL POSTGRES_ADMIN_DATABASE_URL
    export DATABASE_HOST="$POSTGRES_ADMIN_HOST"
    export DATABASE_PORT="$POSTGRES_ADMIN_PORT"
    export DATABASE_NAME="$database_name"
    export DATABASE_USER="$POSTGRES_OWNER_USER"
    export DATABASE_PASSWORD="$POSTGRES_OWNER_PASSWORD"
    export POSTGRES_ADMIN_HOST POSTGRES_ADMIN_PORT POSTGRES_ADMIN_DB POSTGRES_ADMIN_USER
    export POSTGRES_ADMIN_PASSWORD POSTGRES_OWNER_USER POSTGRES_OWNER_PASSWORD
    export POSTGRES_APP_USER POSTGRES_APP_PASSWORD
    exec "$@"
)

trap '' HUP INT TERM
source_created=1
if create_owned_database "$source_db" "$source_identity"; then
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
secure_owned_database "$source_db" "$source_identity"
run_owner "$source_db" backend/.venv/bin/alembic -c backend/alembic.ini upgrade head >/dev/null
printf '%s\n' 'host backup smoke source migration passed'
run_owner "$source_db" backend/.venv/bin/python backend/scripts/grant_runtime_role.py >/dev/null
printf '%s\n' 'host backup smoke source runtime grants passed'

umask 077
trap '' HUP INT TERM
if blob_identity=$(STORAGE_ROOT="$storage_root" VERIFY_FILE_ID="$file_id" \
    PYTHONPATH=. backend/.venv/bin/python - <<'PY'
import os
import signal
from pathlib import Path

from backend.app.safe_files import ensure_private_directory, exclusive_file_writer

root = Path(os.environ["STORAGE_ROOT"])
file_id = os.environ["VERIFY_FILE_ID"]
def interrupt(signum, _frame):
    raise SystemExit(128 + signum)

for signal_name in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(signal_name, interrupt)

ensure_private_directory(root, "files")
try:
    with exclusive_file_writer(root / "files" / file_id, root) as handle:
        handle.write(b"fcr-host-backup-restore-smoke")
        metadata = os.fstat(handle.fileno())
except FileExistsError as exc:
    raise SystemExit("temporary storage blob already exists") from exc
print(f"{metadata.st_dev}:{metadata.st_ino}")
PY
); then
    blob_created=1
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
blob_device=${blob_identity%%:*}
blob_inode=${blob_identity#*:}
case "$blob_device" in
    (''|*[!0-9]*)
        printf '%s\n' 'safe storage writer returned an invalid device identity' >&2
        exit 2
        ;;
esac
case "$blob_inode" in
    (''|*[!0-9]*)
        printf '%s\n' 'safe storage writer returned an invalid inode identity' >&2
        exit 2
        ;;
esac
if [ "$blob_identity" != "$blob_device:$blob_inode" ]; then
    printf '%s\n' 'safe storage writer returned an invalid file identity' >&2
    exit 2
fi

PGPASSWORD="$POSTGRES_APP_PASSWORD" psql -v ON_ERROR_STOP=1 \
    -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
    -U "$POSTGRES_APP_USER" -d "$source_db" \
    -v blob_path="$blob_path" -v upload_temp_path="$storage_root/uploads/$upload_id" \
    >/dev/null <<EOSQL
INSERT INTO project_refs (
    id, project_code, project_name, source, description, lifecycle_status, lock_version, created_at, updated_at
) VALUES (
    '$sentinel_id', 'HOST_$nonce', 'Host backup restore sentinel', 'local',
    'Host backup restore sentinel description', 'active', 1, now(), now()
);
INSERT INTO file_objects (
    id, original_filename, mime_type, file_size, sha256, storage_path,
    owner_principal_id, owner_principal_kind, duration_ms, width, height,
    fps_num, fps_den, media_probe_version, created_at
) VALUES (
    '$file_id', 'host-backup-restore.mp4', 'video/mp4', $blob_size, '$blob_sha', :'blob_path',
    'host-backup-restore', 'system', 1000, 16, 16, 25, 1, 'host-backup-smoke', now()
);
INSERT INTO upload_sessions (
    id, original_filename, mime_type, declared_size, expected_sha256, status,
    temp_path, owner_principal_id, owner_principal_kind, received_parts,
    reserved_bytes, parts_cleanup_confirmed_at, file_id,
    duration_ms, width, height, fps_num, fps_den, created_at, updated_at
) VALUES (
    '$upload_id', 'host-backup-restore.mp4', 'video/mp4', $blob_size, '$blob_sha', 'completed',
    :'upload_temp_path', 'host-backup-restore', 'system', '{}',
    $((blob_size * 2)), now(), '$file_id', 1000, 16, 16, 25, 1, now(), now()
);
EOSQL
printf '%s\n' 'host backup smoke source sentinel passed'

dump_status=0
if PGPASSWORD="$POSTGRES_OWNER_PASSWORD" PYTHONPATH="$backup_workdir_pythonpath" \
    "$backup_workdir_python" "$backup_workdir_helper" write "$work_dir" \
    "$backup_work_device" "$backup_work_inode" "$backup_work_ctime" \
    "$backup_dump_device" "$backup_dump_inode" -- \
    pg_dump -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
    -U "$POSTGRES_OWNER_USER" -d "$source_db" -Fc
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
printf '%s\n' 'host backup smoke logical dump passed'

trap '' HUP INT TERM
restore_created=1
if create_owned_database "$restore_db" "$restore_identity"; then
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
secure_owned_database "$restore_db" "$restore_identity"
refresh_backup_workdir_identity
PGPASSWORD="$POSTGRES_OWNER_PASSWORD" PYTHONPATH="$backup_workdir_pythonpath" \
    "$backup_workdir_python" "$backup_workdir_helper" read "$work_dir" \
    "$backup_work_device" "$backup_work_inode" "$backup_work_ctime" \
    "$backup_dump_device" "$backup_dump_inode" "$backup_dump_ctime" "$backup_dump_size" -- \
    pg_restore \
    -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
    -U "$POSTGRES_OWNER_USER" -d "$restore_db" --no-owner --no-privileges
run_owner "$restore_db" backend/.venv/bin/python backend/scripts/grant_runtime_role.py >/dev/null
printf '%s\n' 'host backup smoke logical restore and grants passed'

restore_check=$(PGPASSWORD="$POSTGRES_OWNER_PASSWORD" psql -X -A -t -v ON_ERROR_STOP=1 \
    -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
    -U "$POSTGRES_OWNER_USER" -d "$restore_db" -v blob_path="$blob_path" \
    <<EOSQL
SELECT count(*) FROM alembic_version;
SELECT EXISTS (
    SELECT 1 FROM project_refs p
    JOIN file_objects f ON f.id = '$file_id'
    JOIN upload_sessions u ON u.file_id = f.id
    WHERE p.id = '$sentinel_id' AND u.id = '$upload_id'
      AND f.storage_path = :'blob_path' AND f.sha256 = '$blob_sha'
);
EOSQL
)
test "$restore_check" = "$(printf '1\nt')"
printf '%s\n' 'host backup smoke restored association passed'

runtime_privileges=$(PGPASSWORD="$POSTGRES_APP_PASSWORD" psql \
    -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
    -U "$POSTGRES_APP_USER" -d "$restore_db" -tAc \
    "SELECT has_database_privilege(current_user, current_database(), 'CONNECT')::int || ':' || has_database_privilege(current_user, current_database(), 'CREATE')::int || ':' || has_database_privilege(current_user, current_database(), 'TEMP')::int || ':' || has_schema_privilege(current_user, 'public', 'USAGE')::int || ':' || has_schema_privilege(current_user, 'public', 'CREATE')::int")
test "$runtime_privileges" = '1:0:0:1:0'
printf '%s\n' 'host backup smoke least privilege passed'

PGPASSWORD="$POSTGRES_APP_PASSWORD" psql -v ON_ERROR_STOP=1 \
    -h "$POSTGRES_ADMIN_HOST" -p "$POSTGRES_ADMIN_PORT" \
    -U "$POSTGRES_APP_USER" -d "$restore_db" >/dev/null <<EOSQL
BEGIN;
INSERT INTO project_refs (
    id, project_code, project_name, source, description, lifecycle_status, lock_version, created_at, updated_at
) VALUES (
    '${sentinel_id}_probe', 'HOST_PROBE_$nonce', 'Runtime DML probe', 'local',
    'Runtime DML probe description', 'active', 1, now(), now()
);
UPDATE project_refs SET project_name = 'Runtime DML updated' WHERE id = '${sentinel_id}_probe';
DELETE FROM project_refs WHERE id = '${sentinel_id}_probe';
ROLLBACK;
EOSQL
printf '%s\n' 'host backup smoke runtime DML passed'

STORAGE_ROOT="$storage_root" VERIFY_FILE_ID="$file_id" \
    VERIFY_BLOB_SIZE="$blob_size" VERIFY_BLOB_SHA256="$blob_sha" \
    PYTHONPATH=. backend/.venv/bin/python backend/scripts/verify_storage_blob.py >/dev/null
printf '%s\n' 'host backup smoke storage digest passed'

(
    unset DATABASE_URL POSTGRES_OWNER_DATABASE_URL POSTGRES_ADMIN_DATABASE_URL
    export DATABASE_HOST="$POSTGRES_ADMIN_HOST"
    export DATABASE_PORT="$POSTGRES_ADMIN_PORT"
    export DATABASE_NAME="$restore_db"
    export DATABASE_USER="$POSTGRES_APP_USER"
    export DATABASE_PASSWORD="$POSTGRES_APP_PASSWORD"
    export STORAGE_ROOT="$storage_root"
    export PACKAGE_ROOT WRITE_GUARD_SESSION_SECRET
    exec backend/.venv/bin/python -c \
        'from backend.app.main import database_readiness; result = database_readiness(); assert result["database_engine"] == "postgresql"; assert int(result["file_associations"]) >= 1'
) >/dev/null

cleanup
trap - EXIT HUP INT TERM
printf '%s\n' 'host PostgreSQL logical backup/restore, least privilege, and file association smoke passed'
