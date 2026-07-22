#!/bin/sh
set -eu

pgdata=${PGDATA:-/var/lib/postgresql/data}
version_file="$pgdata/PG_VERSION"

if [ -L "$pgdata" ] || [ -L "$version_file" ]; then
    printf '%s\n' 'PostgreSQL data path must not be a symlink' >&2
    exit 1
fi
if [ -e "$version_file" ]; then
    if [ ! -f "$version_file" ]; then
        printf '%s\n' 'PostgreSQL PG_VERSION must be a regular file' >&2
        exit 1
    fi
    version=$(cat "$version_file")
    if [ "$version" != "16" ]; then
        printf '%s\n' 'PostgreSQL data volume major version mismatch; use verified backup and restore' >&2
        exit 1
    fi
fi

exec docker-entrypoint.sh "$@"
