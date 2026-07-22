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
output=${1:---digest}
docker_daemon_timeout_seconds=${FCR_DOCKER_DAEMON_TIMEOUT_SECONDS:-30}
docker_command_timeout_seconds=${FCR_DOCKER_COMMAND_TIMEOUT_SECONDS:-1800}
docker_timeout_exit_code=75

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

[ "$#" -le 1 ] || {
    printf '%s\n' 'usage: resolve-delivery-image-digest.sh [--repository|--digest]' >&2
    exit 2
}
case "$output" in
    (--repository|--digest) ;;
    (*)
        printf '%s\n' 'usage: resolve-delivery-image-digest.sh [--repository|--digest]' >&2
        exit 2
        ;;
esac

compose_env_snapshot_dir=$(mktemp -d "${TMPDIR:-/tmp}/fcr-digest-env.XXXXXX")
chmod 0700 "$compose_env_snapshot_dir"
compose_env_file="$compose_env_snapshot_dir/environment"
timeout_marker="$compose_env_snapshot_dir/command-timeout"
cleanup_env_snapshot() {
    rm -f -- "$timeout_marker"
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
output_descriptor = os.open(destination, destination_flags, 0o600)
try:
    os.write(output_descriptor, payload)
    os.fsync(output_descriptor)
finally:
    os.close(output_descriptor)
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
' "$timeout_seconds" "$timeout_marker" "$@" &
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

if compose_json=$(run_with_timeout "$docker_daemon_timeout_seconds" \
    docker compose --env-file "$compose_env_file" --project-directory "$compose_project_directory" \
        -p "$compose_project" -f "$compose_base_file" config --format json \
); then
    :
else
    compose_status=$?
    if command_timed_out; then
        blocked_docker_command
        exit $?
    fi
    exit "$compose_status"
fi
backend_image=$(printf '%s\n' "$compose_json" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["backend"]["image"])')

probe_docker_daemon
source_repository=$(python3 -c '
import re
import sys

reference = sys.argv[1]
if not reference or reference != reference.strip() or any(character.isspace() for character in reference):
    raise SystemExit("delivery backend image reference is invalid")
if "://" in reference:
    raise SystemExit("delivery backend image reference must not be a URL")

name = reference.split("@", 1)[0]
last_slash = name.rfind("/")
last_colon = name.rfind(":")
repository = name[:last_colon] if last_colon > last_slash else name
if not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+", repository):
    raise SystemExit("delivery backend image must use a registry-qualified repository")
registry = repository.split("/", 1)[0]
if registry != "localhost" and "." not in registry and ":" not in registry:
    raise SystemExit("delivery backend image must use a registry-qualified repository")
print(repository)
' "$backend_image")
repo_digests=$(run_docker_bounded "$docker_command_timeout_seconds" \
    image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$backend_image")
repo_digest=$(printf '%s\n' "$repo_digests" | python3 -c '
import re
import sys

source_repository = sys.argv[1]
digest_pattern = re.compile(r"^(?P<repository>[^@]+)@sha256:(?P<digest>[0-9a-f]{64})$")
matches = []
for raw_line in sys.stdin:
    candidate = raw_line.strip()
    if not candidate:
        continue
    parsed = digest_pattern.fullmatch(candidate)
    if parsed is None:
        raise SystemExit("image inspect returned an invalid registry RepoDigest")
    if parsed.group("repository") == source_repository:
        matches.append(candidate)

if len(matches) != 1:
    raise SystemExit("image must expose exactly one registry RepoDigest for its source repository")
print(matches[0])
' "$source_repository")

case "$output" in
    --repository)
        printf '%s\n' "${repo_digest%@sha256:*}"
        ;;
    --digest)
        printf '%s\n' "${repo_digest#*@sha256:}"
        ;;
esac
