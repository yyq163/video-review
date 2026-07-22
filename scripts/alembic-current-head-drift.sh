#!/usr/bin/env bash
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"
: "${POSTGRES_OWNER_DATABASE_URL:?POSTGRES_OWNER_DATABASE_URL is required}"
: "${POSTGRES_ADMIN_DATABASE_URL:?POSTGRES_ADMIN_DATABASE_URL is required}"
: "${POSTGRES_APP_DB:?POSTGRES_APP_DB is required}"
: "${POSTGRES_TEST_DB:?POSTGRES_TEST_DB is required}"

if [[ "${RUN_POSTGRES_CONSTRAINT_TESTS:-}" != "1" ]]; then
  printf '%s\n' 'RUN_POSTGRES_CONSTRAINT_TESTS=1 is required' >&2
  exit 2
fi

backend/.venv/bin/python - <<'PY'
from __future__ import annotations

import os
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

root = Path.cwd()
runtime_url = make_url(os.environ["DATABASE_URL"])
owner_url = make_url(os.environ["POSTGRES_OWNER_DATABASE_URL"])
admin_url = make_url(os.environ["POSTGRES_ADMIN_DATABASE_URL"])
test_database = os.environ["POSTGRES_TEST_DB"]
application_database = os.environ["POSTGRES_APP_DB"]

for name, url in (
    ("DATABASE_URL", runtime_url),
    ("POSTGRES_OWNER_DATABASE_URL", owner_url),
    ("POSTGRES_ADMIN_DATABASE_URL", admin_url),
):
    if not url.drivername.startswith("postgresql"):
        raise SystemExit(f"{name} must use PostgreSQL")

if runtime_url.database != test_database or owner_url.database != test_database:
    raise SystemExit("runtime and owner URLs must target POSTGRES_TEST_DB")
if len({application_database, test_database, admin_url.database}) != 3:
    raise SystemExit("administration, application, and test databases must be distinct")
if len({runtime_url.username, owner_url.username, admin_url.username}) != 3:
    raise SystemExit("runtime, owner, and administration roles must be distinct")

config = Config(str(root / "backend/alembic.ini"))
config.set_main_option("script_location", str(root / "backend/alembic"))
heads = ScriptDirectory.from_config(config).get_heads()
if len(heads) != 1:
    raise SystemExit("Alembic source must have exactly one head")

engine = create_engine(runtime_url)
try:
    with engine.connect() as connection:
        current = connection.scalar(text("SELECT version_num FROM alembic_version"))
finally:
    engine.dispose()
if current != heads[0]:
    raise SystemExit("database current revision does not match source head")
PY

backend/.venv/bin/pytest \
  backend/tests/test_postgresql_constraints.py::test_postgresql_owner_defaults_match_metadata_after_previous_head_upgrade_and_fresh_install \
  -q

printf '%s\n' 'Alembic current/head and metadata drift checks passed'
