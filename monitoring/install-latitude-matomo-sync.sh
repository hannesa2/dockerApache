#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C

(( EUID == 0 )) || { echo "Run with sudo on Latitude." >&2; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE=/home/hannes/.local/state/matomo-mirror

install -o root -g root -m 0755 "$SCRIPT_DIR/sync-job-report.py" \
    /home/hannes/bin/sync-job-report.py
sudo -H -u hannes python3 "$SCRIPT_DIR/sync-matomo.py"

if ! sudo -H -u hannes python3 - <<'PY'
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
path = Path("/home/hannes/.local/state/sync-job-notify/status") / (
    hashlib.sha256(b"Matomo mirror sync").hexdigest() + ".json")
if not path.is_file():
    sys.exit(1)
state = json.loads(path.read_text())
if state.get("result") != "SUCCESS" or not state.get("last_success"):
    sys.exit(1)
last_success = dt.datetime.strptime(state["last_success"], "%Y-%m-%dT%H:%M:%S%z")
age = dt.datetime.now().astimezone() - last_success
sys.exit(0 if dt.timedelta(0) <= age < dt.timedelta(days=1) else 1)
PY
then
    sudo -H -u hannes /usr/local/sbin/run-sync-with-email "Matomo mirror sync" \
        /usr/bin/python3 "$SCRIPT_DIR/sync-matomo.py" --apply
fi

install -m 0644 /dev/null /etc/cron.d/matomo-mirror-sync
cat > /etc/cron.d/matomo-mirror-sync <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
HOME=/home/hannes
45 4 * * * hannes /usr/local/sbin/run-sync-with-email "Matomo mirror sync" /usr/bin/python3 "$SCRIPT_DIR/sync-matomo.py" --apply >> "$STATE/cron.log" 2>&1
EOF
echo "Matomo mirror sync installed for 04:45 daily; status included in availability reports."
