#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="/daten/sicherung.postgresql/postgresql-backup.sh"
old_target="/daten/sicherung.postgresql/sicherung.postgresql.sh"
pgpass="$HOME/.pgpass"
export PGPASSFILE="$pgpass"

[[ "$(id -un)" == "hannes" ]] || { echo "Run as hannes on Latitude, without sudo." >&2; exit 1; }
mountpoint -q /daten || { echo "ERROR: /daten is not mounted." >&2; exit 1; }
[[ -f "$pgpass" && ! -L "$pgpass" ]] || { echo "ERROR: Create $pgpass first (mode 0600)." >&2; exit 1; }
[[ "$(stat -c '%a' "$pgpass")" == 600 ]] || { echo "ERROR: $pgpass must have mode 0600." >&2; exit 1; }
for database in MXServer; do
    psql -w -h localhost -U hannes -d "$database" -X -q -c 'SELECT 1' >/dev/null
done
docker exec owncloud-staging-db-1 pg_isready -U owncloud_stage -d owncloud_stage >/dev/null
docker exec camera-postgres-db-1 sh -c \
    'export PGPASSWORD="$POSTGRES_PASSWORD"; for db in camDoor camHinten camPi3; do psql -w -U postgres -d "$db" -X -q -c "SELECT 1" >/dev/null || exit 1; done'
command -v pg_dump >/dev/null
command -v pg_restore >/dev/null
command -v flock >/dev/null

echo "Running an initial backup before updating the schedule."
bash "$script_dir/postgresql-backup.sh"
install -m 0700 "$script_dir/postgresql-backup.sh" "$target"
python3 - "$old_target" "$target" <<'PY'
import subprocess
import sys

old, new = sys.argv[1:]
cron = subprocess.run(["crontab", "-l"], text=True, capture_output=True, check=True).stdout
lines = cron.splitlines()
matches = [
    i for i, line in enumerate(lines)
    if line.strip() and not line.lstrip().startswith("#")
    and (old in line or new in line)
]
if len(matches) != 1:
    raise SystemExit(f"ERROR: Expected exactly one PostgreSQL cron entry, found {len(matches)}; cron unchanged.")
index = matches[0]
line = lines[index]
if '/usr/local/sbin/run-sync-with-email "PostgreSQL backups"' not in line:
    raise SystemExit("ERROR: Unexpected PostgreSQL cron entry; cron unchanged.")
lines[index] = line.replace(old, new)
subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
PY
echo "PostgreSQL backup installed; cron schedule unchanged."
