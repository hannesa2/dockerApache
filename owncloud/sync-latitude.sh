#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: Run this backup as root on latitude." >&2
    exit 1
fi

SOURCE_CODE="/srv/www/vhosts/mxdocs/owncloud"
SOURCE_DATA="/daten/owncloud/data"
SOURCE_CONFIG="$SOURCE_CODE/config/config.php"
SOURCE_OCC="$SOURCE_CODE/occ"
MIRROR_HOST="mac2016"
MIRROR_USER="hannes"
MIRROR_ROOT="/Volumes/SamsungT7_2T/ownCloudBackup/owncloud"
MIRROR_CODE="$MIRROR_ROOT/owncloud"
MIRROR_DATA="$MIRROR_ROOT/data"
MIRROR_SOURCE_CONFIG="$MIRROR_ROOT/source-config"
SSH_KEY="/home/hannes/.ssh/id_rsa"
KNOWN_HOSTS="/home/hannes/.ssh/known_hosts"
COMPOSE_FILE="/Users/hannes/git/dockerApache/apache/docker-compose.yml"
ACTIVATE_SCRIPT="/Users/hannes/git/dockerApache/owncloud/activate-proxy.sh"
LOCK_DIR="/run/lock/owncloud-mirror-sync.lock"
PGPASS_FILE=""
DB_DUMP=""

die() {
    echo "ERROR: $*" >&2
    exit 1
}

remote() {
    ssh -i "$SSH_KEY" \
        -o IdentitiesOnly=yes \
        -o BatchMode=yes \
        -o ConnectTimeout=15 \
        -o ServerAliveInterval=15 \
        -o ServerAliveCountMax=3 \
        -o StrictHostKeyChecking=yes \
        -o UserKnownHostsFile="$KNOWN_HOSTS" \
        "$MIRROR_USER@$MIRROR_HOST" \
        "export PATH='/Applications/OrbStack.app/Contents/MacOS/xbin:/usr/local/bin:/usr/bin:/bin'; $1"
}

sync_code() {
    echo "Syncing ownCloud code..."
    rsync -rltp --delete --partial \
        --exclude=/config/ \
        --exclude=/data/ \
        --exclude=/.owncloud-mirror-managed \
        --info=stats1 \
        -e "ssh -i $SSH_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS" \
        "$SOURCE_CODE/" "$MIRROR_USER@$MIRROR_HOST:$MIRROR_CODE/"
}

sync_data() {
    echo "Syncing ownCloud data (the initial 438+ GB copy can take hours)..."
    rsync -rltp --delete --partial \
        --exclude=/.owncloud-mirror-managed \
        --info=progress2,stats1 \
        -e "ssh -i $SSH_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS" \
        "$SOURCE_DATA/" "$MIRROR_USER@$MIRROR_HOST:$MIRROR_DATA/"
}

