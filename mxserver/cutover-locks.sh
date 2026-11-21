#!/usr/bin/env bash

# Source this helper and install an EXIT trap that calls release_cutover_locks.
acquire_cutover_locks() {
    local backup_lock="$1" mirror_lock="$2"
    [[ -f "$backup_lock" && ! -L "$backup_lock" ]] || {
        echo "ERROR: Expected existing backup lock file: $backup_lock" >&2
        return 1
    }
    exec {CUTOVER_BACKUP_FD}>>"$backup_lock"
    if ! python3 -c 'import fcntl, sys; fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)' \
        "$CUTOVER_BACKUP_FD"; then
        echo "ERROR: PostgreSQL backup is already running." >&2
        exec {CUTOVER_BACKUP_FD}>&-
        unset CUTOVER_BACKUP_FD
        return 1
    fi
    if ! mkdir -- "$mirror_lock"; then
        echo "ERROR: MXServer mirror sync is running or its lock exists: $mirror_lock" >&2
        exec {CUTOVER_BACKUP_FD}>&-
        unset CUTOVER_BACKUP_FD
        return 1
    fi
    CUTOVER_MIRROR_LOCK="$mirror_lock"
}

release_cutover_locks() {
    local failed=0
    if [[ -n "${CUTOVER_MIRROR_LOCK:-}" ]]; then
        if ! rmdir -- "$CUTOVER_MIRROR_LOCK"; then
            echo "ERROR: Could not remove owned mirror lock: $CUTOVER_MIRROR_LOCK" >&2
            failed=1
        fi
        unset CUTOVER_MIRROR_LOCK
    fi
    if [[ -n "${CUTOVER_BACKUP_FD:-}" ]]; then
        exec {CUTOVER_BACKUP_FD}>&- || failed=1
        unset CUTOVER_BACKUP_FD
    fi
    return "$failed"
}
