#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/compose.yml"
STAGE_ROOT="${OWNCLOUD_STAGE_ROOT:-/media/hannes/T1/owncloud-staging}"
STAGE_MOUNT="${OWNCLOUD_STAGE_MOUNT:-/media/hannes/T1}"
STATE_ROOT="${OWNCLOUD_STAGE_STATE_ROOT:-$STAGE_ROOT}"
STAGE_ENV="$STATE_ROOT/staging.env"
BACKUP_DIR="$STATE_ROOT/backup"
MIRROR_SYNC_LOCK="/run/lock/owncloud-mirror-sync.lock"
LOCK_ACQUIRED=0
TEMP_SCRIPT=""

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

cleanup() {
    local status=$?
    if [[ -n "$TEMP_SCRIPT" ]]; then
        rm -f -- "$TEMP_SCRIPT"
    fi
    if (( LOCK_ACQUIRED )); then
        rmdir "$MIRROR_SYNC_LOCK"
    fi
    exit "$status"
}
trap cleanup EXIT

(( EUID == 0 )) || fail "Run as root: sudo $SCRIPT_DIR/upgrade-staging.sh"
for command_name in docker sed mountpoint findmnt stat; do
    command -v "$command_name" >/dev/null || fail "Required command is missing: $command_name"
done

[[ "$STAGE_ROOT" == "$STATE_ROOT" ]] ||
    fail "Data and state paths differ; inspect the Compose configuration before upgrading."
[[ -d "$STAGE_ROOT" && -f "$STAGE_ENV" ]] ||
    fail "Staging directory or staging.env is missing at $STAGE_ROOT."
[[ -s "$BACKUP_DIR/native-database.dump" ]] ||
    fail "Staging database dump is missing or empty."
[[ -f "$STAGE_ROOT/config/config.php" && -f "$STAGE_ROOT/files/.ocdata" ]] ||
    fail "Staging config or copied ownCloud data marker is missing."
[[ "$(stat -c '%a' "$STAGE_ENV")" == "600" ]] ||
    fail "Expected restricted permissions (0600) on $STAGE_ENV."
mountpoint -q "$STAGE_MOUNT" || fail "Target disk is not mounted at $STAGE_MOUNT."
[[ "$(findmnt -n -o FSTYPE --target "$STAGE_MOUNT")" == "ext4" ]] ||
    fail "Expected an ext4 filesystem mounted at $STAGE_MOUNT."
