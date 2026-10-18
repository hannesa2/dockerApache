#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: Run this installer with sudo on latitude." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC_SCRIPT="$SCRIPT_DIR/sync-latitude.sh"
INSTALL_PATH="/usr/local/sbin/owncloud-mirror-sync"
CRON_FILE="/etc/cron.d/owncloud-mirror-sync"

if [[ ! -f "$SYNC_SCRIPT" ]]; then
    echo "ERROR: Sync script not found at $SYNC_SCRIPT." >&2
    exit 1
fi

if [[ ! -r /home/hannes/.ssh/id_rsa || ! -r /home/hannes/.ssh/known_hosts ]]; then
    echo "ERROR: The existing latitude-to-mac2016 SSH key or known_hosts is unavailable." >&2
    exit 1
fi

install -o root -g root -m 0750 "$SYNC_SCRIPT" "$INSTALL_PATH"
printf '%s\n' \
    'SHELL=/bin/bash' \
    'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
    '45 3 * * * root /usr/local/sbin/owncloud-mirror-sync >> /var/log/owncloud-mirror-sync.log 2>&1' \
    > "$CRON_FILE"
chown root:root "$CRON_FILE"
chmod 0644 "$CRON_FILE"

echo "Installed the daily 03:45 ownCloud mirror job on latitude."
echo "Starting the initial synchronization now; it may take a while on the first run."
"$INSTALL_PATH"
