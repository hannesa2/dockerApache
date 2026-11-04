#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/compose.yml"
NATIVE_DIR="${NATIVE_OWNCLOUD_DIR:-/srv/www/vhosts/mxdocs/owncloud}"
SOURCE_DATA="/daten/owncloud/data"
STAGE_ROOT="${OWNCLOUD_STAGE_ROOT:-/media/hannes/T1/owncloud-staging}"
STAGE_MOUNT="${OWNCLOUD_STAGE_MOUNT:-/media/hannes/T1}"
STATE_ROOT="${OWNCLOUD_STAGE_STATE_ROOT:-$STAGE_ROOT}"
STAGE_ENV="$STATE_ROOT/staging.env"
BACKUP_DIR="$STATE_ROOT/backup"
VHOST_LINK="/etc/apache2/sites-enabled/mxinfo.ssl"
VHOST_FILE=""
LOCK_DIR="/run/lock/owncloud-mirror-sync.lock"
LOCK_ACQUIRED=0
TEMP_DIR=""
SOURCE_MAINTENANCE_CHANGED=0
APACHE_CONFIG_CHANGED=0
CUTOVER_RELEASED=0
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
DB_DUMP="$BACKUP_DIR/native-pre-cutover-$TIMESTAMP.dump"
VHOST_BACKUP="$BACKUP_DIR/apache-vhost-pre-cutover-$TIMESTAMP.conf"
LOG_FILE="$STATE_ROOT/cutover-$TIMESTAMP.log"
PREFLIGHT=0

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

case "${1:-}" in
    "") ;;
    --preflight) PREFLIGHT=1 ;;
    *) fail "Usage: $0 [--preflight]" ;;
