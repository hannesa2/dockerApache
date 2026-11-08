#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="/media/hannes/T1/owncloud-staging"
TARGET_ROOT="/daten/owncloud"
SOURCE_ENV="$SOURCE_ROOT/staging.env"
TARGET_ENV="$TARGET_ROOT/production.env"
SOURCE_COMPOSE="$SCRIPT_DIR/../owncloud-staging/compose.yml"
TARGET_COMPOSE="$SCRIPT_DIR/latitude-compose.yml"
LOCK_DIR="/run/lock/owncloud-mirror-sync.lock"
NATIVE_CONFIG="/srv/www/vhosts/mxdocs/owncloud/config/config.php"
CRON_FILE="/etc/cron.d/owncloud-mirror-sync"
DOCKER_DROP_IN="/etc/systemd/system/docker.service.d/owncloud-data.conf"
MAINTENANCE_CHANGED=0
RECREATION_STARTED=0
RELEASED=0
LOCK_ACQUIRED=0
BOOT_GUARD_INSTALLED=0
SYNC_UPDATED=0

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }
source_compose() {
    docker compose -p owncloud-staging --env-file "$SOURCE_ENV" -f "$SOURCE_COMPOSE" "$@"
}
target_compose() {
    docker compose -p owncloud-staging --env-file "$TARGET_ENV" -f "$TARGET_COMPOSE" "$@"
}
occ() {
    docker exec --user www-data owncloud-staging-owncloud-1 php /var/www/owncloud/occ "$@"
}
check_status() {
    local response
    response="$(curl -fsS --max-time 15 \
        --resolve www.mxtracks.info:443:127.0.0.1 "$1")" ||
        fail "Could not fetch ownCloud status from $1."
    python3 -c '
import json, sys
s = json.load(sys.stdin)
assert s.get("installed") and s.get("version") == "10.16.7.0", "Unexpected ownCloud version/install state"
assert not s.get("needsDbUpgrade"), "Database needs an upgrade"
assert s.get("maintenance") is (sys.argv[1] == "on"), "Unexpected maintenance state"
' "$2" <<< "$response"
}
wait_ready() {
    local attempt
    for attempt in {1..120}; do
        if docker exec owncloud-staging-owncloud-1 test -f /var/www/owncloud/config/config.php &&
            curl -fsS --max-time 5 http://127.0.0.1:8093/status.php >/dev/null; then
            return 0
        fi
        sleep 5
    done
    return 1
}
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if (( status != 0 && ! RELEASED )); then
        if (( BOOT_GUARD_INSTALLED )); then
            rm -- "$DOCKER_DROP_IN" || status=1
            systemctl daemon-reload || status=1
        fi
        if (( SYNC_UPDATED )); then
            install -o root -g root -m 0750 "$BACKUP/installed-mirror-sync.sh" \
                /usr/local/sbin/owncloud-mirror-sync || status=1
        fi
    fi
    if (( status != 0 && RELEASED )); then
        log "ERROR: Internal production may already accept writes. No automatic rollback; inspect it before proceeding." >&2
    elif (( status != 0 && RECREATION_STARTED )); then
        log "Restoring the T1 production containers; the T1 database and files were retained."
        if target_compose stop && source_compose up -d --pull never --force-recreate &&
            wait_ready && occ maintenance:mode --off &&
            check_status http://127.0.0.1:8093/status.php off; then
            log "T1 production restored. Internal partial copies remain for inspection."
        else
            log "ERROR: Automatic rollback failed. Keep T1 attached and inspect container logs." >&2
        fi
    elif (( status != 0 && MAINTENANCE_CHANGED )); then
        if ! occ maintenance:mode --off; then
            log "ERROR: Could not restore T1 production from maintenance." >&2
        fi
    fi
    if (( LOCK_ACQUIRED )); then
        rmdir "$LOCK_DIR" || status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

(( EUID == 0 )) || fail "Run directly on latitude with sudo."
MODE="${1:---preflight}"
[[ "$MODE" == "--preflight" || "$MODE" == "--migrate" ]] ||
    fail "Usage: $0 [--preflight|--migrate]"