sync_config() {
    echo "Syncing ownCloud configuration..."
    rsync -rltp --delete --partial \
        --exclude=/.owncloud-mirror-managed \
        --info=stats1 \
        -e "ssh -i $SSH_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS" \
        "$SOURCE_CODE/config/" "$MIRROR_USER@$MIRROR_HOST:$MIRROR_SOURCE_CONFIG/"
    remote "chmod 0700 '$MIRROR_SOURCE_CONFIG' && find '$MIRROR_SOURCE_CONFIG' -type f -exec chmod 0600 {} +"
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    die "Another ownCloud sync is running or the lock exists: $LOCK_DIR"
fi

SOURCE_MAINTENANCE_CHANGED=0
cleanup() {
    local exit_code=$?
    trap - EXIT
    [[ -z "$PGPASS_FILE" ]] || rm -f "$PGPASS_FILE"
    [[ -z "$DB_DUMP" ]] || rm -f "$DB_DUMP"
    if [[ "$SOURCE_MAINTENANCE_CHANGED" -eq 1 ]]; then
        if ! runuser -u www-data -- php "$SOURCE_OCC" maintenance:mode --off; then
            echo "ERROR: Could not disable source ownCloud maintenance mode." >&2
            exit_code=1
        fi
    fi
    rmdir "$LOCK_DIR"
    exit "$exit_code"
}
trap cleanup EXIT

[[ -d "$SOURCE_CODE" && -r "$SOURCE_CODE/version.php" ]] || die "Source ownCloud code is unavailable."
[[ -f "$SOURCE_DATA/.ocdata" ]] || die "Source ownCloud data marker is missing."
[[ -r "$SOURCE_CONFIG" && -f "$SOURCE_OCC" ]] || die "Source ownCloud config or occ is unavailable."
[[ -r "$SSH_KEY" && -r "$KNOWN_HOSTS" ]] || die "Source-to-mac2016 SSH key or known_hosts is unavailable."

SOURCE_VERSION="$(awk -F"'" '/OC_VersionString/ {print $2; exit}' "$SOURCE_CODE/version.php")"
[[ "$SOURCE_VERSION" == "10.13.4" ]] || die "Expected ownCloud 10.13.4, found ${SOURCE_VERSION:-unknown}."

for marker in \
    "$MIRROR_ROOT/.owncloud-mirror-managed" \
    "$MIRROR_CODE/.owncloud-mirror-managed" \
    "$MIRROR_DATA/.owncloud-mirror-managed" \
    "$MIRROR_SOURCE_CONFIG/.owncloud-mirror-managed"; do
    remote "test -f '$marker'" || die "Required destination safety marker is missing: $marker"
done
remote "test -f '$MIRROR_DATA/.ocdata' && test -d '$MIRROR_CODE' && test -d '$MIRROR_SOURCE_CONFIG'" \
    || die "Destination ownCloud data, code, or config folder is unavailable."
remote "docker compose -f '$COMPOSE_FILE' --profile owncloud up -d owncloud-db"
remote "for attempt in \$(seq 1 60); do
    docker compose -f '$COMPOSE_FILE' --profile owncloud exec -T owncloud-db pg_isready -U postgres -d postgres >/dev/null 2>&1 && exit 0
    sleep 2
done
echo 'OwnCloud mirror PostgreSQL did not become ready.' >&2
exit 1"

if remote "docker compose -f '$COMPOSE_FILE' --profile owncloud exec -T --user www-data owncloud php /var/www/owncloud/occ status >/dev/null 2>&1"; then
    remote "docker compose -f '$COMPOSE_FILE' --profile owncloud exec -T --user www-data owncloud php /var/www/owncloud/occ maintenance:mode --on"
fi

# Transfer the bulk of changed content while production remains available.
sync_code
sync_data
sync_config

SOURCE_MAINTENANCE="$(php -r '$CONFIG = []; include $argv[1]; echo !empty($CONFIG["maintenance"]) ? "1" : "0";' "$SOURCE_CONFIG")"
if [[ "$SOURCE_MAINTENANCE" != "1" ]]; then
    SOURCE_MAINTENANCE_CHANGED=1
    echo "Taking the final consistent ownCloud file and database snapshot..."
    runuser -u www-data -- php "$SOURCE_OCC" maintenance:mode --on
fi

sync_code
sync_data
sync_config

PGPASS_FILE="$(mktemp)"
DB_DUMP="$(mktemp)"
chmod 0600 "$PGPASS_FILE" "$DB_DUMP"
php -r '
$CONFIG = [];
include $argv[1];
if (($CONFIG["dbtype"] ?? "") !== "pgsql") {
    fwrite(STDERR, "Expected a PostgreSQL ownCloud database.\n");
    exit(1);
}
$fields = [
    $CONFIG["dbhost"] ?? "localhost",
    "*",
    $CONFIG["dbname"] ?? "",
    $CONFIG["dbuser"] ?? "",
    $CONFIG["dbpassword"] ?? "",
];
foreach ($fields as &$field) {
    $field = str_replace(["\\", ":"], ["\\\\", "\\:"], (string) $field);
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
' "$SOURCE_CONFIG" "$PGPASS_FILE"

DB_HOST="$(php -r '$CONFIG = []; include $argv[1]; echo $CONFIG["dbhost"] ?? "localhost";' "$SOURCE_CONFIG")"
DB_NAME="$(php -r '$CONFIG = []; include $argv[1]; echo $CONFIG["dbname"] ?? "";' "$SOURCE_CONFIG")"
DB_USER="$(php -r '$CONFIG = []; include $argv[1]; echo $CONFIG["dbuser"] ?? "";' "$SOURCE_CONFIG")"
[[ "$DB_HOST" == "localhost" || "$DB_HOST" == "127.0.0.1" ]] || die "Refusing unexpected source database host: $DB_HOST"
[[ "$DB_NAME" == "owncloud" && "$DB_USER" == "owncloud" ]] || die "Unexpected source ownCloud database name or user."

PGPASSFILE="$PGPASS_FILE" pg_dump \
    --format=custom \
    --no-owner \
    --no-privileges \
    --host="$DB_HOST" \
    --username="$DB_USER" \
    --dbname="$DB_NAME" \
    --file="$DB_DUMP"

if [[ "$SOURCE_MAINTENANCE_CHANGED" -eq 1 ]]; then
    echo "Source database snapshot complete; disabling source maintenance mode."
    runuser -u www-data -- php "$SOURCE_OCC" maintenance:mode --off
    SOURCE_MAINTENANCE_CHANGED=0
fi

remote "docker compose -f '$COMPOSE_FILE' --profile owncloud exec -T owncloud-db \
    psql -U postgres -d postgres -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='owncloud' AND pid <> pg_backend_pid();\" &&
    docker compose -f '$COMPOSE_FILE' --profile owncloud exec -T owncloud-db dropdb --if-exists -U postgres owncloud &&
    docker compose -f '$COMPOSE_FILE' --profile owncloud exec -T owncloud-db createdb -U postgres -O owncloud owncloud"

ssh -i "$SSH_KEY" \
    -o IdentitiesOnly=yes \
    -o BatchMode=yes \
    -o ConnectTimeout=15 \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$KNOWN_HOSTS" \
    "$MIRROR_USER@$MIRROR_HOST" \
    "export PATH='/Applications/OrbStack.app/Contents/MacOS/xbin:/usr/local/bin:/usr/bin:/bin'; \
     docker compose -f '$COMPOSE_FILE' --profile owncloud exec -T owncloud-db \
         pg_restore --exit-on-error --no-owner --no-privileges -U owncloud -d owncloud" \
    < "$DB_DUMP"

remote "docker compose -f '$COMPOSE_FILE' --profile owncloud run --rm --no-deps --user www-data owncloud php /usr/local/bin/prepare-owncloud-mirror-config"
remote "docker compose -f '$COMPOSE_FILE' --profile owncloud up -d owncloud"
remote "docker compose -f '$COMPOSE_FILE' --profile owncloud exec -T --user www-data owncloud php /var/www/owncloud/occ maintenance:mode --off"
remote "docker compose -f '$COMPOSE_FILE' --profile owncloud exec -T --user www-data owncloud php /var/www/owncloud/occ status --output=json"
remote "'$ACTIVATE_SCRIPT'"

echo "OwnCloud $SOURCE_VERSION files, database, config, and data synchronized to mac2016."
