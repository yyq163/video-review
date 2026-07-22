#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd -P)
cd "$project_root"

compose() {
    sh "$script_dir/compose-delivery.sh" "$@"
}

assert_required_services_healthy() {
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
    raise SystemExit("Compose did not return exactly the required delivery services")
unhealthy = [
    service
    for service in sorted(expected)
    if str(by_service[service].get("State", "")).lower() != "running"
    or str(by_service[service].get("Health", "")).lower() != "healthy"
]
if unhealthy:
    raise SystemExit("required delivery services are not healthy: " + ", ".join(unhealthy))
'
}

normalize_runtime_image_id() {
    python3 -c '
import re
import sys

image_id = sys.argv[1]
match = re.fullmatch(r"(?:sha256:)?([0-9a-f]{64})", image_id)
if match is None:
    raise SystemExit("Compose returned a non-canonical runtime image id")
print(f"sha256:{match.group(1)}")
' "$1"
}

resolve_runtime_image_id() {
    service=$1
    image_ids=$(compose images -q "$service")
    set -- $image_ids
    if [ "$#" -ne 1 ]; then
        printf '%s\n' "Compose must resolve exactly one runtime image for $service" >&2
        return 1
    fi
    image_id=$(normalize_runtime_image_id "$1")

    inspected_ids=$(docker image inspect "$image_id" --format '{{.Id}}')
    set -- $inspected_ids
    if [ "$#" -ne 1 ]; then
        printf '%s\n' "Docker must inspect exactly one runtime image for $service" >&2
        return 1
    fi
    inspected_id=$(normalize_runtime_image_id "$1")
    if [ "$inspected_id" != "$image_id" ]; then
        printf '%s\n' "Compose and Docker image identities differ for $service" >&2
        return 1
    fi

    container_ids=$(compose ps -q "$service")
    set -- $container_ids
    if [ "$#" -ne 1 ]; then
        printf '%s\n' "Compose must resolve exactly one runtime container for $service" >&2
        return 1
    fi
    container_image_ids=$(docker container inspect "$1" --format '{{.Image}}')
    set -- $container_image_ids
    if [ "$#" -ne 1 ]; then
        printf '%s\n' "Docker must inspect exactly one container image for $service" >&2
        return 1
    fi
    container_image_id=$(normalize_runtime_image_id "$1")
    if [ "$container_image_id" != "$image_id" ]; then
        printf '%s\n' "Compose image and container image identities differ for $service" >&2
        return 1
    fi
    printf '%s\n' "$image_id"
}

compose config --quiet
compose build
compose up -d --wait --force-recreate --wait-timeout 180
assert_required_services_healthy
sh "$script_dir/postgres-backup-restore-smoke.sh"
sh "$script_dir/verify-compose-persistence.sh"
backend_runtime_image=$(resolve_runtime_image_id backend)
postgres_runtime_image=$(resolve_runtime_image_id postgres)
set -- env -i \
    PATH="$PATH" \
    HOME="${HOME:-/tmp}" \
    TMPDIR="${TMPDIR:-/tmp}" \
    DOCKER_CONFIG="${DOCKER_CONFIG:-${HOME:-/tmp}/.docker}"
[ "${DOCKER_HOST+x}" = x ] && set -- "$@" "DOCKER_HOST=$DOCKER_HOST"
[ "${DOCKER_CONTEXT+x}" = x ] && set -- "$@" "DOCKER_CONTEXT=$DOCKER_CONTEXT"
[ "${DOCKER_TLS+x}" = x ] && set -- "$@" "DOCKER_TLS=$DOCKER_TLS"
[ "${DOCKER_TLS_VERIFY+x}" = x ] && set -- "$@" "DOCKER_TLS_VERIFY=$DOCKER_TLS_VERIFY"
[ "${DOCKER_CERT_PATH+x}" = x ] && set -- "$@" "DOCKER_CERT_PATH=$DOCKER_CERT_PATH"
[ "${DOCKER_API_VERSION+x}" = x ] && set -- "$@" "DOCKER_API_VERSION=$DOCKER_API_VERSION"
set -- "$@" \
    RUN_DOCKER_RUNTIME_TESTS=1 \
    BACKEND_RUNTIME_IMAGE="$backend_runtime_image" \
    POSTGRES_RUNTIME_IMAGE="$postgres_runtime_image" \
    backend/.venv/bin/pytest backend/tests/test_docker_runtime.py -q
"$@"
assert_required_services_healthy
