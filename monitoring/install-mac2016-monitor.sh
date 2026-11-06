#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_SCRIPT="$SCRIPT_DIR/systems-health-check-mac.sh"
INSTALL_DIR="$HOME/bin"
INSTALL_SCRIPT="$INSTALL_DIR/systems-health-check-mac.sh"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/systems-health-monitor"
CRON_MARKER="# dockerApache mac2016 systems health monitor"
CRON_ENTRY="10 * * * * /usr/bin/env HOME=$HOME PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin $INSTALL_SCRIPT >> $STATE_DIR/mac2016-monitor.log 2>&1"

if [[ "$(id -un)" != hannes || "$(hostname -s)" != mac2016 ]]; then
    echo "ERROR: Run this installer as user hannes on mac2016." >&2
    exit 1
fi

for command in mail curl docker ssh python3 crontab; do
    command -v "$command" >/dev/null || {
        echo "ERROR: Required command is unavailable: $command" >&2
        exit 1
    }
done

if [[ ! -r "$HOME/.ssh/id_rsa" || ! -r "$HOME/.ssh/known_hosts" ]]; then
    echo "ERROR: mac2016-to-Latitude SSH key or known_hosts is unavailable." >&2
    exit 1
fi

install -d -m 0750 "$INSTALL_DIR" "$STATE_DIR"
install -m 0750 "$SOURCE_SCRIPT" "$INSTALL_SCRIPT"

current_crontab="$(mktemp)"
updated_crontab="$(mktemp)"
trap 'rm -f "$current_crontab" "$updated_crontab"' EXIT
if ! crontab -l > "$current_crontab" 2>/dev/null; then
    : > "$current_crontab"
fi
grep -vF "$CRON_MARKER" "$current_crontab" |
    grep -vF "$INSTALL_SCRIPT" > "$updated_crontab" || true
{
    cat "$updated_crontab"
    printf '%s\n%s\n' "$CRON_MARKER" "$CRON_ENTRY"
} | crontab -

echo "Installed the hourly mac2016-hosted availability monitor at minute 10."
echo "It checks mac2016 and Latitude, emailing only on failures or recovery."
echo "The user crontab runs independently of an interactive login."
echo "Logs and notification state are stored under $STATE_DIR."
echo "Running the initial check now."
"$INSTALL_SCRIPT"
