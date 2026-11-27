#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C HOME=/root
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 077

fail() { echo "ERROR: $*" >&2; exit 1; }
[[ $# == 1 && "$1" == --backup && "$(id -u)" == 0 && "$(hostname -s)" == beelink ]] ||
    fail "Run as root on beelink: $0 --backup"
for cmd in backintime docker findmnt flock gzip sha256sum python3 curl cmp; do
    command -v "$cmd" >/dev/null || fail "Missing command: $cmd"
done

site=/daten/beelink-runtime/wordpress/www/dev.mxtracks
stage=/daten/beelink-backups/backintime-dev-wordpress
destination=/datenBackup/dev-wordpress-root
history="$destination/backintime/beelink/root/1"
web=beelink-wordpress-apache-1
db=beelink-databases-native-mysql-1
config=""
temp=""
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    [[ -z "$temp" ]] || rm -f -- "$temp"
    [[ -z "$config" ]] || rm -f -- "$config"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
exec 9>/run/lock/owncloud-backintime-online.lock
flock -n 9 || fail "Another Back In Time operation is running."
[[ "$(findmnt -rn -M /daten -t ext4 -o UUID)" == c0c42461-ae4c-4edb-a993-bba94a05ed6c &&
   "$(findmnt -rn -M /datenBackup -t ext4 -o UUID)" == fe91ecb5-c1a4-4e6c-9edf-f3c9c75cffe1 ]] ||
    fail "Expected data and backup filesystems are not mounted."
[[ ! -L "$site" && -f "$site/wordpress/wp-config.php" ]] || fail "Expected dev site missing."
[[ "$(docker inspect -f '{{.State.Running}}' "$web")" == true &&
   "$(docker inspect -f '{{.State.Running}}' "$db")" == true &&
   "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/www/html"}}{{.Source}}{{end}}{{end}}' "$web")" == /daten/beelink-runtime/wordpress/www ]] ||
    fail "Unexpected WordPress containers or document root."
curl --noproxy '*' -fsS --max-time 10 --resolve dev.mxtracks.info:443:127.0.0.1 \
    https://dev.mxtracks.info/wordpress/ >/dev/null || fail "Dev WordPress is unavailable."
for dir in "$stage" "$destination"; do
    [[ ! -L "$dir" ]] || fail "Refusing symlink: $dir"
    [[ -e "$dir" ]] || install -d -o root -g root -m 0700 "$dir"
    [[ -d "$dir" && "$(stat -c '%u:%a' "$dir")" == 0:700 ]] ||
        fail "Directory is not root-private: $dir"
done
python3 - "$site" "$destination" <<'PY'
import shutil
import os
import sys
def fail_walk(error):
    raise error
size = 0
for root, directories, files in os.walk(sys.argv[1], followlinks=False, onerror=fail_walk):
    for name in files:
        path = os.path.join(root, name)
        if not os.path.islink(path):
            size += os.stat(path, follow_symlinks=False).st_size
free = shutil.disk_usage(sys.argv[2]).free
needed = 100 * 1024**3 + size * 3 // 2 + 1024**3
print(f"dev WordPress: {size / 1024**3:.2f} GiB; backup free: {free / 1024**3:.1f} GiB.")
if free < needed:
    raise SystemExit("ERROR: Insufficient backup space with 100 GiB safety reserve.")
PY
[[ ! -e "$history/new_snapshot" && ! -L "$history/new_snapshot" ]] ||
    fail "Incomplete dev snapshot exists; inspect before retrying."

engines="$(docker exec "$db" mysql --defaults-extra-file=/run/restore-client.cnf \
    --batch --skip-column-names -e \
    "SELECT COUNT(*), SUM(engine <> 'InnoDB') FROM information_schema.tables WHERE table_schema='wordpress' AND table_type='BASE TABLE'")"
[[ "$engines" =~ ^[0-9]+[[:space:]]+0$ && "${engines%%[[:space:]]*}" != 0 ]] ||
    fail "WordPress database is missing or has non-InnoDB tables."
temp="$(mktemp "$stage/.wordpress.sql.gz.XXXXXXXX")"
docker exec "$db" mysqldump --defaults-extra-file=/run/restore-client.cnf \
    --single-transaction --quick --no-tablespaces --hex-blob wordpress | gzip > "$temp"
[[ -s "$temp" ]] && gzip -t "$temp" || fail "MySQL dump is invalid."
mv -f -- "$temp" "$stage/wordpress.sql.gz"
temp=""
date '+%Y-%m-%dT%H:%M:%S.%N%z' > "$stage/captured-at"
sha256sum "$stage/wordpress.sql.gz" > "$stage/wordpress.sql.gz.sha256"

config="$(mktemp /run/dev-wordpress-backintime.XXXXXXXX)"
cat > "$config" <<EOF
profiles.version=1
profile1.name=dev-WordPress-online
profile1.snapshots.mode=local
profile1.snapshots.path=$destination
profile1.snapshots.path.host=beelink
profile1.snapshots.path.user=root
profile1.snapshots.path.profile=1
profile1.snapshots.include.size=2
profile1.snapshots.include.1.type=0
profile1.snapshots.include.1.value=$site
profile1.snapshots.include.2.type=0
profile1.snapshots.include.2.value=$stage
profile1.snapshots.exclude.size=0
profile1.snapshots.continue_on_errors=false
profile1.snapshots.automatic_backup_mode=0
profile1.snapshots.remove_old_snapshots.enabled=false
profile1.snapshots.smart_remove=false
profile1.snapshots.min_free_space.enabled=false
profile1.snapshots.min_free_inodes.enabled=false
profile1.snapshots.take_snapshot_regardless_of_changes=true
profile1.snapshots.preserve_acl=true
profile1.snapshots.preserve_xattr=true
EOF
backintime --config "$config" --profile 1 check-config --no-crontab
previous=""
if [[ -L "$history/last_snapshot" ]]; then
    previous="$(backintime --config "$config" --profile 1 --quiet show --last --path)"
fi
echo "Backing up dev WordPress online; database and files may differ in time."
backintime --config "$config" --profile 1 backup
snapshot="$(backintime --config "$config" --profile 1 --quiet show --last --path)"
[[ "$snapshot" == "$history/"* && "$snapshot" != "$previous" && ! -L "$snapshot" &&
   -d "$snapshot" && ! -e "$snapshot/failed" &&
   -f "$snapshot/backup$site/wordpress/wp-config.php" &&
   -f "$snapshot/backup$stage/wordpress.sql.gz" ]] ||
    fail "Completed dev snapshot is missing required content."
cmp "$stage/wordpress.sql.gz" "$snapshot/backup$stage/wordpress.sql.gz" ||
    fail "Snapshot MySQL dump differs from captured dump."
cmp "$stage/captured-at" "$snapshot/backup$stage/captured-at" ||
    fail "Snapshot does not contain this run's capture."
gzip -t "$snapshot/backup$stage/wordpress.sql.gz"
echo "Verified online dev snapshot: $snapshot"
echo "WARNING: Online WordPress files and database are not a point-in-time pair."