case "$STAGE_ROOT" in
    "$STAGE_MOUNT"/*) ;;
    *) fail "Staging path $STAGE_ROOT is outside target mount $STAGE_MOUNT." ;;
esac

SAVED_STAGE_ROOT="$(sed -n 's/^OWNCLOUD_STAGE_ROOT=//p' "$STAGE_ENV")"
SAVED_STATE_ROOT="$(sed -n 's/^OWNCLOUD_STAGE_STATE_ROOT=//p' "$STAGE_ENV")"
STAGE_DB_PASSWORD="$(sed -n 's/^OWNCLOUD_STAGE_DB_PASSWORD=//p' "$STAGE_ENV")"
[[ "$SAVED_STAGE_ROOT" == "$STAGE_ROOT" && "$SAVED_STATE_ROOT" == "$STATE_ROOT" &&
    "$STAGE_DB_PASSWORD" =~ ^[0-9a-f]{64}$ ]] ||
    fail "Saved staging environment does not match the T1 staging paths."
unset STAGE_DB_PASSWORD

mkdir -p "$STAGE_ROOT/apps/marketplace"
chown 33:0 "$STAGE_ROOT/apps/marketplace"
chmod 750 "$STAGE_ROOT/apps/marketplace"

if ! mkdir "$MIRROR_SYNC_LOCK" 2>/dev/null; then
    fail "The ownCloud mirror sync is running or its lock exists: $MIRROR_SYNC_LOCK"
fi
LOCK_ACQUIRED=1

compose() {
    docker compose --env-file "$STAGE_ENV" -f "$COMPOSE_FILE" "$@"
}

DB_CONTAINER_ID="$(compose ps -q db)"
[[ -n "$DB_CONTAINER_ID" ]] || fail "The staging PostgreSQL container does not exist."
[[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$DB_CONTAINER_ID")" == "owncloud-staging" ]] ||
    fail "The PostgreSQL container belongs to a different Compose project."
[[ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Source}}{{end}}{{end}}' "$DB_CONTAINER_ID")" == "$STAGE_ROOT/database" ]] ||
    fail "The PostgreSQL container is not using the expected T1 database directory."
[[ "$(docker inspect -f '{{.State.Health.Status}}' "$DB_CONTAINER_ID")" == "healthy" ]] ||
    fail "The staging PostgreSQL container is not healthy."
USER_TABLES="$(docker exec "$DB_CONTAINER_ID" psql -X -U owncloud_stage -d owncloud_stage -Atc \
    "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
(( USER_TABLES > 0 )) || fail "The restored staging database has no public tables."

APP_CONTAINER_ID="$(compose ps -q owncloud)"
if [[ -n "$APP_CONTAINER_ID" ]] && [[ "$(docker inspect -f '{{.State.Running}}' "$APP_CONTAINER_ID")" == "true" ]]; then
    log "Stopping the staging ownCloud web server before migration."
    compose stop owncloud
fi

log "Registering a writable staging app path for apps required by the database."
TEMP_SCRIPT="$(mktemp /run/owncloud-staging-app-path.XXXXXX.php)"
chmod 644 "$TEMP_SCRIPT"
cat > "$TEMP_SCRIPT" <<'PHP'
<?php
$configPath = "/mnt/data/config/config.php";
$CONFIG = [];
require $configPath;
$paths = $CONFIG["apps_paths"] ?? [];
foreach ($paths as $path) {
    if (($path["path"] ?? "") === "/mnt/data/apps/marketplace") {
        exit(0);
    }
}
$paths[] = [
    "path" => "/mnt/data/apps/marketplace",
    "url" => "/apps-external/marketplace",
    "writable" => true,
];
$CONFIG["apps_paths"] = $paths;
$tmp = $configPath . ".tmp";
if (file_put_contents($tmp, "<?php\n\$CONFIG = " . var_export($CONFIG, true) . ";\n", LOCK_EX) === false) {
    fwrite(STDERR, "Could not update staging app paths.\n");
    exit(1);
}
chmod($tmp, 0640);
if (!rename($tmp, $configPath)) {
    unlink($tmp);
    fwrite(STDERR, "Could not install updated staging app paths.\n");
    exit(1);
}
PHP
compose run --rm --no-deps --user www-data \
    -v "$TEMP_SCRIPT:/tmp/ensure-staging-app-path.php:ro" \
    owncloud /bin/bash -ec \
    'source /etc/owncloud.d/20-config.sh && php /tmp/ensure-staging-app-path.php'

log "Disabling the unavailable legacy gallery app in the staging copy if enabled."
GALLERY_ENABLED="$(docker exec "$DB_CONTAINER_ID" psql -X -U owncloud_stage -d owncloud_stage -Atc \
    "SELECT COALESCE((SELECT configvalue FROM oc_appconfig WHERE appid = 'gallery' AND configkey = 'enabled'), '')")"
if [[ "$GALLERY_ENABLED" == "yes" ]]; then
    compose run --rm --no-deps --user www-data owncloud /bin/bash -ec \
        'source /etc/owncloud.d/20-config.sh && /usr/bin/occ app:disable gallery'
fi

log "Running the image's ownCloud migration wrapper against the staged database."
compose run --rm --no-deps --user www-data owncloud /bin/bash -ec \
    'source /etc/owncloud.d/20-config.sh && /usr/bin/owncloud migrate'
log "Verifying the staging installation and database upgrade status."
STATUS="$(compose run --rm --no-deps --user www-data owncloud /bin/bash -ec \
    'source /etc/owncloud.d/20-config.sh && /usr/bin/occ status')"
printf '%s\n' "$STATUS"
grep -Eq '^[[:space:]]*-[[:space:]]+installed: true$' <<<"$STATUS" ||
    fail "ownCloud does not report an installed staging instance."
grep -Eq '^[[:space:]]*-[[:space:]]+version: 10\.16\.7(\.|$)' <<<"$STATUS" ||
    fail "Staging ownCloud is not running version 10.16.7."
log "Disabling maintenance mode on the upgraded staging copy."
compose run --rm --no-deps --user www-data owncloud /bin/bash -ec \
    'source /etc/owncloud.d/20-config.sh && /usr/bin/occ maintenance:mode --off'
log "Migration finished. The staging ownCloud web server remains stopped."