(( $# <= 1 )) || fail "Usage: $0 [--preflight|--migrate]"
for tool in docker rsync findmnt mountpoint python3 curl php7.4 systemctl; do
    command -v "$tool" >/dev/null || fail "Missing tool: $tool"
done
mountpoint -q /daten || fail "/daten is not mounted."
mountpoint -q /media/hannes/T1 || fail "T1 must remain mounted until migration finishes."
[[ "$(findmnt -n -o FSTYPE -T "$TARGET_ROOT")" == "ext4" ]] ||
    fail "Expected the internal ext4 data disk."
findmnt --fstab --target /daten >/dev/null ||
    fail "/daten must have a persistent mount definition before migration."
[[ ! -e "$DOCKER_DROP_IN" ]] || fail "Docker data-mount guard already exists; inspect it first."
[[ -f "$SOURCE_ENV" && -f "$SOURCE_COMPOSE" && -f "$TARGET_COMPOSE" &&
    -f "$SOURCE_ROOT/files/.ocdata" && -f "$TARGET_ROOT/data/.ocdata" &&
    -f "$SOURCE_ROOT/config/config.php" && -f "$SOURCE_ROOT/database/PG_VERSION" ]] ||
    fail "Source production or existing internal ownCloud data is incomplete."
[[ ! -e "$TARGET_ENV" ]] || fail "production.env already exists; inspect a previous migration before retrying."
for directory in config apps database; do
    [[ ! -e "$TARGET_ROOT/$directory" ]] ||
        fail "Target $directory already exists; inspect it before retrying."
done
for path in "$SOURCE_ROOT" "$TARGET_ROOT" "$SOURCE_ROOT/files" "$TARGET_ROOT/data"; do
    [[ "$(readlink -f "$path")" == "$path" ]] ||
        fail "Unexpected symlink in storage path $path."
done
[[ "$(cat "$SOURCE_ROOT/database/PG_VERSION")" == "16" ]] || fail "Expected PostgreSQL 16."
[[ -f "$NATIVE_CONFIG" ]] || fail "Cannot verify that the old native instance is in maintenance."
php7.4 -r 'include $argv[1]; exit(($CONFIG["maintenance"] ?? false) === true ? 0 : 1);' \
    "$NATIVE_CONFIG" || fail "The old native ownCloud must remain in maintenance; do not enable it."
if [[ -f "$CRON_FILE" ]] && grep -Eq '^[[:space:]]*[^#[:space:]].*owncloud-mirror-sync' "$CRON_FILE"; then
    fail "Disable the scheduled mirror sync before migrating; do not overwrite the mac2016 source."
fi
mkdir "$LOCK_DIR" || fail "Another ownCloud sync or migration holds $LOCK_DIR."
LOCK_ACQUIRED=1
source_compose config --quiet
password="$(sed -n 's/^OWNCLOUD_STAGE_DB_PASSWORD=//p' "$SOURCE_ENV")"
[[ "$password" =~ ^[0-9a-f]{64}$ ]] || fail "Unexpected staging password format."
OWNCLOUD_DB_PASSWORD="$password" OWNCLOUD_DATA_ROOT="$TARGET_ROOT" \
    docker compose -p owncloud-staging -f "$TARGET_COMPOSE" config --quiet
unset password
for service in owncloud db redis; do
    container_id="$(source_compose ps -q "$service")"
    [[ -n "$container_id" &&
        "$(docker inspect -f '{{.Name}}' "$container_id")" == "/owncloud-staging-$service-1" ]] ||
        fail "Expected running production container for $service."
done
for service in db redis; do
    [[ "$(docker inspect -f '{{.State.Health.Status}}' "owncloud-staging-$service-1")" == "healthy" ]] ||
        fail "Production $service is not healthy."
done
for mapping in 'owncloud /mnt/data/files files' 'owncloud /mnt/data/config config' \
    'owncloud /mnt/data/apps apps' 'db /var/lib/postgresql/data database'; do
    read -r service destination directory <<< "$mapping"
    actual="$(docker inspect -f '{{range .Mounts}}{{.Destination}} {{.Source}}{{println}}{{end}}' \
        "owncloud-staging-$service-1" | awk -v destination="$destination" '$1 == destination {print $2}')"
    [[ "$actual" == "$SOURCE_ROOT/$directory" ]] || fail "Unexpected $service storage at $destination."
done
check_status http://127.0.0.1:8093/status.php off
check_status https://www.mxtracks.info/owncloud/status.php off

log "Read-only comparison of T1 production and existing /daten/owncloud/data (may take several minutes)."
comparison="$(rsync -aHAXn --numeric-ids --delete --stats \
    "$SOURCE_ROOT/files/" "$TARGET_ROOT/data/")"
printf '%s\n' "$comparison"
transfer_bytes="$(printf '%s\n' "$comparison" |
    awk '/^Total transferred file size:/ {gsub(/,/, "", $5); print $5}')"
[[ "$transfer_bytes" =~ ^[0-9]+$ ]] || fail "Could not determine transfer size."
database_bytes="$(du -sb "$SOURCE_ROOT/database" | awk '{print $1}')"
available_bytes="$(df -B1 --output=avail "$TARGET_ROOT" | tail -1 | tr -d ' ')"
required_bytes=$(( 2 * transfer_bytes + 3 * database_bytes + 10 * 1024 * 1024 * 1024 ))
(( available_bytes >= required_bytes )) ||
    fail "Insufficient free space for changed files, database, backup, and 10 GiB reserve."
log "Storage preflight passed. No user data has been modified."
[[ "$MODE" == "--migrate" ]] || exit 0
[[ -f "$SCRIPT_DIR/sync-latitude.sh" ]] || fail "Updated mirror sync script is missing."

TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
BACKUP="$TARGET_ROOT/backups/pre-internal-$TIMESTAMP"
mkdir -p "$BACKUP"
chmod 700 "$TARGET_ROOT/backups" "$BACKUP"
LOG="$BACKUP/migration.log"
exec > >(tee -a "$LOG") 2>&1
log "Synchronizing existing internal data while production remains online."
rsync -aHAX --numeric-ids --delete --stats "$SOURCE_ROOT/files/" "$TARGET_ROOT/data/"

MAINTENANCE_CHANGED=1
occ maintenance:mode --on
check_status http://127.0.0.1:8093/status.php on
log "Final file sync and consistent PostgreSQL backup under maintenance."
rsync -aHAX --numeric-ids --delete --stats "$SOURCE_ROOT/files/" "$TARGET_ROOT/data/"
docker exec owncloud-staging-db-1 pg_dump -Fc -U owncloud_stage owncloud_stage > "$BACKUP/database.dump"
docker exec -i owncloud-staging-db-1 pg_restore --list < "$BACKUP/database.dump" >/dev/null
install -m 0600 "$SOURCE_ENV" "$BACKUP/staging.env"
tar -cpf "$BACKUP/config-apps.tar" -C "$SOURCE_ROOT" config apps
for directory in config apps; do
    mkdir "$TARGET_ROOT/$directory"
    rsync -aHAX --numeric-ids "$SOURCE_ROOT/$directory/" "$TARGET_ROOT/$directory/"
done
password="$(sed -n 's/^OWNCLOUD_STAGE_DB_PASSWORD=//p' "$SOURCE_ENV")"
[[ "$password" =~ ^[0-9a-f]{64}$ ]] || fail "Unexpected staging password format."
printf 'OWNCLOUD_DATA_ROOT=%s\nOWNCLOUD_DB_PASSWORD=%s\n' "$TARGET_ROOT" "$password" > "$TARGET_ENV"
chmod 600 "$TARGET_ENV"
unset password
target_compose config --quiet

log "Stopping T1 containers and copying the cleanly stopped PostgreSQL cluster."
RECREATION_STARTED=1
source_compose stop --timeout 120
[[ "$(docker inspect -f '{{.State.ExitCode}}' owncloud-staging-db-1)" == "0" ]] ||
    fail "PostgreSQL did not stop cleanly; refusing to copy its cluster."
mkdir "$TARGET_ROOT/database"
rsync -aHAX --numeric-ids "$SOURCE_ROOT/database/" "$TARGET_ROOT/database/"
log "Recreating the same Compose project with internal bind mounts."
target_compose up -d --pull never --force-recreate
wait_ready || fail "Internal ownCloud did not start within ten minutes."
check_status http://127.0.0.1:8093/status.php on
check_status https://www.mxtracks.info/owncloud/status.php on
for service in owncloud db redis; do
    if docker inspect -f '{{range .Mounts}}{{.Source}}{{println}}{{end}}' \
        "owncloud-staging-$service-1" | grep -q '^/media/hannes/T1'; then
        fail "A production container still mounts T1."
    fi
done
if [[ -f /usr/local/sbin/owncloud-mirror-sync ]]; then
    install -m 0600 /usr/local/sbin/owncloud-mirror-sync "$BACKUP/installed-mirror-sync.sh"
    SYNC_UPDATED=1
    install -o root -g root -m 0750 "$SCRIPT_DIR/sync-latitude.sh" /usr/local/sbin/owncloud-mirror-sync
fi
log "Requiring the internal /daten mount before Docker starts on future boots."
mkdir -p "$(dirname "$DOCKER_DROP_IN")"
BOOT_GUARD_INSTALLED=1
printf '%s\n' '[Unit]' 'RequiresMountsFor=/daten' '' '[Service]' \
    'ExecStartPre=/usr/bin/mountpoint -q /daten' > "$DOCKER_DROP_IN"
chmod 644 "$DOCKER_DROP_IN"
systemctl daemon-reload
RELEASED=1
occ maintenance:mode --off
check_status http://127.0.0.1:8093/status.php off
check_status https://www.mxtracks.info/owncloud/status.php off
log "Production now uses /daten/owncloud. T1 is retained as an offline rollback copy."
log "Do not run staging upgrade/clone/cutover scripts or re-enable mirror sync without reviewing mac2016's current data."
log "Migration log and consistent backup: $BACKUP"
