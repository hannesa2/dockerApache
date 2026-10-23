#!/usr/bin/env bash
set -euo pipefail

recipient="hannes.software@gmx.at"
owner="hannes"
owner_home="/home/$owner"
helper_source="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run-sync-with-email.sh"
helper_target="/usr/local/sbin/run-sync-with-email"
owncloud_job="/usr/local/sbin/owncloud-mirror-sync"
owncloud_cron="/etc/cron.d/owncloud-mirror-sync"
postgres_backup="/daten/sicherung.postgresql/sicherung.postgresql.sh"
gitlab_backup="$owner_home/bin/gitlab-mac-backup.sh"
state_dir="$owner_home/.local/state/sync-job-notify"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: Run this installer with sudo on latitude." >&2
    exit 1
fi

if [[ ! -f "$helper_source" ]]; then
    echo "ERROR: Notification helper not found: $helper_source" >&2
    exit 1
fi

for command in mail python3 crontab; do
    command -v "$command" >/dev/null || {
        echo "ERROR: Required command is unavailable: $command" >&2
        exit 1
    }
done

for job in "$owncloud_job" "$gitlab_backup" "$postgres_backup"; do
    [[ -x "$job" ]] || {
        echo "ERROR: Expected executable scheduled job is missing: $job" >&2
        exit 1
    }
done

install -o root -g root -m 0755 "$helper_source" "$helper_target"
install -d -o "$owner" -g "$owner" -m 0750 "$state_dir"

user_crontab_tmp="$(mktemp)"
trap 'rm -f "$user_crontab_tmp"' EXIT
chmod 0600 "$user_crontab_tmp"

python3 - "$owncloud_cron" "$postgres_backup" "$user_crontab_tmp" <<'PY'
from pathlib import Path
import sys

cron_path = Path(sys.argv[1])
postgres_backup = sys.argv[2]
user_crontab = Path(sys.argv[3])

if not cron_path.is_file():
    raise SystemExit(f"Required ownCloud cron file is missing: {cron_path}")

lines = cron_path.read_text().splitlines()
updated = []
found_owncloud = False
for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith(("SHELL=", "PATH=")):
        updated.append(line)
        continue
    fields = stripped.split(None, 6)
    if len(fields) < 7 or fields[5] != "root" or "/usr/local/sbin/owncloud-mirror-sync" not in fields[6]:
        updated.append(line)
        continue
    updated.append(
        f"{' '.join(fields[:5])} root /usr/local/sbin/run-sync-with-email "
        f"\"ownCloud mirror sync\" /usr/local/sbin/owncloud-mirror-sync "
        ">> /var/log/owncloud-mirror-sync.log 2>&1"
    )
    found_owncloud = True

if not found_owncloud:
    raise SystemExit(f"Could not find the existing ownCloud cron entry in {cron_path}")

cron_path.write_text("\n".join(updated) + "\n")

import subprocess

result = subprocess.run(
    ["crontab", "-u", "hannes", "-l"],
    text=True,
    capture_output=True,
)
if result.returncode not in (0, 1):
    raise SystemExit(f"Could not read hannes crontab: {result.stderr.strip()}")

lines = result.stdout.splitlines()
jobs = {
    "/home/hannes/bin/gitlab-mac-backup.sh": (
        "15 3 * * * /usr/local/sbin/run-sync-with-email "
        '"GitLab standby backup" /home/hannes/bin/gitlab-mac-backup.sh '
        ">> /home/hannes/.local/state/gitlab-mac-backup/cron.log 2>&1"
    ),
    postgres_backup: (
        "15 19 * * * /usr/local/sbin/run-sync-with-email "
        '"PostgreSQL backups" /daten/sicherung.postgresql/sicherung.postgresql.sh '
        ">> /home/hannes/.local/state/sync-job-notify/postgresql.log 2>&1"
    ),
}
seen = set()
updated = []
for line in lines:
    match = next((path for path in jobs if path in line), None)
    if match is None:
        updated.append(line)
        continue
    if match in seen:
        continue
    updated.append(jobs[match])
    seen.add(match)

missing = set(jobs) - seen
if missing:
    raise SystemExit("Could not find scheduled user-cron job(s): " + ", ".join(sorted(missing)))

user_crontab.write_text("\n".join(updated) + "\n")
PY

chown root:root "$owncloud_cron"
chmod 0644 "$owncloud_cron"
crontab -u "$owner" "$user_crontab_tmp"

python3 - "$postgres_backup" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
lines = text.splitlines()
if not lines or lines[0].strip() != "#!/bin/bash":
    raise SystemExit(f"Unexpected PostgreSQL backup script header: {path}")
if "set -Eeuo pipefail" not in lines[:5]:
    lines.insert(1, "set -Eeuo pipefail")
    path.write_text("\n".join(lines) + "\n")
PY

if ! printf 'Latitude sync-job notifications are configured.\nHost: %s\nTime: %s\n' \
    "$(hostname -f 2>/dev/null || hostname)" \
    "$(date '+%Y-%m-%dT%H:%M:%S%z')" \
    | mail -s "[Latitude] Notification setup test" "$recipient"; then
    echo "ERROR: Configuration is installed, but the test email could not be queued." >&2
    exit 1
fi

echo "Configured success/failure emails for ownCloud, GitLab, and PostgreSQL jobs."
echo "A setup test message was sent to $recipient."
echo "No backup or mirror sync was started."
