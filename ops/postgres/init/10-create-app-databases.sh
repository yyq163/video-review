#!/bin/sh
set -eu

# Application roles and databases are managed by the idempotent migrate service.
# This first-initialization hook only removes the unsafe default schema CREATE grant
# from the administration database; it intentionally receives no application secrets.
psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" <<'EOSQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
EOSQL
