#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C

(( EUID == 0 )) || { echo "Run with sudo on Latitude." >&2; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -d -o hannes -g hannes -m 0750 /home/hannes/.local/state/sync-job-notify/status
install -o root -g root -m 0755 "$SCRIPT_DIR/sync-job-report.py" \
    /home/hannes/bin/sync-job-report.py
install -o root -g root -m 0755 "$SCRIPT_DIR/../owncloud/run-sync-with-email.sh" \
    /usr/local/sbin/run-sync-with-email

python3 - <<'PY'
from pathlib import Path
path = Path("/etc/cron.d/nextcloud-mirror-sync")
if path.exists():
    text = path.read_text()
    command = "/home/hannes/git/dockerApache/nextcloud/sync-to-mirror.sh"
    lines = []
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and command in line:
            if "/usr/local/sbin/run-sync-with-email" not in line:
                line = line.replace(command, '/usr/local/sbin/run-sync-with-email "Nextcloud mirror sync" ' + command, 1)
        lines.append(line)
    path.write_text("\n".join(lines) + "\n")
PY
echo "Sync status recording installed; schedules unchanged and no sync job started."
