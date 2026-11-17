#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

fail() { echo "ERROR: $*" >&2; exit 1; }
mode="${1:-}"
[[ $# == 1 && "$mode" == --seed-only || $# == 2 && "$mode" == --finish-seed ]] ||
    fail "Usage: sudo bash monitoring/seed-latitude-to-beelink.sh --seed-only | --finish-seed /daten/latitude-beelink-seed.XXXXXXXX"
[[ "$(id -u)" == 0 && "$(hostname -s | tr '[:upper:]' '[:lower:]')" == latitude ]] ||
    fail "Run as root directly on Latitude."
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target=/daten/latitude-migration
expected_uuid=c0c42461-ae4c-4edb-a993-bba94a05ed6c
ssh=(ssh -i /home/hannes/.ssh/id_rsa -o IdentitiesOnly=yes -o BatchMode=yes
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/home/hannes/.ssh/known_hosts
    -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 hannes@beelink)
for cmd in rsync docker mysql mysqldump pg_restore gzip git tar python3 flock sha256sum runuser; do
    command -v "$cmd" >/dev/null || fail "Missing command: $cmd"
done
mountpoint -q /daten || fail "Latitude /daten is not mounted."
exec 9>/run/lock/latitude-beelink-seed.lock
flock -n 9 || fail "Another seed is running."
if [[ "$mode" == --finish-seed ]]; then
    work="$2"
    [[ "$work" =~ ^/daten/latitude-beelink-seed\.[[:alnum:]]{8}$ &&
        -d "$work" && ! -L "$work" &&
        "$(stat -c '%u:%a' "$work")" == 0:700 ]] ||
        fail "Expected an existing root-owned private seed directory."
    [[ -f "$work/seed.log" && -f "$work/databases/SHA256SUMS" ]] ||
        fail "Seed diagnostics or database checksums are missing."
    grep -Fq ' Seeding repository code, without data trees, local caches or Git metadata...' \
        "$work/seed.log" || fail "The live-file seed did not reach the repository step; use --seed-only."
fi
if [[ "$mode" == --seed-only ]]; then
native_mysql_options=(--user=root)
native_mysql_password=
if [[ -f /etc/mysql/debian.cnf && ! -L /etc/mysql/debian.cnf ]] &&
    mysql --defaults-file=/etc/mysql/debian.cnf --batch --skip-column-names \
        -e 'SELECT 1' >/dev/null 2>&1; then
    native_mysql_options=(--defaults-file=/etc/mysql/debian.cnf)
elif ! mysql --user=root --batch --skip-column-names -e 'SELECT 1' >/dev/null 2>&1; then
    echo "Native MySQL requires administrator credentials; no data has been copied."
    [[ -t 0 ]] || fail "Run interactively to enter the native MySQL root password."
    IFS= read -r -s -p "Native MySQL root password (not Docker MySQL): " native_mysql_password
    printf '\n'
    MYSQL_PWD="$native_mysql_password" mysql --user=root --batch --skip-column-names \
        -e 'SELECT 1' >/dev/null || fail "Native MySQL administrator authentication failed."
fi
for container in mxserver-postgres-db-1 camera-postgres-db-1 owncloud-staging-db-1 \
    owncloud-staging-owncloud-1 apache-db-1 gitlab; do
    [[ "$(docker inspect -f '{{.State.Running}}' "$container")" == true ]] ||
        fail "Source container is not running: $container"
done
fi
"${ssh[@]}" "set -eu
test \"\$(hostname -s)\" = beelink
ls /daten >/dev/null
test \"\$(findmnt -rn -M /daten -t ext4 -o UUID)\" = '$expected_uuid'
test ! -L '$target'
test ! -e '$target/files-promoted'
umask 077
if test -e '$target'; then
    test -f '$target/.latitude-seed'
    test \"\$(cat '$target/.latitude-seed')\" = latitude-to-beelink-v1
else
    mkdir '$target'
    printf '%s\n' latitude-to-beelink-v1 > '$target/.latitude-seed'
fi
mkdir -p '$target/metadata' '$target/files'
rm -f '$target/seed-complete'
command -v rsync >/dev/null
if test '$mode' = --seed-only; then
    test \"\$(df -Pk /daten | awk 'NR==2 {print \$4}')\" -ge 104857600
fi
"
if [[ "$mode" == --seed-only ]]; then
    work="$(mktemp -d /daten/latitude-beelink-seed.XXXXXXXX)"
fi
log="$work/seed.log"
exec > >(tee -a "$log") 2>&1
trap 'status=$?; if [[ $status != 0 ]]; then echo "ERROR: Incomplete seed; diagnostics retained at '"$work"'. Production and schedules were not switched."; fi' EXIT
log() { printf '%s %s\n' "$(date -Is)" "$*"; }
rsync_ssh="ssh -i /home/hannes/.ssh/id_rsa -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/home/hannes/.ssh/known_hosts -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"
rsync_base=(rsync -aHAX --numeric-ids --partial --info=stats1
    --rsync-path='rsync --fake-super' -e "$rsync_ssh")
if [[ "$mode" == --seed-only ]]; then
python3 - "$work/data-paths" <<'PY'
from pathlib import Path
import sys

paths = [
    "/daten/owncloud/config", "/daten/owncloud/apps", "/daten/owncloud/data",
    "/srv/www.docker/www", "/srv/www.docker/vhosts", "/srv/www.docker/certs",
    "/srv/www.docker/php-config", "/srv/www.docker/mysql-config", "/srv/www/vhosts",
    "/srv/postgres/data/mxinfo", "/opt/glassfish-4.0", "/opt/wildfly",
    "/home/hannes/git/MXServer", "/home/hannes/git/githubArtifactsPy",
    "/home/hannes/git/githubArtifacts", "/home/hannes/git/SendMQTT",
    "/daten/githubArtifactsPy",
]
resolved = set()
with Path(sys.argv[1]).open("wb") as stream:
    for name in paths:
        path = Path(name)
        if not path.exists():
            raise SystemExit("ERROR: Expected source path is missing: " + name)
        # A symlink such as /opt/wildfly must copy its actual tree as well.
        for value in (path, path.resolve()):
            relative = str(value.relative_to("/"))
            if relative not in resolved:
                stream.write(relative.encode() + b"\0")
                resolved.add(relative)
PY
log "Creating private inventory and logical database snapshots. No source service will be stopped."
python3 - "$work" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

work = Path(sys.argv[1])
ids = subprocess.check_output(["docker", "ps", "-aq"], text=True).split()
(work / "docker-inspect.json").write_bytes(
    subprocess.check_output(["docker", "inspect", *ids]))
for filename, command in (
    ("packages.txt", ["dpkg-query", "-W"]),
    ("services.txt", ["systemctl", "list-units", "--type=service", "--all", "--no-pager"]),
    ("timers.txt", ["systemctl", "list-timers", "--all", "--no-pager"]),
    ("mounts.txt", ["findmnt", "--json"]),
):
    (work / filename).write_bytes(subprocess.check_output(command))
for user in ("root", "hannes"):
    result = subprocess.run(["crontab", "-u", user, "-l"], capture_output=True)
    if result.returncode and b"no crontab" not in result.stderr.lower():
        raise RuntimeError("Cannot inventory crontab for " + user)
    (work / ("crontab-" + user)).write_bytes(result.stdout if not result.returncode else b"# no crontab\n")
    (work / ("crontab-" + user + ".diagnostic")).write_bytes(result.stderr)
PY
mkdir "$work/databases"
docker exec --user postgres mxserver-postgres-db-1 pg_dump -Fc -U postgres -d MXServer \
    > "$work/databases/mxserver.dump"
docker exec --user postgres mxserver-postgres-db-1 pg_dumpall -U postgres --roles-only \
    > "$work/databases/mxserver-roles.sql"
docker exec owncloud-staging-db-1 pg_dump -Fc -U owncloud_stage -d owncloud_stage \
    > "$work/databases/owncloud.dump"
docker exec owncloud-staging-db-1 pg_dumpall -U owncloud_stage --roles-only \
    > "$work/databases/owncloud-roles.sql"
for database in camDoor camHinten camPi3; do
    docker exec camera-postgres-db-1 sh -c \
        'export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_dump -w -U postgres -Fc -d "$1"' \
        sh "$database" > "$work/databases/$database.dump"
done
docker exec camera-postgres-db-1 sh -c \
    'export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_dumpall -w -U postgres --roles-only' \
    > "$work/databases/camera-roles.sql"
for dump in "$work"/databases/*.dump; do
    pg_restore -f /dev/null "$dump"
done
docker exec apache-db-1 sh -c \
    'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysqldump -uroot --all-databases --single-transaction --routines --events --triggers --hex-blob --no-tablespaces' \
    | gzip > "$work/databases/apache-mysql.sql.gz"
MYSQL_PWD="$native_mysql_password" mysqldump "${native_mysql_options[@]}" \
    --all-databases --single-transaction --routines --events --triggers --hex-blob \
    --no-tablespaces | gzip > "$work/databases/native-mysql.sql.gz"
unset native_mysql_password
gzip -t "$work/databases/apache-mysql.sql.gz" "$work/databases/native-mysql.sql.gz"
(cd "$work/databases" && sha256sum ./*.dump ./*.sql ./*.gz > SHA256SUMS)

# Only configuration and code go into this archive, never active database files.
python3 - "$work/config-paths" <<'PY'
from pathlib import Path
import sys

paths = [
    "/etc/apache2", "/etc/php", "/etc/letsencrypt", "/etc/mysql", "/etc/postfix",
    "/etc/aliases", "/etc/mailname", "/etc/mosquitto", "/etc/cron.d", "/etc/crontab",
    "/etc/systemd/system", "/etc/hosts", "/etc/fstab", "/etc/crypttab",
    "/usr/local/sbin/owncloud-mirror-sync", "/usr/local/sbin/mxserver-mirror-sync",
    "/usr/local/sbin/run-sync-with-email", "/usr/local/sbin/mxserver-postgres-wait",
    "/home/hannes/bin", "/home/hannes/.pgpass", "/daten/owncloud/production.env",
    "/home/hannes/git/dockerApache/apache/.env", "/home/hannes/git/dockerApache/nextcloud/.env",
    "/home/hannes/git/dockerApache/camera-postgres/.env",
    "/home/hannes/git/dockerApache/mxserver-postgres/.env",
    "/home/hannes/server/mxserver-postgres/tls", "/srv/gitlab/config",
    "/srv/gitlab-runner/config",
]
Path(sys.argv[1]).write_bytes(b"".join(
    str(Path(path).relative_to("/")).encode() + b"\0"
    for path in paths if Path(path).exists()))
PY
tar --acls --xattrs --numeric-owner --null -C / -T "$work/config-paths" \
    -czf "$work/host-configuration.tar.gz"
tar -tzf "$work/host-configuration.tar.gz" >/dev/null
log "Transferring inventory and logical snapshots..."
"${rsync_base[@]}" "$work/" "hannes@beelink:$target/metadata/"

log "Creating official GitLab application backup (may take a while)..."
gitlab_backup="beelink_seed_$(date -u +%Y%m%dT%H%M%SZ)"
docker exec gitlab gitlab-backup create "BACKUP=$gitlab_backup"
gitlab_archive="/srv/gitlab/data/backups/${gitlab_backup}_gitlab_backup.tar"
[[ -s "$gitlab_archive" ]] || fail "Expected GitLab backup is missing: $gitlab_archive"
tar -tf "$gitlab_archive" >/dev/null
sha256sum "$gitlab_archive" | awk '{print $1}' > "$work/gitlab-backup.sha256"
"${ssh[@]}" "mkdir -p '$target/gitlab'"
"${rsync_base[@]}" "$gitlab_archive" "hannes@beelink:$target/gitlab/"
"${rsync_base[@]}" "$work/gitlab-backup.sha256" "hannes@beelink:$target/metadata/"
"${ssh[@]}" "set -eu; cd '$target/gitlab'; test \"\$(sha256sum '${gitlab_backup}_gitlab_backup.tar' | awk '{print \$1}')\" = \"\$(cat '$target/metadata/gitlab-backup.sha256')\""

log "Seeding live files. This is NOT a consistent final copy and must not serve production."
set +e
"${rsync_base[@]}" --relative --recursive --from0 --files-from="$work/data-paths" \
    / "hannes@beelink:$target/files/"
result=$?
set -e
if [[ "$result" == 24 ]]; then
    log "WARNING: Live files vanished during seeding. A quiesced final reconciliation is required."
elif [[ "$result" != 0 ]]; then
    fail "File seeding failed with rsync exit $result."
fi
else
    log "Finishing existing seed; reusing snapshots and live files without repeating their transfer."
fi
log "Seeding repository code, without data trees, local caches or Git metadata..."
runuser -u hannes -- git -C "$root" ls-files --recurse-submodules -z > "$work/repo-paths"
printf '%s\0' monitoring/seed-latitude-to-beelink.sh >> "$work/repo-paths"
for path in camera-postgres/Dockerfile camera-postgres/docker-compose.yml camera-postgres/pg_hba.conf \
    mxserver-postgres/docker-compose.yml mxserver-postgres/pg_hba.conf; do
    [[ -f "$root/$path" ]] || fail "Missing stack definition: $path"
    printf '%s\0' "$path" >> "$work/repo-paths"
done
"${rsync_base[@]}" --from0 --files-from="$work/repo-paths" "$root/" \
    "hannes@beelink:$target/repository/"
"${ssh[@]}" "set -eu
test \"\$(findmnt -rn -M /daten -t ext4 -o UUID)\" = '$expected_uuid'
cd '$target/metadata/databases'
sha256sum -c SHA256SUMS
umask 077
printf '%s\n' 'Seed only: live files may differ; no production cutover or GitLab restore performed.' > '$target/seed-complete'
"
log "Initial seed complete at beelink:$target. Source and destination services and schedules unchanged."
log "GitLab application backup transferred; an isolated restore is still required. Active database files were not copied."
log "Snapshots, key material and configuration are private. Preserve $work until final cutover."
