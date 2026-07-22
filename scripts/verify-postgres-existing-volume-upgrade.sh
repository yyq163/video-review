#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
probe_id="$(openssl rand -hex 16)"
safe_id="$probe_id"
legacy_project="fj-pg-upgrade-legacy-${safe_id//_/-}"
current_project="fj-pg-upgrade-current-${safe_id//_/-}"
postgres_volume="${legacy_project}_fj-final-cut-review-postgres"
data_volume="${legacy_project}_fj-final-cut-review-data"
runtime_state_volume="${legacy_project}_fj-final-cut-review-runtime-state"
probe_dir="$(mktemp -d "${TMPDIR:-/tmp}/fj-pg-upgrade-probe.XXXXXX")"
chmod 700 "$probe_dir"
env_file="$probe_dir/environment"
admin_secret_file="$probe_dir/postgres-admin-password"
owner_secret_file="$probe_dir/postgres-owner-password"
app_secret_file="$probe_dir/postgres-app-password"
code_secret_file="$probe_dir/write-guard-code"
session_secret_file="$probe_dir/write-guard-session-secret"
probe_resources_owned=0

cleanup() {
  local exit_code=$?
  local cleanup_failed=0
  local index
  local volume
  local volume_role
  local -a expected_volume_names=(
    "$postgres_volume"
    "$data_volume"
    "$runtime_state_volume"
  )
  local -a expected_volume_roles=(postgres data runtime-state)

  trap - EXIT
  if [[ "$probe_resources_owned" == 1 ]]; then
    for ((index = 0; index < ${#expected_volume_names[@]}; index += 1)); do
      volume="${expected_volume_names[$index]}"
      volume_role="${expected_volume_roles[$index]}"
      if ! docker volume inspect "$volume" >/dev/null 2>&1 && [[ "$exit_code" == 0 ]]; then
        printf 'retained-volume precondition failed: role=%s status=missing\n' "$volume_role" >&2
        cleanup_failed=1
      fi
    done

    if ! docker compose \
      --project-directory "$root_dir" \
      --project-name "$current_project" \
      --env-file "$env_file" \
      down --remove-orphans --rmi local >/dev/null 2>&1; then
      printf 'cleanup failed for current Compose probe resources\n' >&2
      cleanup_failed=1
    fi
    if ! docker compose \
      --project-directory "$root_dir" \
      --project-name "$legacy_project" \
      --env-file "$env_file" \
      down --remove-orphans --rmi local >/dev/null 2>&1; then
      printf 'cleanup failed for legacy Compose probe resources\n' >&2
      cleanup_failed=1
    fi

    for ((index = 0; index < ${#expected_volume_names[@]}; index += 1)); do
      volume="${expected_volume_names[$index]}"
      volume_role="${expected_volume_roles[$index]}"
      if docker volume inspect "$volume" >/dev/null 2>&1; then
        printf 'retained-volume: role=%s status=preserved\n' "$volume_role"
      else
        printf 'retained-volume check failed: role=%s status=missing\n' "$volume_role" >&2
        cleanup_failed=1
      fi
    done
  fi
  if ! rm -f "$env_file" "$admin_secret_file" "$owner_secret_file" \
    "$app_secret_file" "$code_secret_file" "$session_secret_file"; then
    cleanup_failed=1
  fi
  rmdir "$probe_dir" 2>/dev/null || true

  if [[ "$exit_code" == 0 && "$cleanup_failed" != 0 ]]; then
    exit_code=2
  fi
  if [[ "$exit_code" == 0 ]]; then
    echo "Existing PostgreSQL 16 named-volume cross-project upgrade: PASS"
  fi
  exit "$exit_code"
}
trap cleanup EXIT

admin_password="$(openssl rand -hex 24)"
owner_password="$(openssl rand -hex 24)"
app_password="$(openssl rand -hex 24)"
write_guard_code="$(openssl rand -hex 24)"
session_secret="$(openssl rand -hex 32)"
probe_name="fj_probe_${safe_id}"

FCR_ADMIN_PASSWORD="$admin_password" \
FCR_OWNER_PASSWORD="$owner_password" \
FCR_APP_PASSWORD="$app_password" \
FCR_WRITE_GUARD_CODE="$write_guard_code" \
FCR_SESSION_SECRET="$session_secret" \
python3 - "$env_file" "$admin_secret_file" "$owner_secret_file" \
  "$app_secret_file" "$code_secret_file" "$session_secret_file" "$probe_name" "$postgres_volume" \
  "$data_volume" "$runtime_state_volume" "$current_project" "$safe_id" <<'PY'
import os
import stat
import sys

(
    environment_path,
    admin_path,
    owner_path,
    app_path,
    code_path,
    session_path,
    probe_name,
    postgres_volume,
    data_volume,
    runtime_state_volume,
    current_project,
    safe_id,
) = sys.argv[1:]


def write_private_file(path: str, payload: bytes, *, single_line: bool) -> None:
    if not payload or b"\x00" in payload or b"\r" in payload:
        raise SystemExit("generated private file content is unsafe")
    if single_line and b"\n" in payload:
        raise SystemExit("generated secret must contain exactly one non-empty value")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        remaining = memoryview(payload + b"\n")
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise SystemExit("generated private file write was incomplete")
            remaining = remaining[written:]
        os.fsync(descriptor)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            raise SystemExit("generated secret file permissions are unsafe")
    finally:
        os.close(descriptor)


secret_files = (
    (admin_path, os.environ.pop("FCR_ADMIN_PASSWORD").encode("utf-8")),
    (owner_path, os.environ.pop("FCR_OWNER_PASSWORD").encode("utf-8")),
    (app_path, os.environ.pop("FCR_APP_PASSWORD").encode("utf-8")),
    (code_path, os.environ.pop("FCR_WRITE_GUARD_CODE").encode("utf-8")),
    (session_path, os.environ.pop("FCR_SESSION_SECRET").encode("utf-8")),
)
for secret_path, secret_value in secret_files:
    write_private_file(secret_path, secret_value, single_line=True)

environment = f"""\
POSTGRES_HOST_PORT=0
POSTGRES_ADMIN_DB=postgres
POSTGRES_ADMIN_USER=postgres
POSTGRES_OWNER_USER={probe_name}_owner
POSTGRES_APP_DB={probe_name}_db
POSTGRES_APP_USER={probe_name}_app
POSTGRES_TEST_DB={probe_name}_test
POSTGRES_VOLUME_NAME={postgres_volume}
DATA_VOLUME_NAME={data_volume}
RUNTIME_STATE_VOLUME_NAME={runtime_state_volume}
BACKEND_IMAGE={current_project}-backend:local
REVERSE_PROXY_TRUSTED_HOSTS=127.0.0.1
LEGACY_VOLUME_PROBE_SENTINEL={safe_id}
COMPOSE_POSTGRES_ADMIN_PASSWORD_FILE={admin_path}
COMPOSE_POSTGRES_OWNER_PASSWORD_FILE={owner_path}
COMPOSE_POSTGRES_APP_PASSWORD_FILE={app_path}
COMPOSE_WRITE_GUARD_CODE_FILE={code_path}
COMPOSE_WRITE_GUARD_SESSION_SECRET_FILE={session_path}
"""
write_private_file(
    environment_path,
    environment.rstrip("\n").encode("utf-8"),
    single_line=False,
)
PY
unset admin_password owner_password app_password write_guard_code session_secret

legacy_probe_python='import os
import sys

from backend.scripts import legacy_volume_upgrade_probe as probe
from backend.scripts.bootstrap_database_roles import read_secret_from_environment

action = sys.argv[1]
if action not in {"seed", "verify"}:
    raise SystemExit("unsupported legacy volume probe action")
for secret_name in ("POSTGRES_ADMIN_PASSWORD", "POSTGRES_APP_PASSWORD"):
    if os.environ.get(secret_name) or not os.environ.get(f"{secret_name}_FILE"):
        raise SystemExit(f"Compose probe requires {secret_name}_FILE only")
if os.environ.get("ALLOW_DESTRUCTIVE_LEGACY_VOLUME_PROBE") != "1":
    raise SystemExit("legacy volume probe requires an explicit safety opt-in")
config = probe.ProbeConfig(
    host=probe._required("POSTGRES_ADMIN_HOST"),
    port=int(probe._required("POSTGRES_ADMIN_PORT")),
    admin_database=probe._required("POSTGRES_ADMIN_DB"),
    admin_user=probe._required("POSTGRES_ADMIN_USER"),
    admin_password=read_secret_from_environment("POSTGRES_ADMIN_PASSWORD"),
    app_database=probe._probe_identifier("POSTGRES_APP_DB"),
    app_user=probe._probe_identifier("POSTGRES_APP_USER"),
    app_password=read_secret_from_environment("POSTGRES_APP_PASSWORD"),
    owner_user=probe._probe_identifier("POSTGRES_OWNER_USER"),
    sentinel=probe._required("LEGACY_VOLUME_PROBE_SENTINEL"),
)
getattr(probe, action)(config)'

legacy_compose=(
  docker compose
  --project-directory "$root_dir"
  --project-name "$legacy_project"
  --env-file "$env_file"
)
current_compose=(
  docker compose
  --project-directory "$root_dir"
  --project-name "$current_project"
  --env-file "$env_file"
)

for project in "$legacy_project" "$current_project"; do
  if [[ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$project")" ]]; then
    printf 'refusing to reuse existing Compose project resources: %s\n' "$project" >&2
    exit 2
  fi
  if docker network inspect "${project}_default" >/dev/null 2>&1; then
    printf 'refusing to reuse existing Compose network: %s_default\n' "$project" >&2
    exit 2
  fi
done
for volume in "$postgres_volume" "$data_volume" "$runtime_state_volume"; do
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    printf 'refusing to reuse existing probe volume: %s\n' "$volume" >&2
    exit 2
  fi
done
for image in "${legacy_project}-postgres" "${current_project}-postgres" "${current_project}-backend:local"; do
  if docker image inspect "$image" >/dev/null 2>&1; then
    printf 'refusing to reuse existing probe image: %s\n' "$image" >&2
    exit 2
  fi
done
probe_resources_owned=1

echo "Building isolated PostgreSQL and migration images..."
"${legacy_compose[@]}" build postgres migrate
echo "Materializing isolated application named volumes without deleting them..."
"${current_compose[@]}" run --rm --no-deps --entrypoint /bin/true backend
echo "Creating an isolated legacy PostgreSQL 16 named volume..."
"${legacy_compose[@]}" up -d --wait --no-deps postgres
"${legacy_compose[@]}" run --rm --no-deps \
  -e ALLOW_DESTRUCTIVE_LEGACY_VOLUME_PROBE=1 \
  -e LEGACY_VOLUME_PROBE_SENTINEL="${safe_id}" \
  migrate python -c "$legacy_probe_python" seed

echo "Restarting the same named volume and applying current bootstrap/migrations..."
"${legacy_compose[@]}" stop postgres
"${current_compose[@]}" up -d --wait --no-deps postgres
"${current_compose[@]}" run --rm --no-deps migrate

echo "Verifying data preservation, ownership transfer, and runtime least privilege..."
"${current_compose[@]}" run --rm --no-deps \
  -e ALLOW_DESTRUCTIVE_LEGACY_VOLUME_PROBE=1 \
  -e LEGACY_VOLUME_PROBE_SENTINEL="${safe_id}" \
  migrate python -c "$legacy_probe_python" verify
