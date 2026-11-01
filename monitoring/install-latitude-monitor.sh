#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_SCRIPT="$SCRIPT_DIR/systems-health-check.sh"
INSTALL_DIR="$HOME/bin"
INSTALL_SCRIPT="$INSTALL_DIR/systems-health-check.sh"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/systems-health-monitor"
CRON_MARKER="# dockerApache systems health monitor"
CRON_ENTRY="0 * * * * $INSTALL_SCRIPT >> $STATE_DIR/monitor.log 2>&1"

if [[ "$(id -un)" != hannes ]]; then
    echo "ERROR: Run this installer as user hannes on latitude (no sudo needed)." >&2
    exit 1
fi

for command in mail curl docker ssh flock crontab; do
    command -v "$command" >/dev/null || {
        echo "ERROR: Required command is unavailable: $command" >&2
        exit 1
    }
done

if [[ ! -r /home/hannes/.ssh/id_rsa || ! -r /home/hannes/.ssh/known_hosts ]]; then
    echo "ERROR: Latitude-to-mac2016 SSH key or known_hosts is unavailable." >&2
    exit 1
fi

install -d -m 0750 "$INSTALL_DIR" "$STATE_DIR"
install -m 0750 "$SOURCE_SCRIPT" "$INSTALL_SCRIPT"

current_crontab="$(mktemp)"
updated_crontab="$(mktemp)"
trap 'rm -f "$current_crontab" "$updated_crontab"' EXIT
if ! crontab -l >"$current_crontab" 2>/dev/null; then
    : >"$current_crontab"
fi
grep -vF "$CRON_MARKER" "$current_crontab" |
    grep -vF "$INSTALL_SCRIPT" >"$updated_crontab" || true
{
    cat "$updated_crontab"
    printf '%s\n%s\n' "$CRON_MARKER" "$CRON_ENTRY"
} | crontab -

echo "Installed an hourly availability check on latitude."
echo "It checks Latitude and mac2016 services, emailing only on state changes."
echo "Running the initial check now."
"$INSTALL_SCRIPT"
