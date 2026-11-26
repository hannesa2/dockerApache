#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C HOME=/root
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 077

fail() { echo "ERROR: $*" >&2; exit 1; }
[[ $# == 1 && ( "$1" == --backup || "$1" == --scheduled ) ]] ||
    fail "Usage: sudo bash owncloud/backintime-online-beelink.sh --backup|--scheduled"
scheduled=0
[[ "$1" != --scheduled ]] || scheduled=1
[[ "$(id -u)" == 0 && "$(hostname -s)" == beelink ]] || fail "Run as root on beelink."

data=/daten/beelink-runtime/rootfs/daten/owncloud/data
volume=/daten/beelink-runtime/apps/owncloud/volume
stage=/daten/beelink-backups/backintime-owncloud
destination=/datenBackup/owncloud-root
pending="$destination/backintime/beelink/root/1/new_snapshot"
web=beelink-apps-owncloud-app-1

exec 9>/run/lock/owncloud-backintime-online.lock
flock -n 9 || fail "An online ownCloud backup is already running."
systemctl is-active --quiet beelink-production.service || fail "Production service is not active."
if systemctl is-active --quiet owncloud-backintime.service; then
    fail "The offline ownCloud backup is already running."
fi
ls -ld /daten/. /datenBackup/. >/dev/null
[[ "$(findmnt -rn -M /daten -t ext4 -o UUID)" == c0c42461-ae4c-4edb-a993-bba94a05ed6c ]] ||
    fail "Expected beelink T7 is not mounted."
[[ "$(findmnt -rn -M /datenBackup -t ext4 -o UUID)" == fe91ecb5-c1a4-4e6c-9edf-f3c9c75cffe1 ]] ||
    fail "Expected encrypted backup disk is not mounted."
[[ ! -L "$destination" && -d "$destination" && "$(stat -c '%u:%a' "$destination")" == 0:700 ]] ||
    fail "Backup directory is not root-private."
if (( ! scheduled )) || [[ -e "$pending" || -L "$pending" ]]; then
    [[ -d "$pending/backup" && ! -L "$pending" && -f "$pending/save_to_continue" ]] ||
        fail "Expected resumable incomplete snapshot is missing; refusing to replace it."
fi
[[ -f "$data/.ocdata" && -d "$volume/apps" && -f "$volume/config/config.php" &&
   -d "$stage" && ! -L "$stage" ]] || fail "Expected ownCloud source directories are unavailable."
[[ "$(docker inspect -f '{{.State.Running}}' "$web")" == true ]] ||
    fail "ownCloud container is not running."
[[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$web")" == beelink-apps ]] ||
    fail "Unexpected ownCloud container."
[[ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/mnt/data/files"}}{{.Source}}{{end}}{{end}}' "$web")" == "$data" &&
   "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/mnt/data"}}{{.Source}}{{end}}{{end}}' "$web")" == "$volume" ]] ||
    fail "Unexpected ownCloud mounts."
curl --noproxy '*' -fsS --max-time 10 --resolve www.mxtracks.info:443:127.0.0.1 \
    https://www.mxtracks.info/owncloud/status.php |
    python3 -c 'import json,sys; s=json.load(sys.stdin); assert s.get("installed") and not s.get("maintenance") and not s.get("needsDbUpgrade")' ||
    fail "ownCloud is not healthy."

config="$(mktemp /run/owncloud-backintime-online.XXXXXXXX)"
trap 'rm -f -- "$config"' EXIT
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
profile1.snapshots.continue_on_errors=$([[ "$scheduled" == 1 ]] && echo false || echo true)
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
echo "Creating or continuing a preliminary snapshot while ownCloud remains online."
echo "WARNING: This is only a preliminary copy, not a consistent or restorable backup."
echo "Any existing database dump in this copy is stale."
backintime --config "$config" --profile 1 backup
echo "Online copy finished; a separate, consistent final backup is still required."
