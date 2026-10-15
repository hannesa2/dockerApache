#!/bin/bash
set -euo pipefail

source_host="latitude"
source_dir="/daten/githubArtifactsPy"
data_root="/Volumes/SamsungT5_1T/githubArtifactsPy"
target_dir="$data_root/files"
lock_dir="$data_root/.sync-in-progress"

if ! /sbin/mount | /usr/bin/awk \
    '$2 == "on" && $3 == "/Volumes/SamsungT5_1T" { mounted = 1 } END { exit !mounted }'; then
    echo "ERROR: SamsungT5_1T is not mounted; refusing to sync." >&2
    exit 1
fi

mkdir -p "$target_dir"
if ! mkdir "$lock_dir" 2>/dev/null; then
    echo "ERROR: Another GitHub artifacts sync is running (or lock exists): $lock_dir" >&2
    exit 1
fi
trap 'rmdir "$lock_dir"' EXIT

if ! ssh -o BatchMode=yes -o ConnectTimeout=15 "$source_host" \
    "test -d '$source_dir' && test -r '$source_dir' &&
     find '$source_dir' -mindepth 1 -maxdepth 1 -type f -print -quit | grep -q ."; then
    echo "ERROR: Source directory is unavailable or empty; refusing destructive sync." >&2
    exit 1
fi

/usr/bin/rsync -rt --delete --delay-updates --human-readable --stats \
    -e "ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3" \
    "$source_host:$source_dir/" "$target_dir/"
