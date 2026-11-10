#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C

(( EUID == 0 )) || { echo "Run with sudo on Latitude." >&2; exit 1; }
for job in owncloud mxserver; do
    script="/usr/local/sbin/$job-mirror-sync"
    [[ -f "$script" ]] || { echo "ERROR: Missing installed sync: $script" >&2; exit 1; }
    if grep -q '/Volumes/SamsungT7_2T' "$script"; then
        backup="$script.before-t7-to-t5"
        [[ -e "$backup" ]] || cp -p "$script" "$backup"
        temporary="$(mktemp "$script.XXXXXX")"
        sed 's@/Volumes/SamsungT7_2T@/Volumes/SamsungT5_1T@g' "$script" > "$temporary"
        if ! bash -n "$temporary"; then
            rm -f "$temporary"
            echo "ERROR: Updated sync failed syntax validation: $script" >&2
            exit 1
        fi
        chown root:root "$temporary"
        chmod 0750 "$temporary"
        mv "$temporary" "$script"
    fi
    grep -q '/Volumes/SamsungT5_1T' "$script"
    echo "Updated installed $job sync to SamsungT5_1T; schedule unchanged."
done
