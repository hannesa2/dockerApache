#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C HOME=/root
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 077

fail() { echo "ERROR: $*" >&2; exit 1; }
[[ $# == 1 && "$1" == --backup ]] || fail "Usage: sudo bash owncloud/backintime-beelink.sh --backup"
[[ "$(id -u)" == 0 && "$(hostname -s)" == beelink ]] || fail "Run as root on beelink."

for cmd in backintime docker findmnt flock curl python3 sha256sum systemctl cmp; do
    command -v "$cmd" >/dev/null || fail "Missing command: $cmd"
done

data=/daten/beelink-runtime/rootfs/daten/owncloud/data
volume=/daten/beelink-runtime/apps/owncloud/volume
stage=/daten/beelink-backups/backintime-owncloud
destination=/datenBackup/owncloud-root
web=beelink-apps-owncloud-app-1
db=beelink-databases-owncloud-1
lockdir=/run/lock/owncloud-mirror-sync.lock
stopped=0
maintenance=0
locked=0
config=""

healthy() {
    curl --noproxy '*' -fsS --max-time 10 \
        --resolve www.mxtracks.info:443:127.0.0.1 \
        https://www.mxtracks.info/owncloud/status.php |
        python3 -c 'import json,sys; s=json.load(sys.stdin); assert s.get("installed") and not s.get("maintenance") and not s.get("needsDbUpgrade")'
}

cleanup() {
    local status=$? attempt
    trap - EXIT INT TERM
    if (( stopped )); then
        if ! docker start "$web_id"; then
            echo "ERROR: Could not restart ownCloud; manual recovery required." >&2
            status=1
        else
            for attempt in {1..60}; do
                docker exec "$web_id" test -f /var/www/owncloud/occ && break
                sleep 2
            done
        fi
    fi
    if (( maintenance )); then
        if ! docker exec --user www-data "$web_id" occ maintenance:mode --off; then
            echo "ERROR: Could not disable ownCloud maintenance mode; manual recovery required." >&2
            status=1
        else
            for attempt in {1..60}; do
                healthy && break
                sleep 2
            done
            if (( attempt == 60 )) && ! healthy; then
                echo "ERROR: ownCloud did not return healthy; manual recovery required." >&2
                status=1
            fi
        fi
    fi
    if (( locked )); then
        rmdir "$lockdir" || status=1
    fi
    if [[ -n "$config" ]]; then
        rm -f -- "$config" || status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

exec 9>/run/lock/beelink-staging-control.lock
flock -n 9 || fail "Another production lifecycle operation is running."
exec 8>/run/lock/owncloud-backintime-online.lock
flock -n 8 || fail "An online ownCloud backup is already running."
if systemctl is-active --quiet owncloud-backintime-online.service; then
    fail "The online ownCloud backup service is still active."
fi
systemctl is-active --quiet beelink-production.service || fail "Production service is not active."
ls -ld /daten/. /datenBackup/. >/dev/null
[[ "$(findmnt -rn -M /daten -t ext4 -o UUID)" == c0c42461-ae4c-4edb-a993-bba94a05ed6c ]] ||
    fail "Expected beelink T7 is not mounted."
[[ "$(findmnt -rn -M /datenBackup -t ext4 -o UUID)" == fe91ecb5-c1a4-4e6c-9edf-f3c9c75cffe1 ]] ||
    fail "Expected encrypted backup disk is not mounted."
[[ -f "$data/.ocdata" && -d "$volume/apps" && -f "$volume/config/config.php" ]] ||
    fail "ownCloud data, apps or configuration missing."
mkdir "$lockdir" || fail "ownCloud mirror sync is running or its lock needs inspection."
locked=1

web_id="$(docker inspect -f '{{.Id}}' "$web")"
db_id="$(docker inspect -f '{{.Id}}' "$db")"
[[ "$(docker inspect -f '{{.State.Running}}' "$web_id")" == true &&
   "$(docker inspect -f '{{.State.Running}}' "$db_id")" == true ]] ||
    fail "ownCloud or PostgreSQL container is not running."
[[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$web_id")" == beelink-apps ]] ||
    fail "Unexpected ownCloud container."
[[ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/mnt/data/files"}}{{.Source}}{{end}}{{end}}' "$web_id")" == "$data" &&
   "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/mnt/data"}}{{.Source}}{{end}}{{end}}' "$web_id")" == "$volume" ]] ||
    fail "Unexpected ownCloud mounts."
docker exec "$db_id" pg_isready -U beelink_restore -d owncloud_stage
healthy || fail "ownCloud is not healthy or is already in maintenance mode."
docker exec --user www-data "$web_id" occ status

for dir in "$stage" "$destination"; do
    [[ ! -L "$dir" ]] || fail "Refusing symlink: $dir"
    if [[ ! -e "$dir" ]]; then
        install -d -o root -g root -m 0700 "$dir"
    fi
    [[ -d "$dir" ]] || fail "Not a directory: $dir"
    [[ "$(stat -c '%u:%a' "$dir")" == 0:700 ]] || fail "Directory is not root-private: $dir"
done
config="$(mktemp /run/owncloud-backintime-config.XXXXXXXX)"
cat > "$config" <<EOF
profiles.version=1
profile1.name=ownCloud
profile1.snapshots.mode=local
profile1.snapshots.path=$destination
profile1.snapshots.path.host=beelink
profile1.snapshots.path.user=root
profile1.snapshots.path.profile=1
profile1.snapshots.include.size=4
profile1.snapshots.include.1.type=0
profile1.snapshots.include.1.value=$data
profile1.snapshots.include.2.type=0
profile1.snapshots.include.2.value=$volume/apps
profile1.snapshots.include.3.type=0
profile1.snapshots.include.3.value=$volume/config
profile1.snapshots.include.4.type=0
profile1.snapshots.include.4.value=$stage
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

baseline="$(backintime --config "$config" --profile 1 --quiet show --last --path)"
[[ "$baseline" == "$destination"/backintime/beelink/root/1/* &&
   ! -L "$baseline" && -d "$baseline" && ! -e "$baseline/failed" &&
   -f "$baseline/backup$data/.ocdata" &&
   -f "$baseline/backup$volume/config/config.php" &&
   -d "$baseline/backup$volume/apps" ]] ||
    fail "A usable preliminary snapshot is required; ownCloud was not stopped."
[[ ! -e "$destination/backintime/beelink/root/1/new_snapshot" ]] ||
    fail "An incomplete snapshot exists; inspect it before the final pass."
echo "Using preliminary snapshot: $baseline"

echo "Enabling maintenance mode and stopping ownCloud for the entire snapshot."
maintenance=1
docker exec --user www-data "$web_id" occ maintenance:mode --on
stopped=1
docker stop --timeout 60 "$web_id"
[[ "$(docker inspect -f '{{.State.Running}}' "$web_id")" == false ]] ||
    fail "ownCloud did not stop."

docker exec "$db_id" pg_dump -w -U beelink_restore -d owncloud_stage \
    --format=custom --no-owner --no-privileges > "$stage/owncloud.dump.tmp"
[[ -s "$stage/owncloud.dump.tmp" ]] || fail "Database dump is empty."
docker exec -i "$db_id" pg_restore --list < "$stage/owncloud.dump.tmp" >/dev/null
docker exec -i "$db_id" pg_restore -f /dev/null < "$stage/owncloud.dump.tmp"
mv -f "$stage/owncloud.dump.tmp" "$stage/owncloud.dump"
docker inspect "$web_id" "$db_id" > "$stage/containers.json"
docker image inspect "$(docker inspect -f '{{.Image}}' "$web_id")" \
    "$(docker inspect -f '{{.Image}}' "$db_id")" > "$stage/images.json"
date '+%Y-%m-%dT%H:%M:%S.%N%z' > "$stage/captured-at"
sha256sum "$stage/owncloud.dump" > "$stage/owncloud.dump.sha256"

backintime --config "$config" --profile 1 backup
snapshot="$(backintime --config "$config" --profile 1 --quiet show --last --path)"
[[ "$snapshot" == "$destination"/backintime/beelink/root/1/* &&
   "$snapshot" != "$baseline" &&
   ! -e "$snapshot/failed" &&
   -f "$snapshot/backup$data/.ocdata" &&
   -f "$snapshot/backup$volume/config/config.php" &&
   -d "$snapshot/backup$volume/apps" &&
   -f "$snapshot/backup$stage/owncloud.dump" ]] ||
    fail "Snapshot is missing required ownCloud data."
cmp "$stage/owncloud.dump" "$snapshot/backup$stage/owncloud.dump" ||
    fail "Snapshot database dump differs from the verified dump."
cmp "$stage/captured-at" "$snapshot/backup$stage/captured-at" ||
    fail "Back In Time did not produce the current snapshot."
echo "Verified snapshot: $snapshot"
echo "Restore requires matching application images; snapshot configuration has maintenance enabled."
