#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root_dir"

tracked_env_count=0
while IFS= read -r path; do
  if [ "$path" != ".env.example" ]; then
    tracked_env_count=$((tracked_env_count + 1))
  fi
done < <(git ls-files '.env*')
if [ "$tracked_env_count" -gt 0 ]; then
  printf 'security gate: blocked; tracked environment files=%s\n' "$tracked_env_count" >&2
  exit 1
fi

credential_pattern='(postgres(ql)?://[^<[:space:]]+:[^<[:space:]]+@|(git\+)?https?://[^/@:<[:space:]]+(:[^@/<[:space:]]*)?@|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|npm_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,})'
candidate_list="$(mktemp "${TMPDIR:-/tmp}/fcr-security-candidates.XXXXXX")"
chmod 600 "$candidate_list"
trap 'rm -f "$candidate_list"' EXIT
git ls-files -co --exclude-standard -z >"$candidate_list"

lockfile_is_safe() {
  python3 - "$1" <<'PY'
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

SENSITIVE_NORMALIZED_KEYS = {
    "auth",
    "authtoken",
    "npmauthtoken",
    "accesstoken",
    "bearertoken",
    "authorization",
    "password",
    "passwd",
    "registryauth",
    "registryauthtoken",
    "registrytoken",
}
SENSITIVE_KEY_SUFFIXES = ("authtoken", "accesstoken", "bearertoken")
SENSITIVE_QUERY_KEYS = SENSITIVE_NORMALIZED_KEYS | {"key", "secret", "token"}
SUPPORTED_URL_SCHEMES = {"http", "https", "git+http", "git+https"}
SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"npm_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
)
PLACEHOLDERS = {"", "null", "redacted", "<redacted>", "<token>"}


def is_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in PLACEHOLDERS or (normalized.startswith("${") and normalized.endswith("}"))


def normalize_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def key_is_sensitive(value: object) -> bool:
    normalized = normalize_key(value)
    return normalized in SENSITIVE_NORMALIZED_KEYS or normalized.endswith(SENSITIVE_KEY_SUFFIXES)


def string_contains_credentials(value: str) -> bool:
    if any(pattern.search(value) for pattern in SECRET_PATTERNS):
        return True
    if not value.lower().startswith(("http://", "https://", "git+http://", "git+https://")):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    if parsed.scheme.lower() not in SUPPORTED_URL_SCHEMES:
        return False
    if parsed.username is not None or parsed.password is not None:
        return True
    query_values = parse_qsl(parsed.query, keep_blank_values=True)
    query_values.extend(parse_qsl(parsed.fragment, keep_blank_values=True))
    return any(
        normalize_key(key) in SENSITIVE_QUERY_KEYS and not is_placeholder(item)
        for key, item in query_values
    )


def contains_credentials(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key_is_sensitive(key) and isinstance(item, str) and not is_placeholder(item):
                return True
            if contains_credentials(item):
                return True
        return False
    if isinstance(value, list):
        return any(contains_credentials(item) for item in value)
    return isinstance(value, str) and string_contains_credentials(value)


try:
    document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError):
    raise SystemExit(2)
raise SystemExit(1 if contains_credentials(document) else 0)
PY
}

candidate_count=0
violation_count=0
while IFS= read -r -d '' path; do
  [ -e "$path" ] || [ -L "$path" ] || continue
  case "$path" in
    scripts/run-security-gate.sh)
      continue
      ;;
  esac
  candidate_count=$((candidate_count + 1))
  if [ -L "$path" ]; then
    violation_count=$((violation_count + 1))
    continue
  fi
  case "$path" in
    package-lock.json)
      if ! lockfile_is_safe "$path"; then
        violation_count=$((violation_count + 1))
      fi
      continue
      ;;
    .codex-agent-team/reports/browser-qa/*/local-qa-proxy.py)
      if git ls-files --error-unmatch -- "$path" >/dev/null 2>&1; then
        violation_count=$((violation_count + 1))
      fi
      continue
      ;;
    .env.example)
      ;;
    .env|.env.*|*/.env|*/.env.*)
      violation_count=$((violation_count + 1))
      continue
      ;;
  esac
  if [ ! -r "$path" ]; then
    violation_count=$((violation_count + 1))
    continue
  fi
  if LC_ALL=C grep -I -E -q "$credential_pattern" -- "$path" 2>/dev/null; then
    violation_count=$((violation_count + 1))
  else
    grep_status=$?
    if [ "$grep_status" -gt 1 ]; then
      violation_count=$((violation_count + 1))
    fi
  fi
done <"$candidate_list"

if [ "$violation_count" -gt 0 ]; then
  printf 'security gate: blocked; violations=%s\n' "$violation_count" >&2
  printf '%s\n' "credential pattern, candidate environment file, tracked QA proxy, unreadable file, invalid lockfile, or symlink found" >&2
  exit 1
fi

env -u NODE_TLS_REJECT_UNAUTHORIZED \
  npm audit --registry=https://registry.npmjs.org --audit-level=high
backend/.venv/bin/pip-audit \
  --requirement backend/requirements-dev.txt \
  --strict \
  --progress-spinner off
backend/.venv/bin/pip check
backend/.venv/bin/pytest \
  backend/tests/test_safe_upload_writes.py \
  backend/tests/test_database_governance.py \
  backend/tests/test_api_contract_runtime.py \
  -q

printf 'security candidate files scanned=%s\n' "$candidate_count"
echo "security gate: PASS"
