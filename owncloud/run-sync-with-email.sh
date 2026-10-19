#!/usr/bin/env bash
set -uo pipefail

recipient="hannes.software@gmx.at"

if [[ "$#" -lt 2 ]]; then
    echo "Usage: $0 <job-name> <command> [arguments...]" >&2
    exit 64
fi

job_name="$1"
shift
output_file="$(mktemp)"
trap 'rm -f "$output_file"' EXIT

started_at="$(date '+%Y-%m-%dT%H:%M:%S%z')"
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

subject="[Latitude] $result: $job_name"
{
    printf 'Job: %s\n' "$job_name"
    printf 'Result: %s\n' "$result"
    printf 'Host: %s\n' "$(hostname -f 2>/dev/null || hostname)"
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