esac
(( $# <= 1 )) || fail "Usage: $0 [--preflight]"

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if (( status != 0 && ! CUTOVER_RELEASED &&
        (APACHE_CONFIG_CHANGED || SOURCE_MAINTENANCE_CHANGED) )); then
        log "Cutover did not complete; restoring the native Apache route and service."
        if (( APACHE_CONFIG_CHANGED )); then
            cp -a -- "$VHOST_BACKUP" "$VHOST_FILE" || status=1
            if apache2ctl -t && apache2ctl graceful; then
                :
            else
                printf 'ERROR: Apache rollback failed; inspect %s\n' "$VHOST_FILE" >&2
                status=1
            fi
        fi
        if (( SOURCE_MAINTENANCE_CHANGED )); then
            if ! runuser -u www-data -- php7.4 "$NATIVE_DIR/occ" maintenance:mode --off; then
                printf 'ERROR: Could not disable native ownCloud maintenance mode.\n' >&2
                status=1
            fi
        fi
    fi
    if (( status != 0 && CUTOVER_RELEASED )); then
        printf 'ERROR: Cutover was released; do not roll back automatically because writes may have occurred.\n' >&2
    fi
    [[ -z "$TEMP_DIR" ]] || rm -rf -- "$TEMP_DIR"
    if (( LOCK_ACQUIRED )); then
        rmdir "$LOCK_DIR" || status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

(( EUID == 0 )) || fail "Run as root: sudo $SCRIPT_DIR/cutover-native.sh"
for command_name in docker rsync php7.4 pg_dump psql runuser python3 mountpoint findmnt \
    apache2ctl systemctl curl; do
    command -v "$command_name" >/dev/null || fail "Required command is missing: $command_name"
done

[[ "$STAGE_ROOT" == "$STATE_ROOT" ]] ||
    fail "Data and state paths differ; inspect the Compose configuration before cutover."
[[ -d "$STAGE_ROOT/files" && -d "$STATE_ROOT/config" && -d "$STATE_ROOT/apps" &&
    -d "$STATE_ROOT/database" && -f "$STAGE_ENV" ]] ||
    fail "The existing staging copy is incomplete."
[[ -s "$BACKUP_DIR/native-database.dump" ]] ||
    fail "The original staging database dump is missing; refusing cutover without a rollback artifact."
[[ -f "$NATIVE_DIR/config/config.php" && -f "$NATIVE_DIR/occ" &&
    -f "$SOURCE_DATA/.ocdata" ]] ||
    fail "Native ownCloud code, config, or data marker is unavailable."
[[ "$(stat -c '%a' "$STAGE_ENV")" == "600" ]] ||
    fail "Expected restricted permissions (0600) on $STAGE_ENV."
mountpoint -q "$STAGE_MOUNT" || fail "Target disk is not mounted at $STAGE_MOUNT."
[[ "$(findmnt -n -o FSTYPE --target "$STAGE_MOUNT")" == "ext4" ]] ||
    fail "Expected an ext4 filesystem mounted at $STAGE_MOUNT."
case "$STAGE_ROOT" in
    "$STAGE_MOUNT"/*) ;;
    *) fail "Staging path $STAGE_ROOT is outside target mount $STAGE_MOUNT." ;;
esac
[[ "$(awk -F"'" '/OC_VersionString/ {print $2; exit}' "$NATIVE_DIR/version.php")" == "10.16.6" ]] ||
    fail "Expected native ownCloud 10.16.6."
[[ "$(systemctl is-active apache2)" == "active" ]] || fail "Host Apache is not active."
[[ -L "$VHOST_LINK" ]] || fail "Expected active ownCloud vhost symlink at $VHOST_LINK."
VHOST_FILE="$(readlink -f "$VHOST_LINK")"
[[ "$VHOST_FILE" == /etc/apache2/sites-available/* && -f "$VHOST_FILE" ]] ||
    fail "The active HTTPS vhost does not resolve to a sites-available file."
grep -qi 'ServerName[[:space:]]\+www\.mxtracks\.info' "$VHOST_FILE" ||
    fail "The active HTTPS vhost is not for www.mxtracks.info."
grep -q 'owncloud-staging-cutover' "$VHOST_FILE" &&
    fail "The ownCloud production route already points to staging."
if grep -Eiq '^[[:space:]]*(ProxyPass|ProxyPassMatch|Alias)[[:space:]]+/?owncloud(/|[[:space:]]|$)' "$VHOST_FILE"; then
    fail "The active vhost already defines an ownCloud-specific proxy or alias; inspect it before cutover."
fi
APACHE_MODULES="$(apache2ctl -M 2>&1)"
for module_name in proxy_module proxy_http_module headers_module alias_module; do
    grep -q "$module_name" <<<"$APACHE_MODULES" ||
        fail "Required Apache module is not enabled: $module_name"
done

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    fail "The ownCloud mirror sync is running or its lock exists: $LOCK_DIR"
fi
LOCK_ACQUIRED=1

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
TEMP_DIR="$(mktemp -d /run/owncloud-cutover.XXXXXX)"
chmod 700 "$TEMP_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

SAVED_STAGE_ROOT="$(sed -n 's/^OWNCLOUD_STAGE_ROOT=//p' "$STAGE_ENV")"
SAVED_STATE_ROOT="$(sed -n 's/^OWNCLOUD_STAGE_STATE_ROOT=//p' "$STAGE_ENV")"
STAGE_DB_PASSWORD="$(sed -n 's/^OWNCLOUD_STAGE_DB_PASSWORD=//p' "$STAGE_ENV")"
[[ "$SAVED_STAGE_ROOT" == "$STAGE_ROOT" && "$SAVED_STATE_ROOT" == "$STATE_ROOT" &&
    "$STAGE_DB_PASSWORD" =~ ^[0-9a-f]{64}$ ]] ||
    fail "Saved staging environment does not match the T1 staging paths."
unset STAGE_DB_PASSWORD

compose() { docker compose --env-file "$STAGE_ENV" -f "$COMPOSE_FILE" "$@"; }
compose config --quiet

DB_CONTAINER_ID="$(compose ps -q db)"
[[ -n "$DB_CONTAINER_ID" ]] || fail "The staging PostgreSQL container does not exist."
[[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$DB_CONTAINER_ID")" == "owncloud-staging" ]] ||
    fail "The PostgreSQL container belongs to a different Compose project."
[[ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Source}}{{end}}{{end}}' "$DB_CONTAINER_ID")" == "$STAGE_ROOT/database" ]] ||
    fail "The PostgreSQL container is not using the expected T1 database directory."
[[ "$(docker inspect -f '{{.State.Health.Status}}' "$DB_CONTAINER_ID")" == "healthy" ]] ||
    fail "The staging PostgreSQL container is not healthy."
(( "$(docker exec "$DB_CONTAINER_ID" psql -X -U owncloud_stage -d owncloud_stage -Atc \
    "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")" > 0 )) ||
    fail "The existing staging database is empty."

APP_CONTAINER_ID="$(compose ps -q owncloud)"
if [[ -n "$APP_CONTAINER_ID" ]] &&
    [[ "$(docker inspect -f '{{.State.Running}}' "$APP_CONTAINER_ID")" == "true" ]]; then
    log "Stopping the staging web service before final synchronization."
    compose stop owncloud
fi

log "Reading native ownCloud settings for the final consistent snapshot."
php7.4 -r '
$CONFIG = [];
require $argv[1];
$result = [
    "dbtype" => $CONFIG["dbtype"] ?? null,
    "dbname" => $CONFIG["dbname"] ?? null,
    "dbhost" => $CONFIG["dbhost"] ?? null,
    "dbport" => $CONFIG["dbport"] ?? 5432,
    "dbuser" => $CONFIG["dbuser"] ?? null,
    "dbpassword" => $CONFIG["dbpassword"] ?? null,
    "dbtableprefix" => $CONFIG["dbtableprefix"] ?? null,
    "maintenance" => !empty($CONFIG["maintenance"]),
    "writable_apps_paths" => array_values(array_map(
        static fn($entry) => $entry["path"],
        array_filter($CONFIG["apps_paths"] ?? [], static fn($entry) =>
            !empty($entry["writable"]) && !empty($entry["path"])
        )
    )),
];
file_put_contents($argv[2], json_encode($result, JSON_THROW_ON_ERROR), LOCK_EX);
chmod($argv[2], 0600);
' "$NATIVE_DIR/config/config.php" "$TEMP_DIR/source.json"

read_source() {
    python3 -c 'import json,sys; v=json.load(open(sys.argv[1]))[sys.argv[2]]; print(str(v).lower() if isinstance(v,bool) else (v or ""))' \
        "$TEMP_DIR/source.json" "$1"
}
[[ "$(read_source dbtype)" == "pgsql" && "$(read_source dbname)" == "owncloud" &&
    "$(read_source dbuser)" == "owncloud" ]] ||
    fail "Unexpected native ownCloud PostgreSQL settings."
DB_HOST="$(read_source dbhost)"
DB_PORT="$(read_source dbport)"
[[ "$DB_HOST" == "localhost" || "$DB_HOST" == "127.0.0.1" ]] ||
    fail "Refusing unexpected native PostgreSQL host: $DB_HOST"
[[ "$DB_PORT" =~ ^[0-9]+$ ]] || fail "Invalid native PostgreSQL port."
[[ "$(read_source dbtableprefix)" == "oc_" ]] ||
    fail "Native database prefix is not oc_; inspect the staged Compose configuration."
SOURCE_ALREADY_IN_MAINTENANCE="$(read_source maintenance)"
mapfile -t WRITABLE_APP_PATHS < <(
    python3 -c 'import json,sys; sys.stdout.write("\n".join(path for path in json.load(open(sys.argv[1]))["writable_apps_paths"] if path))' \
        "$TEMP_DIR/source.json"
)
for APP_PATH in "${WRITABLE_APP_PATHS[@]}"; do
    [[ "$APP_PATH" == /* && -d "$APP_PATH" ]] ||
        fail "Native writable app path is unavailable: $APP_PATH"
done

python3 - "$TEMP_DIR/source.json" "$TEMP_DIR/pgpass" <<'PY'
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    config = json.load(source)
fields = [
    config["dbhost"] or "localhost",
    config["dbport"] or 5432,
    config["dbname"],
    config["dbuser"],
    config["dbpassword"],
]
if any(value is None or value == "" for value in fields[2:]):
    raise SystemExit("Native PostgreSQL credentials are incomplete.")
escaped = [str(value).replace("\\", "\\\\").replace(":", "\\:") for value in fields]
with open(sys.argv[2], "w", encoding="utf-8") as password_file:
    password_file.write(":".join(escaped) + "\n")
os.chmod(sys.argv[2], 0o600)
PY

DB_SIZE_BYTES="$(PGPASSFILE="$TEMP_DIR/pgpass" psql -X -qAt \
    --host="$DB_HOST" --port="$DB_PORT" --username=owncloud --dbname=owncloud \
    --command='SELECT pg_database_size(current_database())')"
[[ "$DB_SIZE_BYTES" =~ ^[0-9]+$ ]] || fail "Could not determine native database size."
FREE_KB="$(df -Pk "$STAGE_ROOT" | awk 'END {print $4}')"
(( FREE_KB > 5242880 + (DB_SIZE_BYTES + 1023) / 1024 )) ||
    fail "Less than database-size plus 5 GiB remains free on T1."
log "Native database size: $((DB_SIZE_BYTES / 1048576)) MiB; available T1 space: $((FREE_KB / 1048576)) GiB."

if (( PREFLIGHT )); then
    CURRENT_STATUS="$(curl -fsS --max-time 30 --resolve www.mxtracks.info:443:127.0.0.1 \
        https://www.mxtracks.info/owncloud/status.php)" ||
        fail "The current production ownCloud HTTPS endpoint is not healthy."
    python3 -c 'import json,sys; s=json.load(sys.stdin); assert s.get("installed") and s.get("version","").startswith("10.16.6"), "Production endpoint is not ownCloud 10.16.6."' \
        <<<"$CURRENT_STATUS" || fail "The current production ownCloud endpoint does not report version 10.16.6."
    log "Host memory available: $(awk '/MemAvailable:/ {printf "%.1f GiB", $2 / 1048576}' /proc/meminfo)."
    log "Preflight passed. No maintenance mode, data, database, or Apache configuration was changed."
    exit 0
fi

if [[ "$SOURCE_ALREADY_IN_MAINTENANCE" != "true" ]]; then
    log "Enabling maintenance mode on native ownCloud."
    SOURCE_MAINTENANCE_CHANGED=1
    runuser -u www-data -- php7.4 "$NATIVE_DIR/occ" maintenance:mode --on
fi

log "Applying the final incremental ownCloud file sync."
rsync -aHAX --numeric-ids --delete --exclude=/sessions/ --exclude=/.owncloud-mirror-managed \
    --stats "$SOURCE_DATA/" "$STAGE_ROOT/files/"
for index in "${!WRITABLE_APP_PATHS[@]}"; do
    rsync -aHAX --numeric-ids --delete --exclude=/.owncloud-mirror-managed \
        --stats "${WRITABLE_APP_PATHS[$index]}/" "$STATE_ROOT/apps/$index/"
done

log "Creating a fresh rollback-capable native PostgreSQL snapshot."
PGPASSFILE="$TEMP_DIR/pgpass" pg_dump --format=custom --no-owner --no-privileges \
    --host="$DB_HOST" --port="$DB_PORT" --username=owncloud --dbname=owncloud \
    --file="$DB_DUMP"
chmod 600 "$DB_DUMP"
[[ -s "$DB_DUMP" ]] || fail "The final native database dump is empty."

log "Replacing only the staging database with the consistent native snapshot."
docker exec "$DB_CONTAINER_ID" psql -X -v ON_ERROR_STOP=1 -U owncloud_stage -d postgres \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'owncloud_stage' AND pid <> pg_backend_pid()" >/dev/null
docker exec "$DB_CONTAINER_ID" dropdb -U owncloud_stage --if-exists owncloud_stage
docker exec "$DB_CONTAINER_ID" createdb -U owncloud_stage -O owncloud_stage owncloud_stage
compose exec -T db pg_restore --exit-on-error --no-owner --no-privileges \
    -U owncloud_stage -d owncloud_stage < "$DB_DUMP"

log "Registering the writable marketplace app path required by the restored database."
mkdir -p "$STATE_ROOT/apps/marketplace"
chown 33:0 "$STATE_ROOT/apps/marketplace"
chmod 750 "$STATE_ROOT/apps/marketplace"
cat > "$TEMP_DIR/ensure-marketplace-app-path.php" <<'PHP'
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
chmod 644 "$TEMP_DIR/ensure-marketplace-app-path.php"
compose run --rm --no-deps --user www-data \
    -v "$TEMP_DIR/ensure-marketplace-app-path.php:/tmp/ensure-marketplace-app-path.php:ro" \
    owncloud /bin/bash -ec \
    'source /etc/owncloud.d/20-config.sh && php /tmp/ensure-marketplace-app-path.php'

GALLERY_ENABLED="$(docker exec "$DB_CONTAINER_ID" psql -X -U owncloud_stage -d owncloud_stage -Atc \
    "SELECT COALESCE((SELECT configvalue FROM oc_appconfig WHERE appid = 'gallery' AND configkey = 'enabled'), '')")"
if [[ "$GALLERY_ENABLED" == "yes" ]]; then
    log "Disabling the unavailable gallery app in the staging copy."
    compose run --rm --no-deps --user www-data owncloud /bin/bash -ec \
        'source /etc/owncloud.d/20-config.sh && /usr/bin/occ app:disable gallery'
fi

run_app_command() {
    compose run --rm --no-deps --user www-data owncloud /bin/bash -ec \
        'source /etc/owncloud.d/20-config.sh && exec "$@"' owncloud-cli "$@"
}

log "Upgrading the staging database to ownCloud 10.16.7."
run_app_command /usr/bin/owncloud migrate
STATUS="$(run_app_command /usr/bin/occ status)"
printf '%s\n' "$STATUS"
grep -Eq '^[[:space:]]*-[[:space:]]+installed: true$' <<<"$STATUS" ||
    fail "The upgraded staging copy does not report installed."
grep -Eq '^[[:space:]]*-[[:space:]]+version: 10\.16\.7(\.|$)' <<<"$STATUS" ||
    fail "The staging copy is not running ownCloud 10.16.7."

log "Applying the production URL settings to the staging copy."
run_app_command /usr/bin/occ config:system:set trusted_domains 0 --value=www.mxtracks.info
run_app_command /usr/bin/occ config:system:set overwritehost --value=www.mxtracks.info
run_app_command /usr/bin/occ config:system:set overwriteprotocol --value=https
run_app_command /usr/bin/occ config:system:set overwritewebroot --value=/owncloud
run_app_command /usr/bin/occ config:system:set overwrite.cli.url --value=https://www.mxtracks.info/owncloud
PROXY_GATEWAY="$(docker network inspect owncloud-staging_owncloud_stage \
    --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}')"
[[ "$PROXY_GATEWAY" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
    fail "Could not determine the staging container's trusted proxy address."
run_app_command /usr/bin/occ config:system:set trusted_proxies 0 --value="$PROXY_GATEWAY"

log "Starting the staging ownCloud service on loopback only."
compose up -d owncloud
APP_CONTAINER_ID="$(compose ps -q owncloud)"
[[ -n "$APP_CONTAINER_ID" ]] || fail "Compose did not create the staging ownCloud container."
for _ in $(seq 1 60); do
    [[ "$(docker inspect -f '{{.State.Running}}' "$APP_CONTAINER_ID" 2>/dev/null || true)" == "true" ]] && break
    sleep 2
done
[[ "$(docker inspect -f '{{.State.Running}}' "$APP_CONTAINER_ID")" == "true" ]] ||
    fail "The staging ownCloud web service did not stay running."
STATUS_READY=0
for _ in $(seq 1 60); do
    if curl -fsS --connect-timeout 2 --max-time 10 \
        http://127.0.0.1:8093/status.php -o "$TEMP_DIR/status.json" \
        2>"$TEMP_DIR/status-curl.err"; then
        STATUS_READY=1
        break
    fi
    [[ "$(docker inspect -f '{{.State.Running}}' "$APP_CONTAINER_ID" 2>/dev/null || true)" == "true" ]] ||
        fail "The staging ownCloud web service exited during startup."
    sleep 2
done
if (( ! STATUS_READY )); then
    cat "$TEMP_DIR/status-curl.err" >&2
    docker logs --tail 40 "$APP_CONTAINER_ID" >&2
    fail "Staging ownCloud status endpoint did not become ready on loopback within 120 seconds."
fi
python3 - "$TEMP_DIR/status.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as response:
    status = json.load(response)
if not status.get("installed") or not status.get("version", "").startswith("10.16.7"):
    raise SystemExit("Staging HTTP status does not report installed ownCloud 10.16.7.")
PY

log "Backing up the active Apache vhost and adding only the /owncloud/ proxy."
cp -a -- "$VHOST_FILE" "$VHOST_BACKUP"
APACHE_CONFIG_CHANGED=1
python3 - "$VHOST_FILE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
if "owncloud-staging-cutover" in text:
    raise SystemExit("An ownCloud staging proxy marker already exists.")
closing = text.lower().rfind("</virtualhost>")
if closing < 0:
    raise SystemExit("Could not locate the active vhost closing directive.")
rules = """    # owncloud-staging-cutover
    RedirectMatch 301 ^/owncloud$ /owncloud/
    ProxyPass /owncloud/ http://127.0.0.1:8093/ connectiontimeout=5 timeout=3600 retry=0
    ProxyPassReverse /owncloud/ http://127.0.0.1:8093/
    <Location /owncloud/>
        RequestHeader set X-Forwarded-Proto "https"
    </Location>
"""
path.write_text(text[:closing] + rules + text[closing:])
PY
apache2ctl -t
apache2ctl graceful

log "Verifying the production ownCloud route before releasing maintenance mode."
curl -fsS --max-time 30 --resolve www.mxtracks.info:443:127.0.0.1 \
    https://www.mxtracks.info/owncloud/status.php -o "$TEMP_DIR/proxy-status.json" ||
    fail "The production HTTPS ownCloud route did not respond successfully."
python3 - "$TEMP_DIR/proxy-status.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as response:
    status = json.load(response)
if not status.get("installed") or not status.get("version", "").startswith("10.16.7"):
    raise SystemExit("Production HTTPS route does not report installed ownCloud 10.16.7.")
PY

log "Disabling maintenance mode on Docker ownCloud; native ownCloud remains in maintenance as rollback."
CUTOVER_RELEASED=1
run_app_command /usr/bin/occ maintenance:mode --off
log "Cutover complete: https://www.mxtracks.info/owncloud/ now serves ownCloud 10.16.7."
log "Native ownCloud remains untouched except maintenance mode; rollback dump: $DB_DUMP"
