#!/bin/sh
set -eu
umask 077

if [ -n "${DELIVERY_IMAGE_REPOSITORY:-}" ] || [ -n "${DELIVERY_IMAGE_DIGEST:-}" ]; then
    repository=${DELIVERY_IMAGE_REPOSITORY:?delivery image repository is required}
    digest=${DELIVERY_IMAGE_DIGEST:?delivery image digest is required}
    case "$repository" in
        ''|*[!a-z0-9./:_-]*) printf '%s\n' 'invalid delivery image repository' >&2; exit 1 ;;
    esac
    case "${repository##*/}" in
        *:*) printf '%s\n' 'delivery image repository must not include a mutable tag' >&2; exit 1 ;;
    esac
    case "$digest" in
        ''|*[!0-9a-f]*) printf '%s\n' 'invalid delivery image digest' >&2; exit 1 ;;
    esac
    if [ "${#digest}" -ne 64 ]; then
        printf '%s\n' 'invalid delivery image digest' >&2
        exit 1
    fi
fi

if [ "$(id -u)" -eq 0 ]; then
    mkdir -p /data
    app_uid=$(id -u app)
    app_gid=$(id -g app)
    runtime_state_root=/var/lib/fj-runtime-state
    if [ -L "$runtime_state_root" ]; then
        printf '%s\n' 'runtime state root must not be a symlink' >&2
        exit 1
    fi
    mkdir -p "$runtime_state_root"
    chown root:root "$runtime_state_root"
    chmod 0755 "$runtime_state_root"
    ownership_lock="$runtime_state_root/.ownership-lock"
    lock_timeout_seconds=${DATA_OWNERSHIP_LOCK_TIMEOUT_SECONDS:-120}
    lock_stale_seconds=${DATA_OWNERSHIP_LOCK_STALE_SECONDS:-30}
    case "$lock_timeout_seconds:$lock_stale_seconds" in
        *[!0-9:]*|0:*|*:0) printf '%s\n' 'invalid data ownership lock timeout' >&2; exit 1 ;;
    esac
    if [ "$lock_stale_seconds" -le 5 ]; then
        printf '%s\n' 'data ownership lock stale threshold must exceed the heartbeat interval' >&2
        exit 1
    fi
    lock_attempt=0
    while ! mkdir "$ownership_lock" 2>/dev/null; do
        if [ -d "$ownership_lock" ] && [ ! -L "$ownership_lock" ]; then
            lock_timestamp=$(stat -c '%Y' "$ownership_lock")
            if [ -f "$ownership_lock/heartbeat" ] && [ ! -L "$ownership_lock/heartbeat" ]; then
                lock_timestamp=$(stat -c '%Y' "$ownership_lock/heartbeat")
            fi
            lock_age=$(($(date +%s) - lock_timestamp))
            if [ "$lock_age" -ge "$lock_stale_seconds" ]; then
                stale_lock="$runtime_state_root/.ownership-lock-stale-$(cat /proc/sys/kernel/random/uuid)"
                if mv "$ownership_lock" "$stale_lock" 2>/dev/null; then
                    rm -f "$stale_lock/owner" "$stale_lock/heartbeat"
                    if ! rmdir "$stale_lock"; then
                        printf '%s\n' 'stale ownership lock contains unexpected entries' >&2
                        exit 1
                    fi
                    continue
                fi
            fi
        fi
        lock_attempt=$((lock_attempt + 1))
        if [ "$lock_attempt" -ge "$lock_timeout_seconds" ]; then
            printf '%s\n' 'timed out waiting for data ownership lock' >&2
            exit 1
        fi
        sleep 1
    done
    lock_token="$(hostname)-$$-$(cat /proc/sys/kernel/random/uuid)"
    printf '%s\n' "$lock_token" > "$ownership_lock/owner"
    touch "$ownership_lock/heartbeat"
    (
        while [ -f "$ownership_lock/owner" ] \
            && [ "$(cat "$ownership_lock/owner")" = "$lock_token" ]; do
            touch "$ownership_lock/heartbeat"
            sleep 5
        done
    ) &
    lock_heartbeat_pid=$!
    cleanup_ownership_lock() {
        kill "$lock_heartbeat_pid" 2>/dev/null || true
        wait "$lock_heartbeat_pid" 2>/dev/null || true
        if [ -f "$ownership_lock/owner" ] \
            && [ "$(cat "$ownership_lock/owner")" = "$lock_token" ]; then
            rm -f "$ownership_lock/owner" "$ownership_lock/heartbeat"
            rmdir "$ownership_lock" 2>/dev/null || true
        fi
    }
    assert_ownership_lock() {
        if [ ! -f "$ownership_lock/owner" ] \
            || [ "$(cat "$ownership_lock/owner")" != "$lock_token" ]; then
            printf '%s\n' 'data ownership lock lease lost' >&2
            exit 1
        fi
    }
    trap cleanup_ownership_lock EXIT HUP INT TERM
    assert_ownership_lock
    data_identity=$(stat -c '%d:%i' /data)
    ownership_state="$runtime_state_root/data-ownership-v1-${app_uid}-${app_gid}"
    state_valid=0
    if [ "${FORCE_DATA_OWNERSHIP_MIGRATION:-0}" != 1 ] \
        && [ -f "$ownership_state" ] \
        && [ ! -L "$ownership_state" ] \
        && [ "$(stat -c '%u:%g:%a' "$ownership_state")" = '0:0:444' ] \
        && [ "$(cat "$ownership_state")" = "$data_identity" ]; then
        state_valid=1
    fi
    if [ "$state_valid" -ne 1 ]; then
        assert_ownership_lock
        ownership_scan=$(mktemp "$runtime_state_root/.ownership-scan.XXXXXX")
        if ! find /data -xdev \( ! -uid "$app_uid" -o ! -gid "$app_gid" \) -print -quit > "$ownership_scan"; then
            rm -f "$ownership_scan"
            printf '%s\n' 'data ownership scan failed' >&2
            exit 1
        fi
        if [ -s "$ownership_scan" ]; then
            assert_ownership_lock
            printf '%s\n' 'data ownership migration required' >&2
            chown -R -h -P app:app /data
        fi
        rm -f "$ownership_scan"
        state_tmp=$(mktemp "$runtime_state_root/.ownership-state.XXXXXX")
        printf '%s\n' "$data_identity" > "$state_tmp"
        chown root:root "$state_tmp"
        chmod 0444 "$state_tmp"
        assert_ownership_lock
        mv -f "$state_tmp" "$ownership_state"
    fi
    cleanup_ownership_lock
    trap - EXIT HUP INT TERM
    exec gosu app "$@"
fi

exec "$@"
