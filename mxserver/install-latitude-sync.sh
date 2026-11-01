#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: Run this installer with sudo on latitude." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC_SCRIPT="$SCRIPT_DIR/sync-latitude.sh"
INSTALL_PATH="/usr/local/sbin/mxserver-mirror-sync"
CRON_FILE="/etc/cron.d/mxserver-mirror-sync"
EMAIL_WRAPPER="/usr/local/sbin/run-sync-with-email"

if [[ ! -f "$SYNC_SCRIPT" ]]; then
    echo "ERROR: Sync script not found at $SYNC_SCRIPT." >&2
    exit 1
fi
if [[ ! -x "$EMAIL_WRAPPER" ]]; then
    echo "ERROR: Install the sync notification wrapper before this job: $EMAIL_WRAPPER" >&2
    exit 1
fi
if [[ ! -r /home/hannes/.ssh/id_rsa || ! -r /home/hannes/.ssh/known_hosts ]]; then
    echo "ERROR: Latitude-to-mac2016 SSH key or known_hosts is unavailable." >&2
    exit 1
fi

install -o root -g root -m 0750 "$SYNC_SCRIPT" "$INSTALL_PATH"
printf '%s\n' \
    'SHELL=/bin/bash' \
    'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
    '30 4 * * * root /usr/local/sbin/run-sync-with-email "MXServer mirror sync" /usr/local/sbin/mxserver-mirror-sync >> /var/log/mxserver-mirror-sync.log 2>&1' \
    > "$CRON_FILE"
chown root:root "$CRON_FILE"
chmod 0644 "$CRON_FILE"

echo "Installed the daily 04:30 MXServer mirror job on latitude."
echo "Starting the initial synchronization now; the first 16 GB picture copy may take a while."
"$EMAIL_WRAPPER" "MXServer mirror sync" "$INSTALL_PATH"
