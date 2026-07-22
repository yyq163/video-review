#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd -P)
compose_env_input=${FCR_COMPOSE_ENV_FILE:-.env}
case "$compose_env_input" in
    (/*) compose_env_source=$compose_env_input ;;
    (*) compose_env_source="$project_root/$compose_env_input" ;;
esac
compose_project_directory=$project_root
compose_project=${FCR_COMPOSE_PROJECT:-fj-final-cut-review}
compose_base_file="$project_root/docker-compose.yml"
compose_delivery_file="$project_root/docker-compose.delivery.yml"
docker_daemon_timeout_seconds=${FCR_DOCKER_DAEMON_TIMEOUT_SECONDS:-30}
docker_command_timeout_seconds=${FCR_DOCKER_COMMAND_TIMEOUT_SECONDS:-1800}
docker_timeout_exit_code=75

reject_compose_control_overrides() {
    for argument in "$@"; do
        case "$argument" in
            (-f|--file|--env-file|--project-directory|-p|--project-name|-f?*|-p?*|--file=*|--env-file=*|--project-directory=*|--project-name=*)
                printf '%s\n' 'delivery Compose files, environment, project directory, and project name are fixed' >&2
                return 2
                ;;
        esac
    done
}

require_positive_timeout() {
    variable_name=$1
    timeout_value=$2
    case "$timeout_value" in
        (''|*[!0-9]*|0)
            printf '%s\n' "$variable_name must be a positive integer" >&2
            exit 2
            ;;
    esac
}

require_positive_timeout FCR_DOCKER_DAEMON_TIMEOUT_SECONDS "$docker_daemon_timeout_seconds"
require_positive_timeout FCR_DOCKER_COMMAND_TIMEOUT_SECONDS "$docker_command_timeout_seconds"
case "$compose_project" in
    (''|[!a-z0-9]*|*[!a-z0-9_-]*|????????????????????????????????????????????????????????????????*)
        printf '%s\n' 'FCR_COMPOSE_PROJECT must be a lowercase Compose project identifier of at most 63 characters' >&2
        exit 2
        ;;
esac
reject_compose_control_overrides "$@" || exit $?

compose_env_snapshot_dir=$(mktemp -d "${TMPDIR:-/tmp}/fcr-compose-env.XXXXXX")
chmod 0700 "$compose_env_snapshot_dir"
compose_env_file="$compose_env_snapshot_dir/environment"
timeout_marker="$compose_env_snapshot_dir/command-timeout"
legacy_volume_inspect_error="$compose_env_snapshot_dir/legacy-volume-inspect-error"
cleanup_env_snapshot() {
    rm -f -- "$timeout_marker"
    rm -f -- "$legacy_volume_inspect_error"
    rm -f -- "$compose_env_file"
    rmdir "$compose_env_snapshot_dir" 2>/dev/null || true
}
trap cleanup_env_snapshot EXIT

python3 - "$compose_env_source" "$compose_env_file" <<'PY'
import os
import stat
import sys

source, destination = sys.argv[1:]
try:
    if stat.S_ISLNK(os.lstat(source).st_mode):
        raise SystemExit("delivery environment file must be a regular non-symlink file")
except OSError as exc:
    raise SystemExit(f"delivery environment file is unavailable: {type(exc).__name__}") from exc
flags = os.O_RDONLY
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
try:
    descriptor = os.open(source, flags)
except OSError as exc:
    raise SystemExit(f"delivery environment file is unavailable: {type(exc).__name__}") from exc
try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit("delivery environment file must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SystemExit("delivery environment file must not grant group or other permissions")
    with os.fdopen(descriptor, "rb", closefd=False) as handle:
        payload = handle.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise SystemExit("delivery environment file exceeds the 1 MiB safety limit")
finally:
    os.close(descriptor)

destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    destination_flags |= os.O_NOFOLLOW
output = os.open(destination, destination_flags, 0o600)
try:
    os.write(output, payload)
    os.fsync(output)
finally:
    os.close(output)
PY

timeout_runner_pid=

forward_timeout_signal() {
    signal_name=$1
    signal_status=$2
    if [ -n "$timeout_runner_pid" ]; then
        kill -"$signal_name" "$timeout_runner_pid" 2>/dev/null || true
        wait "$timeout_runner_pid" 2>/dev/null || true
        timeout_runner_pid=
    fi
    trap - HUP INT TERM
    exit "$signal_status"
}

run_with_timeout() {
    timeout_seconds=$1
    shift
    rm -f -- "$timeout_marker"
    python3 -c '
import os
import signal
import subprocess
import sys

timeout = int(sys.argv[1])
timeout_marker = sys.argv[2]
process = subprocess.Popen(sys.argv[3:], start_new_session=True)

def forward_signal(signum, _frame):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass

for signal_name in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(signal_name, forward_signal)

try:
    return_code = process.wait(timeout=timeout)
except subprocess.TimeoutExpired:
    marker = os.open(timeout_marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(marker)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    raise SystemExit(124)

if return_code < 0:
    raise SystemExit(128 - return_code)
raise SystemExit(return_code)
' "$timeout_seconds" "$timeout_marker" "$@" <&0 &
    timeout_runner_pid=$!
    trap 'forward_timeout_signal HUP 129' HUP
    trap 'forward_timeout_signal INT 130' INT
    trap 'forward_timeout_signal TERM 143' TERM
    if wait "$timeout_runner_pid"; then
        timeout_status=0
    else
        timeout_status=$?
    fi
    timeout_runner_pid=
    trap - HUP INT TERM
    return "$timeout_status"
}

command_timed_out() {
    [ -f "$timeout_marker" ]
}

blocked_docker_daemon() {
    printf '%s\n' 'BLOCKED_DOCKER_DAEMON_TIMEOUT' >&2
    return "$docker_timeout_exit_code"
}

docker_daemon_unavailable() {
    daemon_status=$1
    printf '%s\n' 'DOCKER_DAEMON_UNAVAILABLE' >&2
    return "$daemon_status"
}

blocked_docker_command() {
    printf '%s\n' 'BLOCKED_DOCKER_COMMAND_TIMEOUT' >&2
    return "$docker_timeout_exit_code"
}

probe_docker_daemon() {
    if run_with_timeout "$docker_daemon_timeout_seconds" docker info >/dev/null 2>&1; then
        return 0
    else
        daemon_status=$?
    fi
    if command_timed_out; then
        blocked_docker_daemon
        return $?
    fi
    docker_daemon_unavailable "$daemon_status"
}

run_docker_bounded() {
    timeout_seconds=$1
    shift
    if run_with_timeout "$timeout_seconds" docker "$@"; then
        return 0
    else
        command_status=$?
    fi
    if command_timed_out; then
        blocked_docker_command
        return $?
    fi
    return "$command_status"
}

compose_requires_daemon() {
    compose_subcommand=
    while [ "$#" -gt 0 ]; do
        case "$1" in
            (--ansi|--parallel|--profile|--progress)
                [ "$#" -ge 2 ] || return 0
                shift 2
                ;;
            (--ansi=*|--parallel=*|--profile=*|--progress=*)
                shift
                ;;
            (--all-resources|--compatibility|--dry-run|--all-resources=*|--compatibility=*|--dry-run=*)
                shift
                ;;
            (-h|--help|--version)
                compose_subcommand=$1
                break
                ;;
            (--)
                shift
                compose_subcommand=${1:-}
                break
                ;;
            (-*)
                return 0
                ;;
            (*)
                compose_subcommand=$1
                break
                ;;
        esac
    done
    case "$compose_subcommand" in
        (''|config|convert|version|help|completion|-h|--help|--version)
            return 1
            ;;
        (*)
            return 0
            ;;
    esac
}

if resolved_environment=$(run_with_timeout "$docker_daemon_timeout_seconds" \
    docker compose --env-file "$compose_env_file" --project-directory "$compose_project_directory" \
    -p "$compose_project" -f "$compose_base_file" -f "$compose_delivery_file" config --environment)
then
    :
else
    environment_status=$?
    if command_timed_out; then
        blocked_docker_command
        exit $?
    fi
    exit "$environment_status"
fi
resolved_or_default() {
    variable_name=$1
    default_value=$2
    resolved_value=$(printf '%s\n' "$resolved_environment" | sed -n "s/^${variable_name}=//p")
    if [ -n "$resolved_value" ]; then
        printf '%s\n' "$resolved_value"
    else
        printf '%s\n' "$default_value"
    fi
}

validate_compose_secret_files() {
    legacy_secret_names='DATABASE_PASSWORD POSTGRES_ADMIN_PASSWORD POSTGRES_OWNER_PASSWORD POSTGRES_APP_PASSWORD WRITE_GUARD_CODE WRITE_GUARD_SESSION_SECRET'
    for variable_name in $legacy_secret_names; do
        resolved_value=$(printf '%s\n' "$resolved_environment" | sed -n "s/^${variable_name}=//p")
        if [ -n "$resolved_value" ]; then
            printf '%s\n' "$variable_name must not be provided directly to Compose" >&2
            exit 2
        fi
    done

    secret_file_names='COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE COMPOSE_POSTGRES_OWNER_PASSWORD_FILE COMPOSE_POSTGRES_APP_PASSWORD_FILE COMPOSE_WRITE_GUARD_CODE_FILE COMPOSE_WRITE_GUARD_SESSION_SECRET_FILE'
    resolved_secret_files=
    resolved_secret_count=0
    for variable_name in $secret_file_names; do
        secret_file=$(printf '%s\n' "$resolved_environment" | sed -n "s/^${variable_name}=//p")
        if [ -n "$secret_file" ]; then
            resolved_secret_count=$((resolved_secret_count + 1))
            resolved_secret_files="$resolved_secret_files
$variable_name
$secret_file"
        fi
    done

    # A reduced Compose file used by tooling may define no secrets. The real
    # project Compose file makes all five variables mandatory during expansion.
    if [ "$resolved_secret_count" -eq 0 ]; then
        return
    fi
    if [ "$resolved_secret_count" -ne 5 ]; then
        printf '%s\n' 'all Compose secret files must be configured together' >&2
        exit 2
    fi

    FCR_RESOLVED_SECRET_FILES="$resolved_secret_files" python3 - \
        "$project_root" "$compose_env_file" "$compose_project" <<'PY'
import os
import secrets
import stat
import sys

project_root, compose_env_file, compose_project = sys.argv[1:]
lines = os.environ.pop("FCR_RESOLVED_SECRET_FILES").splitlines()
if lines and not lines[0]:
    lines = lines[1:]
if len(lines) % 2:
    raise SystemExit("invalid Compose secret-file validation input")

snapshot_names = {
    "COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE": "secret-postgres-admin",
    "COMPOSE_POSTGRES_OWNER_PASSWORD_FILE": "secret-postgres-owner",
    "COMPOSE_POSTGRES_APP_PASSWORD_FILE": "secret-postgres-app",
    "COMPOSE_WRITE_GUARD_CODE_FILE": "secret-write-guard-code",
    "COMPOSE_WRITE_GUARD_SESSION_SECRET_FILE": "secret-write-guard-session",
}
directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
project_fd = os.open(project_root, directory_flags)
secrets_root_fd = -1
snapshot_dir_fd = -1
try:
    try:
        os.mkdir(".env.secrets", 0o700, dir_fd=project_fd)
    except FileExistsError:
        pass
    secrets_root_fd = os.open(".env.secrets", directory_flags, dir_fd=project_fd)
    try:
        os.mkdir(f".compose-delivery-{compose_project}", 0o700, dir_fd=secrets_root_fd)
    except FileExistsError:
        pass
    snapshot_dir_fd = os.open(
        f".compose-delivery-{compose_project}",
        directory_flags,
        dir_fd=secrets_root_fd,
    )
    for label, descriptor in (
        ("Compose secret root", secrets_root_fd),
        ("Compose validated secret directory", snapshot_dir_fd),
    ):
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SystemExit(f"{label} must be private and owned by the current user")
    snapshot_dir = os.path.join(project_root, ".env.secrets", f".compose-delivery-{compose_project}")
finally:
    os.close(project_fd)

overrides: list[tuple[str, str]] = []
for index in range(0, len(lines), 2):
    variable_name, file_path = lines[index], lines[index + 1]
    if not file_path or "\x00" in file_path or "\r" in file_path or "\n" in file_path:
        raise SystemExit(f"{variable_name} must identify one secret file")
    source_path = file_path if os.path.isabs(file_path) else os.path.join(project_root, file_path)
    descriptor = -1
    try:
        descriptor = os.open(source_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        metadata_before = os.fstat(descriptor)
        if not stat.S_ISREG(metadata_before.st_mode) or not 0 < metadata_before.st_size <= 64 * 1024:
            raise SystemExit(f"{variable_name} must reference a non-empty bounded regular file")
        if metadata_before.st_uid != os.geteuid():
            raise SystemExit(f"{variable_name} must be owned by the current user")
        if stat.S_IMODE(metadata_before.st_mode) & 0o077:
            raise SystemExit(f"{variable_name} must not grant group or other permissions")
        with os.fdopen(descriptor, "rb") as secret_file:
            descriptor = -1
            raw_value = secret_file.read(64 * 1024 + 1)
            metadata_after = os.fstat(secret_file.fileno())
    except OSError as exc:
        raise SystemExit(
            f"{variable_name} must reference a readable non-symlink regular file"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if (
        metadata_after.st_dev != metadata_before.st_dev
        or metadata_after.st_ino != metadata_before.st_ino
        or metadata_after.st_ctime_ns != metadata_before.st_ctime_ns
        or metadata_after.st_mtime_ns != metadata_before.st_mtime_ns
        or metadata_after.st_size != metadata_before.st_size
    ):
        raise SystemExit(f"{variable_name} changed while it was being validated")

    if len(raw_value) > 64 * 1024:
        raise SystemExit(f"{variable_name} exceeds the maximum supported size")
    if raw_value.endswith(b"\n"):
        raw_value = raw_value[:-1]
        if raw_value.endswith(b"\r"):
            raw_value = raw_value[:-1]
    if not raw_value or b"\x00" in raw_value or b"\r" in raw_value or b"\n" in raw_value:
        raise SystemExit(f"{variable_name} must contain exactly one non-empty secret value")
    try:
        raw_value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"{variable_name} must contain valid UTF-8") from exc

    snapshot_name = snapshot_names[variable_name]
    temporary_name = f".{snapshot_name}.{secrets.token_hex(16)}"
    temporary_created = False
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=snapshot_dir_fd,
        )
        temporary_created = True
        try:
            os.write(temporary_fd, raw_value)
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)
        os.replace(
            temporary_name,
            snapshot_name,
            src_dir_fd=snapshot_dir_fd,
            dst_dir_fd=snapshot_dir_fd,
        )
        temporary_created = False
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=snapshot_dir_fd)
            except FileNotFoundError:
                pass
    os.fsync(snapshot_dir_fd)
    snapshot_path = os.path.join(snapshot_dir, snapshot_name)
    overrides.append((variable_name, snapshot_path))

environment_fd = os.open(compose_env_file, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW)
try:
    for variable_name, snapshot_path in overrides:
        os.write(environment_fd, f"\n{variable_name}={snapshot_path}".encode("utf-8"))
    os.write(environment_fd, b"\n")
    os.fsync(environment_fd)
finally:
    os.close(environment_fd)
try:
    os.fsync(snapshot_dir_fd)
    os.fsync(secrets_root_fd)
finally:
    os.close(snapshot_dir_fd)
    os.close(secrets_root_fd)
PY
}

validate_compose_secret_files

legacy_volume_is_missing() {
    error_file=$1
    volume_name=$2
    python3 - "$error_file" "$volume_name" <<'PY'
from pathlib import Path
import sys

error_file, volume_name = sys.argv[1:]
try:
    message = Path(error_file).read_text(encoding="utf-8", errors="strict").strip()
except (OSError, UnicodeError):
    raise SystemExit(1)

expected_messages = {
    f"Error response from daemon: get {volume_name}: no such volume",
    f"Error: No such volume: {volume_name}",
}
raise SystemExit(0 if message in expected_messages else 1)
PY
}

preflight_legacy_volumes() {
    case "${1:-}" in
        create|restart|run|start|up) ;;
        *) return ;;
    esac
    postgres_volume=$(resolved_or_default POSTGRES_VOLUME_NAME fj-final-cut-review_fj-final-cut-review-postgres)
    data_volume=$(resolved_or_default DATA_VOLUME_NAME fj-final-cut-review_fj-final-cut-review-data)
    state_volume=$(resolved_or_default RUNTIME_STATE_VOLUME_NAME fj-final-cut-review_fj-final-cut-review-runtime-state)
    for mapping in \
        "${compose_project}_fj-final-cut-review-postgres:$postgres_volume:POSTGRES_VOLUME_NAME" \
        "${compose_project}_fj-final-cut-review-data:$data_volume:DATA_VOLUME_NAME" \
        "${compose_project}_fj-final-cut-review-runtime-state:$state_volume:RUNTIME_STATE_VOLUME_NAME"
    do
        legacy_volume=${mapping%%:*}
        remainder=${mapping#*:}
        selected_volume=${remainder%%:*}
        variable_name=${remainder##*:}
        if [ "$legacy_volume" != "$selected_volume" ]; then
            rm -f -- "$legacy_volume_inspect_error"
            if run_with_timeout "$docker_daemon_timeout_seconds" \
                docker volume inspect "$legacy_volume" >/dev/null 2>"$legacy_volume_inspect_error"
            then
                printf '%s\n' "existing legacy Compose volume detected; set $variable_name to $legacy_volume or complete a verified backup/restore before starting" >&2
                exit 2
            else
                inspect_status=$?
            fi
            if command_timed_out; then
                blocked_docker_command
                exit $?
            fi
            if legacy_volume_is_missing "$legacy_volume_inspect_error" "$legacy_volume"; then
                rm -f -- "$legacy_volume_inspect_error"
                continue
            fi
            printf '%s\n' "legacy Compose volume absence could not be verified for $variable_name" >&2
            exit "$inspect_status"
        fi
    done
}

if [ -z "${BACKEND_IMAGE_REPOSITORY:-}" ]; then
    BACKEND_IMAGE_REPOSITORY=$(printf '%s\n' "$resolved_environment" | sed -n 's/^BACKEND_IMAGE_REPOSITORY=//p')
fi
if [ -z "${BACKEND_IMAGE_DIGEST:-}" ]; then
    BACKEND_IMAGE_DIGEST=$(printf '%s\n' "$resolved_environment" | sed -n 's/^BACKEND_IMAGE_DIGEST=//p')
fi
export BACKEND_IMAGE_REPOSITORY BACKEND_IMAGE_DIGEST

sh "$script_dir/validate-delivery-image-ref.sh" >/dev/null
if compose_requires_daemon "$@"; then
    probe_docker_daemon
    preflight_legacy_volumes "$compose_subcommand"
    run_docker_bounded "$docker_command_timeout_seconds" \
        compose --env-file "$compose_env_file" --project-directory "$compose_project_directory" \
        -p "$compose_project" -f "$compose_base_file" -f "$compose_delivery_file" "$@"
    exit $?
fi

run_docker_bounded "$docker_daemon_timeout_seconds" \
    compose --env-file "$compose_env_file" --project-directory "$compose_project_directory" \
    -p "$compose_project" -f "$compose_base_file" -f "$compose_delivery_file" "$@"
