#!/usr/bin/env bash
set -uo pipefail

recipient="hannes.software@gmx.at"

if [[ "$#" -lt 2 ]]; then
    echo "Usage: $0 <job-name> <command> [arguments...]" >&2
    exit 64
fi

job_name="$1"
shift
host="$(hostname -f 2>/dev/null || hostname)"

dotenv_value() {
    python3 - "$1" "$2" "$3" <<'PY'
import re
import shlex
import sys
from pathlib import Path

path, key, fallback = sys.argv[1:]
result = fallback
try:
    lines = Path(path).read_text().splitlines()
except FileNotFoundError:
    lines = []
except OSError as error:
    raise SystemExit(f"Cannot read target configuration: {error}")
for line in lines:
    match = re.match(r"^\s*(?:export\s+)?" + re.escape(key) + r"\s*=(.*)$", line)
    if not match:
        continue
    raw = match.group(1).strip()
    if any(char in raw for char in ("$", "`", "\n", "\r", ";")):
        raise SystemExit(f"Cannot safely resolve {key} in {path}")
    try:
        words = shlex.split(raw, comments=True, posix=True)
    except ValueError as error:
        raise SystemExit(f"Cannot parse {key} in {path}: {error}")
    if len(words) > 1:
        raise SystemExit(f"Cannot safely resolve {key} in {path}")
    result = words[0] if words else ""
print(result)
PY
}

target=""
case "$job_name" in
    "PostgreSQL backups")
        target="${POSTGRES_BACKUP_ROOT:-/daten/sicherung.postgresql/dump}/daily/ (lokal)"
        ;;
    "Matomo mirror sync")
        target="mac2016 ${MATOMO_MIRROR_DIR:-/Users/hannes/server/apache/www/mxdocs/piwik}"
        ;;
    "ownCloud mirror sync")
        target="mac2016 /Volumes/SamsungT5_1T/ownCloudBackup/owncloud"
        ;;
    "MXServer mirror sync")
        target="mac2016 /Volumes/SamsungT5_1T/MXServerMirror"
        ;;
    "GitLab standby backup")
        target="${MIRROR_HOST:-mac2016.local} GitLab-Standby (gitlab16; /Users/hannes/server/gitlab)"
        ;;
    "Apache mirror sync" | "Nextcloud mirror sync")
        if [[ "$job_name" == "Apache mirror sync" ]]; then
            config="/home/hannes/git/dockerApache/apache/.env"
            if apache_dir="$(dotenv_value "$config" MIRROR_APACHE_DIR "${MIRROR_APACHE_DIR:-~/git/dockerApache/apache}")"; then
                [[ -n "$apache_dir" ]] || apache_dir="${MIRROR_APACHE_DIR:-~/git/dockerApache/apache}"
            else
                apache_dir=""
            fi
            default_dir="${apache_dir:+$apache_dir/data}"
            host_default="${MIRROR_HOST:-mac2016}"
        else
            config="/home/hannes/git/dockerApache/nextcloud/.env"
            host_default="${MIRROR_HOST:-}"
            default_dir="${3:-${MIRROR_NEXTCLOUD_DIR:-/opt/nextcloud}}"
            if [[ -z "${3:-}" ]]; then
                default_dir="$(dotenv_value "$config" MIRROR_NEXTCLOUD_DIR "$default_dir")" || default_dir=""
            fi
        fi
        if mirror_host="$(dotenv_value "$config" MIRROR_HOST "$host_default")"; then
            if [[ "$job_name" == "Apache mirror sync" && -z "$mirror_host" ]]; then
                mirror_host="${MIRROR_HOST:-mac2016}"
            fi
        else
            mirror_host=""
        fi
        if [[ "$job_name" == "Nextcloud mirror sync" && -n "${2:-}" ]]; then
            mirror_host="$2"
        fi
        if mirror_dir="$(dotenv_value "$config" MIRROR_DATA_DIR "${MIRROR_DATA_DIR:-$default_dir}")"; then
            [[ -n "$mirror_dir" ]] || mirror_dir="$default_dir"
        else
            mirror_dir=""
        fi
        if [[ -n "$mirror_host" && -n "$mirror_dir" ]]; then
            target="$mirror_host $mirror_dir"
        else
            target="unbekannt (Mirror-Konfiguration prüfen)"
        fi
        ;;
    *)
        target="unbekannt (für diesen Job nicht hinterlegt)"
        ;;
esac
route="$host -> $target"
output_file="$(mktemp)"
trap 'rm -f "$output_file"' EXIT

started_at="$(date '+%Y-%m-%dT%H:%M:%S%z')"
reporter="${SYNC_JOB_REPORTER:-/home/hannes/bin/sync-job-report.py}"
python3 "$reporter" --record "$job_name" RUNNING "$started_at" "" || {
    echo "ERROR: Could not record sync start." >&2
    exit 75
}
if "$@" >"$output_file" 2>&1; then
    job_status=0
else
    job_status=$?
fi
finished_at="$(date '+%Y-%m-%dT%H:%M:%S%z')"

if [[ "$job_status" -eq 0 ]] && grep -Eqi 'another .* (is )?already running' "$output_file"; then
    result="SKIPPED"
    excerpt_lines=40
elif [[ "$job_status" -eq 0 ]]; then
    result="SUCCESS"
    excerpt_lines=40
else
    result="FAILED (exit $job_status)"
    excerpt_lines=120
fi
python3 "$reporter" --record "$job_name" "$result" "$started_at" "$finished_at" || {
    echo "ERROR: Could not record sync completion." >&2
    exit 75
}

subject="[Latitude] $result: $job_name ($route)"
{
    printf 'Job: %s\n' "$job_name"
    printf 'Result: %s\n' "$result"
    printf 'Host: %s\n' "$route"
    printf 'Started: %s\n' "$started_at"
    printf 'Finished: %s\n\n' "$finished_at"
    printf 'Output (last %s lines):\n' "$excerpt_lines"
    if [[ -s "$output_file" ]]; then
        tr '\r' '\n' < "$output_file" | tail -n "$excerpt_lines"
    else
        printf '(no command output)\n'
    fi
} | mail -s "$subject" "$recipient"
mail_status=$?

tr '\r' '\n' < "$output_file"

if [[ "$mail_status" -ne 0 ]]; then
    echo "ERROR: Could not send job notification to $recipient (mail exit $mail_status)." >&2
    if [[ "$job_status" -eq 0 ]]; then
        exit 75
    fi
fi

exit "$job_status"
