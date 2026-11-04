#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/compose.yml"
NATIVE_DIR="${NATIVE_OWNCLOUD_DIR:-/srv/www/vhosts/mxdocs/owncloud}"
STAGE_ROOT="${OWNCLOUD_STAGE_ROOT:-/media/hannes/T1/owncloud-staging}"
STAGE_MOUNT="${OWNCLOUD_STAGE_MOUNT:-/media/hannes/T1}"
STATE_ROOT="${OWNCLOUD_STAGE_STATE_ROOT:-$STAGE_ROOT}"
STAGE_ENV="$STATE_ROOT/staging.env"
LOG_FILE="$STATE_ROOT/clone.log"
BACKUP_DIR="$STATE_ROOT/backup"
MIRROR_SYNC_LOCK="/run/lock/owncloud-mirror-sync.lock"
SOURCE_MAINTENANCE=0
TEMP_DIR=""
MIRROR_SYNC_LOCK_ACQUIRED=0
RESUME=0

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
format_gib() { awk -v kib="$1" 'BEGIN { printf "%.1f GiB", kib / 1048576 }'; }

case "${1:-}" in
    "") ;;
    --resume) RESUME=1 ;;
    *) fail "Usage: $0 [--resume]" ;;
esac
(( $# <= 1 )) || fail "Usage: $0 [--resume]"

cleanup() {
    local status=$?
    if (( SOURCE_MAINTENANCE )); then
        log "Restoring native ownCloud from maintenance mode."
        if ! runuser -u www-data -- php7.4 "$NATIVE_DIR/occ" maintenance:mode --off; then
            printf 'ERROR: Could not disable maintenance mode on native ownCloud.\n' >&2
            status=1
        fi
    fi
    if [[ -n "$TEMP_DIR" ]]; then
        rm -rf -- "$TEMP_DIR"
    fi
    if (( MIRROR_SYNC_LOCK_ACQUIRED )); then
        rmdir "$MIRROR_SYNC_LOCK"
    fi
    exit "$status"
}
trap cleanup EXIT

(( EUID == 0 )) || fail "Run as root, for example: sudo $SCRIPT_DIR/clone-native.sh"
for command_name in docker php7.4 pg_dump psql rsync python3 openssl runuser curl tee; do
    command -v "$command_name" >/dev/null || fail "Required command is missing: $command_name"
done
[[ -f "$NATIVE_DIR/config/config.php" ]] || fail "Native ownCloud config not found at $NATIVE_DIR/config/config.php"
[[ -x "$NATIVE_DIR/occ" ]] || fail "Native ownCloud occ command not found."

if (( RESUME )); then
    [[ -d "$STAGE_ROOT" && -d "$STATE_ROOT" && -f "$STAGE_ENV" ]] ||
        fail "Cannot resume: staging directories or saved staging.env are missing."
    [[ "$STAGE_ROOT" == "$STATE_ROOT" ]] ||
        fail "Cannot resume a partial initialization split across different data/state directories."
    SAVED_STAGE_ROOT="$(sed -n 's/^OWNCLOUD_STAGE_ROOT=//p' "$STAGE_ENV")"
    SAVED_STATE_ROOT="$(sed -n 's/^OWNCLOUD_STAGE_STATE_ROOT=//p' "$STAGE_ENV")"
    STAGE_DB_PASSWORD="$(sed -n 's/^OWNCLOUD_STAGE_DB_PASSWORD=//p' "$STAGE_ENV")"
    [[ "$SAVED_STAGE_ROOT" == "$STAGE_ROOT" && "$SAVED_STATE_ROOT" == "$STATE_ROOT" &&
        "$STAGE_DB_PASSWORD" =~ ^[0-9a-f]{64}$ ]] ||
        fail "Cannot resume: saved staging environment does not match the configured destination."
    [[ -d "$STAGE_ROOT/files" && -z "$(find "$STAGE_ROOT/files" -mindepth 1 -print -quit)" ]] ||
        fail "Cannot resume: staging files directory is not empty."
    [[ -d "$STAGE_ROOT/config" && -z "$(find "$STAGE_ROOT/config" -mindepth 1 -print -quit)" ]] ||
        fail "Cannot resume: staging config directory is not empty."
    [[ -d "$STAGE_ROOT/apps" && -z "$(find "$STAGE_ROOT/apps" -mindepth 1 -print -quit)" ]] ||
        fail "Cannot resume: staging apps directory is not empty."
    [[ ! -e "$BACKUP_DIR/native-database.dump" ]] ||
        fail "Cannot resume: a database dump already exists; inspect staging state instead of resuming."
    [[ -d "$BACKUP_DIR" && -z "$(find "$BACKUP_DIR" -mindepth 1 -print -quit)" ]] ||
        fail "Cannot resume: staging backup directory is not empty."
    [[ ! -e "$STAGE_ROOT/config/config.php" ]] ||
        fail "Cannot resume: staging ownCloud config already exists."
    log "Resuming the interrupted staging initialization without replacing its database credentials."
else
    if [[ -e "$STAGE_ROOT" ]] && find "$STAGE_ROOT" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
        fail "$STAGE_ROOT is not empty. Inspect it and use --resume only if this was the interrupted database initialization."
    fi
    if [[ -e "$STATE_ROOT" ]] && find "$STATE_ROOT" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
        fail "$STATE_ROOT is not empty. Refusing to overwrite existing staging state."
    fi
fi

if ! mkdir "$MIRROR_SYNC_LOCK" 2>/dev/null; then
    fail "The ownCloud mirror sync is running or its lock exists: $MIRROR_SYNC_LOCK"
fi
MIRROR_SYNC_LOCK_ACQUIRED=1

TEMP_DIR="$(mktemp -d /run/owncloud-stage.XXXXXX)"
chmod 700 "$TEMP_DIR"

SOURCE_VERSION="$(awk -F"'" '/OC_VersionString/ {print $2; exit}' "$NATIVE_DIR/version.php")"
[[ "$SOURCE_VERSION" == "10.16.6" ]] ||
    fail "Expected native ownCloud 10.16.6, found ${SOURCE_VERSION:-unknown}."

log "Reading native ownCloud connection and data-directory settings."
php7.4 -r '
$CONFIG = [];
require $argv[1];
$keys = ["dbtype", "dbname", "dbhost", "dbuser", "dbpassword", "dbport", "datadirectory"];
$result = [];
foreach ($keys as $key) {
    $result[$key] = $CONFIG[$key] ?? null;
}
$result["maintenance"] = !empty($CONFIG["maintenance"]);
$result["uses_objectstore"] = !empty($CONFIG["objectstore"]);
$result["writable_apps_paths"] = array_values(array_map(
    static fn($entry) => $entry["path"],
    array_filter($CONFIG["apps_paths"] ?? [], static fn($entry) =>
        !empty($entry["writable"]) && !empty($entry["path"])
    )
));
file_put_contents($argv[2], json_encode($result, JSON_THROW_ON_ERROR));
' "$NATIVE_DIR/config/config.php" "$TEMP_DIR/source.json"

read_config() {
    python3 -c 'import json,sys; value=json.load(open(sys.argv[1]))[sys.argv[2]]; print(str(value).lower() if isinstance(value,bool) else (value or ""))' \
        "$TEMP_DIR/source.json" "$1"
}

DB_TYPE="$(read_config dbtype)"
DB_NAME="$(read_config dbname)"
DB_HOST="$(read_config dbhost)"
DB_USER="$(read_config dbuser)"
DATA_DIR="$(read_config datadirectory)"
DB_PORT="$(read_config dbport)"
DB_PASSWORD="$(read_config dbpassword)"
USES_OBJECTSTORE="$(read_config uses_objectstore)"
SOURCE_ALREADY_IN_MAINTENANCE="$(read_config maintenance)"
mapfile -t WRITABLE_APP_PATHS < <(
    python3 -c 'import json,sys; sys.stdout.write("\n".join(json.load(open(sys.argv[1]))["writable_apps_paths"]))' \
        "$TEMP_DIR/source.json"
)

[[ "$DB_TYPE" == "pgsql" ]] || fail "Expected a PostgreSQL-backed native instance; detected: $DB_TYPE"
[[ "$DB_NAME" == "owncloud" && "$DB_USER" == "owncloud" ]] ||
    fail "Unexpected source database name or user; refusing to dump an unknown database."
[[ "$DB_HOST" == "localhost" || "$DB_HOST" == "127.0.0.1" ]] ||
    fail "Refusing unexpected source database host: $DB_HOST"
[[ -n "$DB_PASSWORD" && -n "$DATA_DIR" ]] ||
    fail "Native ownCloud config is missing its database password or data directory."
[[ "$DB_PORT" =~ ^[0-9]+$ ]] || DB_PORT=5432
(( DB_PORT > 0 && DB_PORT < 65536 )) || fail "Invalid PostgreSQL port in native ownCloud config."
[[ "$USES_OBJECTSTORE" != "true" ]] ||
    fail "Native ownCloud uses an object store; copying only its local data directory would be incomplete."
[[ -f "$DATA_DIR/.ocdata" ]] || fail "Configured ownCloud data directory is unavailable: $DATA_DIR"

for APP_PATH in "${WRITABLE_APP_PATHS[@]}"; do
    [[ "$APP_PATH" == /* && -d "$APP_PATH" ]] ||
        fail "Configured writable ownCloud app path is missing or not absolute: $APP_PATH"
done

write_pgpass() {
    php7.4 -r '
$CONFIG = [];
require $argv[1];
$fields = [
    $CONFIG["dbhost"] ?? "localhost",
    $CONFIG["dbport"] ?? 5432,
    $CONFIG["dbname"] ?? "",
    $CONFIG["dbuser"] ?? "",
    $CONFIG["dbpassword"] ?? "",
];
foreach ($fields as &$field) {
    $field = (string)$field;
    if (strpbrk($field, "\r\n") !== false) {
        fwrite(STDERR, "PostgreSQL connection settings contain an invalid newline.\n");
        exit(1);
    }
    $field = str_replace(["\\", ":"], ["\\\\", "\\:"], $field);
}
unset($field);
if (in_array("", array_slice($fields, 2), true)) {
    fwrite(STDERR, "Required PostgreSQL settings are missing from ownCloud config.\n");
    exit(1);
}
if (file_put_contents($argv[2], implode(":", $fields) . "\n", LOCK_EX) === false) {
    fwrite(STDERR, "Could not create the temporary PostgreSQL password file.\n");
    exit(1);
}
chmod($argv[2], 0600);
' "$NATIVE_DIR/config/config.php" "$TEMP_DIR/pgpass"
}

write_pgpass
log "Reading source PostgreSQL database size for the storage estimate."
DATABASE_BYTES="$(PGPASSFILE="$TEMP_DIR/pgpass" psql -X -qAt \
    --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" --dbname="$DB_NAME" \
    --command='SELECT pg_database_size(current_database())')"
[[ "$DATABASE_BYTES" =~ ^[0-9]+$ ]] || fail "Could not determine the source PostgreSQL database size."
DATABASE_KB=$(((DATABASE_BYTES + 1023) / 1024))

log "Checking storage space before copying native ownCloud files."
mountpoint -q "$STAGE_MOUNT" || fail "Target disk is not mounted at $STAGE_MOUNT; refusing to write to the root filesystem."
STAGE_FS_TYPE="$(findmnt -n -o FSTYPE --target "$STAGE_MOUNT")"
[[ "$STAGE_FS_TYPE" == "ext4" ]] ||
    fail "Expected an ext4 target at $STAGE_MOUNT, found $STAGE_FS_TYPE."
case "$STAGE_ROOT" in
    "$STAGE_MOUNT"/*) ;;
    *) fail "Staging data directory $STAGE_ROOT must be inside target mount $STAGE_MOUNT." ;;
esac
log "Staging destination: $STAGE_ROOT"
log "Target mount: $STAGE_MOUNT ($STAGE_FS_TYPE)"
log "Destination filesystem and current free space:"
DATA_FS_PATH="$STAGE_ROOT"
while [[ ! -e "$DATA_FS_PATH" ]]; do
    DATA_FS_PATH="$(dirname "$DATA_FS_PATH")"
done
df -h "$DATA_FS_PATH" | awk 'NR <= 2 { print }'
SOURCE_KB="$(du -sk "$DATA_DIR" | awk '{print $1}')"
log "Source data directory: $DATA_DIR ($(format_gib "$SOURCE_KB"))"
APP_KB=0
for APP_PATH in "${WRITABLE_APP_PATHS[@]}"; do
    APP_SIZE_KB="$(du -sk "$APP_PATH" | awk '{print $1}')"
    APP_KB=$((APP_KB + APP_SIZE_KB))
    log "Writable app directory: $APP_PATH ($(format_gib "$APP_SIZE_KB"))"
done
if ((${#WRITABLE_APP_PATHS[@]} == 0)); then
    log "Writable app directories: none"
fi
DATA_AVAILABLE_KB="$(df -Pk "$DATA_FS_PATH" | awk 'END {print $4}')"
DATA_DEVICE="$(df -Pk "$DATA_FS_PATH" | awk 'END {print $1}')"
STATE_FS_PATH="$STATE_ROOT"
while [[ ! -e "$STATE_FS_PATH" ]]; do
    STATE_FS_PATH="$(dirname "$STATE_FS_PATH")"
done
DB_AVAILABLE_KB="$(df -Pk "$STATE_FS_PATH" | awk 'END {print $4}')"
STATE_DEVICE="$(df -Pk "$STATE_FS_PATH" | awk 'END {print $1}')"
log "Source PostgreSQL database: $(format_gib "$DATABASE_KB")"
log "State volume: $STATE_ROOT"
df -h "$STATE_FS_PATH" | awk 'NR <= 2 { print }'
if [[ "$DATA_DEVICE" == "$STATE_DEVICE" ]]; then
    TOTAL_REQUIRED_KB=$((SOURCE_KB + APP_KB + 2 * DATABASE_KB + 5242880))
    log "Combined estimate on $DATA_DEVICE: data $(format_gib "$SOURCE_KB") + writable apps $(format_gib "$APP_KB") + database dump and restored database (2 x $(format_gib "$DATABASE_KB")) + 5.0 GiB reserve"
    log "Required free space: $(format_gib "$TOTAL_REQUIRED_KB"); available: $(format_gib "$DATA_AVAILABLE_KB")"
    (( DATA_AVAILABLE_KB >= TOTAL_REQUIRED_KB )) ||
        fail "Insufficient free space on $DATA_DEVICE: need at least $(format_gib "$TOTAL_REQUIRED_KB") but only $(format_gib "$DATA_AVAILABLE_KB") is available."
else
    DATA_REQUIRED_KB=$((SOURCE_KB + 2097152))
    STATE_REQUIRED_KB=$((APP_KB + 2 * DATABASE_KB + 2097152))
    log "Data-volume estimate: data $(format_gib "$SOURCE_KB") + 2.0 GiB reserve"
    log "Data volume required: $(format_gib "$DATA_REQUIRED_KB"); available: $(format_gib "$DATA_AVAILABLE_KB")"
    log "State-volume estimate: writable apps $(format_gib "$APP_KB") + database dump and restored database (2 x $(format_gib "$DATABASE_KB")) + 2.0 GiB reserve"
    log "State volume required: $(format_gib "$STATE_REQUIRED_KB"); available: $(format_gib "$DB_AVAILABLE_KB")"
    (( DATA_AVAILABLE_KB >= DATA_REQUIRED_KB )) ||
        fail "Insufficient free space on $DATA_DEVICE: need at least $(format_gib "$DATA_REQUIRED_KB") but only $(format_gib "$DATA_AVAILABLE_KB") is available."
    (( DB_AVAILABLE_KB >= STATE_REQUIRED_KB )) ||
        fail "Insufficient free space on $STATE_DEVICE for staging apps and PostgreSQL."
fi

umask 077
mkdir -p "$STAGE_ROOT/files" "$BACKUP_DIR" "$STATE_ROOT/config" "$STATE_ROOT/apps" "$STATE_ROOT/apps/marketplace" "$STATE_ROOT/database"
chmod 755 "$STAGE_ROOT" "$STATE_ROOT"
chown 33:0 "$STAGE_ROOT/files"
chmod 750 "$STAGE_ROOT/files"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1
if (( ! RESUME )); then
    cat > "$STAGE_ENV" <<EOF
OWNCLOUD_STAGE_ROOT=$STAGE_ROOT
OWNCLOUD_STAGE_STATE_ROOT=$STATE_ROOT
OWNCLOUD_STAGE_DB_PASSWORD=$(openssl rand -hex 32)
EOF
    chmod 600 "$STAGE_ENV"
fi
chmod 700 "$BACKUP_DIR"

compose() {
    docker compose --env-file "$STAGE_ENV" -f "$COMPOSE_FILE" "$@"
}

if (( RESUME )); then
    DB_CONTAINER_ID="$(compose ps -q db)"
    [[ -n "$DB_CONTAINER_ID" ]] || fail "Cannot resume: the staging PostgreSQL container does not exist."
    [[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$DB_CONTAINER_ID")" == "owncloud-staging" ]] ||
        fail "Cannot resume: the database container does not belong to the owncloud-staging Compose project."
    [[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$DB_CONTAINER_ID")" == "db" ]] ||
        fail "Cannot resume: the existing container is not the staging database."
    [[ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Source}}{{end}}{{end}}' "$DB_CONTAINER_ID")" == "$STAGE_ROOT/database" ]] ||
        fail "Cannot resume: the staging database is not using the expected T1 database directory."
    [[ "$(docker inspect -f '{{.State.Health.Status}}' "$DB_CONTAINER_ID")" == "healthy" ]] ||
        fail "Cannot resume: the existing staging database is not healthy."
    USER_TABLES="$(docker exec "$DB_CONTAINER_ID" psql -X -U owncloud_stage -d owncloud_stage -Atc \
        "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
    [[ "$USER_TABLES" == "0" ]] ||
        fail "Cannot resume: staging PostgreSQL already contains $USER_TABLES public tables."
fi

wait_for_healthy() {
    local service="$1"
    local container_id
    local health
    container_id="$(compose ps -q "$service")"
    [[ -n "$container_id" ]] || fail "Compose did not create the $service container."
    for _ in $(seq 1 90); do
        health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$container_id" 2>/dev/null || true)"
        [[ "$health" == "healthy" ]] && return 0
        if [[ "$health" == "unhealthy" ]]; then
            compose logs --tail=80 "$service" >&2
            fail "The staging $service container became unhealthy."
        fi
        sleep 2
    done
    compose logs --tail=80 "$service" >&2
    fail "The staging $service container did not become healthy within 180 seconds (last status: ${health:-unknown})."
}

log "Starting the isolated PostgreSQL database and Redis."
compose up -d db redis
wait_for_healthy db
wait_for_healthy redis
DB_CONTAINER_ID="$(compose ps -q db)"
docker exec "$DB_CONTAINER_ID" psql -X -U owncloud_stage -d owncloud_stage -Atc 'SELECT 1' |
    grep -qx '1' || fail "Staging PostgreSQL is healthy but the owncloud_stage database cannot be queried."

log "Copying the bulk of ownCloud data while the native service remains available."
rsync -aHAX --numeric-ids --delete --exclude=/sessions/ --exclude=/.owncloud-mirror-managed \
    "$DATA_DIR/" "$STAGE_ROOT/files/"

APP_MAPPINGS=()
for index in "${!WRITABLE_APP_PATHS[@]}"; do
    APP_SOURCE="${WRITABLE_APP_PATHS[$index]}"
    APP_TARGET="$STATE_ROOT/apps/$index"
    mkdir -p "$APP_TARGET"
    rsync -aHAX --numeric-ids --delete "$APP_SOURCE/" "$APP_TARGET/"
    APP_MAPPINGS+=("/mnt/data/apps/$index")
done
python3 - "$TEMP_DIR/apps.json" "${APP_MAPPINGS[@]}" <<'PY'
import json
import sys

with open(sys.argv[1], "w", encoding="utf-8") as output:
    json.dump(sys.argv[2:], output)
PY

log "Enabling maintenance mode for the final file and database snapshot."
if [[ "$SOURCE_ALREADY_IN_MAINTENANCE" == "true" ]]; then
    log "Native ownCloud was already in maintenance mode; it will be left that way."
else
    SOURCE_MAINTENANCE=1
    runuser -u www-data -- php7.4 "$NATIVE_DIR/occ" maintenance:mode --on
fi

rsync -aHAX --numeric-ids --delete --exclude=/sessions/ --exclude=/.owncloud-mirror-managed \
    "$DATA_DIR/" "$STAGE_ROOT/files/"
for index in "${!WRITABLE_APP_PATHS[@]}"; do
    rsync -aHAX --numeric-ids --delete "${WRITABLE_APP_PATHS[$index]}/" "$STATE_ROOT/apps/$index/"
done
rsync -aHAX --numeric-ids --delete --exclude=/config.php --exclude=/.owncloud-mirror-managed \
    "$NATIVE_DIR/config/" "$STATE_ROOT/config/"

log "Creating a restricted PostgreSQL database snapshot."
PGPASSFILE="$TEMP_DIR/pgpass" pg_dump \
    --format=custom --no-owner --no-privileges \
    --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" --dbname="$DB_NAME" \
    --file="$BACKUP_DIR/native-database.dump"
chmod 600 "$BACKUP_DIR/native-database.dump"

if (( SOURCE_MAINTENANCE )); then
    log "Database snapshot complete; restoring the native service."
    runuser -u www-data -- php7.4 "$NATIVE_DIR/occ" maintenance:mode --off
    SOURCE_MAINTENANCE=0
fi

log "Writing a staging-only ownCloud configuration."
STAGE_DB_PASSWORD="$(sed -n 's/^OWNCLOUD_STAGE_DB_PASSWORD=//p' "$STAGE_ENV")"
STAGE_DB_PASSWORD="$STAGE_DB_PASSWORD" php7.4 -r '
$CONFIG = [];
require $argv[1];
$CONFIG["dbtype"] = "pgsql";
$CONFIG["dbname"] = "owncloud_stage";
$CONFIG["dbhost"] = "db";
$CONFIG["dbuser"] = "owncloud_stage";
$CONFIG["dbpassword"] = getenv("STAGE_DB_PASSWORD");
$CONFIG["datadirectory"] = "/mnt/data/files";
$CONFIG["logfile"] = "/mnt/data/files/owncloud.log";
$CONFIG["tempdirectory"] = "/tmp";
$appsPaths = [
    ["path" => "/var/www/owncloud/apps", "url" => "/apps", "writable" => false],
    ["path" => "/mnt/data/apps/marketplace", "url" => "/apps-external/marketplace", "writable" => true],
];
foreach (json_decode(file_get_contents($argv[3]), true, 512, JSON_THROW_ON_ERROR) as $index => $path) {
    $appsPaths[] = ["path" => $path, "url" => "/apps-external/" . $index, "writable" => true];
}
$CONFIG["apps_paths"] = $appsPaths;
$CONFIG["trusted_domains"] = ["localhost:8093", "localhost", "127.0.0.1"];
$CONFIG["maintenance"] = false;
$CONFIG["memcache.local"] = "\\OC\\Memcache\\APCu";
$CONFIG["memcache.locking"] = "\\OC\\Memcache\\Redis";
$CONFIG["redis"] = ["host" => "redis", "port" => 6379, "timeout" => 1.5];
foreach ([
    "overwritehost", "overwriteprotocol", "overwrite.cli.url", "overwritewebroot",
    "mail_smtpmode", "mail_smtphost", "mail_smtpport", "mail_smtpname",
    "mail_smtppassword", "mail_smtpsecure", "mail_smtpauthtype",
    "mail_from_address", "mail_domain", "objectstore",
] as $key) {
    unset($CONFIG[$key]);
}
$target = $argv[2] . ".tmp";
if (file_put_contents($target, "<?php\n\$CONFIG = " . var_export($CONFIG, true) . ";\n", LOCK_EX) === false) {
    fwrite(STDERR, "Could not write staging ownCloud config.\n");
    exit(1);
}
chmod($target, 0640);
if (!rename($target, $argv[2])) {
    unlink($target);
    fwrite(STDERR, "Could not install staging ownCloud config.\n");
    exit(1);
}
' "$NATIVE_DIR/config/config.php" "$STATE_ROOT/config/config.php" "$TEMP_DIR/apps.json"
unset STAGE_DB_PASSWORD DB_PASSWORD
chown -R 33:0 "$STATE_ROOT/config" "$STATE_ROOT/apps"
chmod 750 "$STATE_ROOT/config" "$STATE_ROOT/apps"

log "Restoring the snapshot into the isolated PostgreSQL database."
STAGE_DB_PASSWORD="$(sed -n 's/^OWNCLOUD_STAGE_DB_PASSWORD=//p' "$STAGE_ENV")"
compose exec -T -e "PGPASSWORD=$STAGE_DB_PASSWORD" db \
    pg_restore --exit-on-error --no-owner --no-privileges \
        -U owncloud_stage -d owncloud_stage < "$BACKUP_DIR/native-database.dump"
unset STAGE_DB_PASSWORD

log "Disabling the unavailable legacy gallery app in the staging copy if enabled."
GALLERY_ENABLED="$(docker exec "$DB_CONTAINER_ID" psql -X -U owncloud_stage -d owncloud_stage -Atc \
    "SELECT COALESCE((SELECT configvalue FROM oc_appconfig WHERE appid = 'gallery' AND configkey = 'enabled'), '')")"
if [[ "$GALLERY_ENABLED" == "yes" ]]; then
    compose run --rm --no-deps --user www-data owncloud /bin/bash -ec \
        'source /etc/owncloud.d/20-config.sh && /usr/bin/occ app:disable gallery'
fi

log "Running the ownCloud 10.16.7 migration wrapper against the staging copy."
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
log "Staging upgrade finished. The staging ownCloud web server remains stopped."
