#!/bin/sh
set -eu

repository=${BACKEND_IMAGE_REPOSITORY:?BACKEND_IMAGE_REPOSITORY is required}
digest=${BACKEND_IMAGE_DIGEST:?BACKEND_IMAGE_DIGEST is required}

case "$repository" in
    ''|*[!a-z0-9./:_-]*)
        printf '%s\n' 'BACKEND_IMAGE_REPOSITORY has an invalid repository name' >&2
        exit 1
        ;;
esac
case "${repository##*/}" in
    *:*)
        printf '%s\n' 'BACKEND_IMAGE_REPOSITORY must not include a mutable tag' >&2
        exit 1
        ;;
esac

case "$digest" in
    ''|*[!0-9a-f]*) printf '%s\n' 'BACKEND_IMAGE_DIGEST has an invalid digest' >&2; exit 1 ;;
esac

if [ "${#digest}" -ne 64 ]; then
    printf '%s\n' 'BACKEND_IMAGE_DIGEST must contain exactly 64 hexadecimal digest characters' >&2
    exit 1
fi

printf '%s\n' 'delivery image reference validation passed'
