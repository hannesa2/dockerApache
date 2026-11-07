#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C
umask 077

if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: Run this backup as root on latitude." >&2
    exit 1
fi

SOURCE_ROOT="/daten/owncloud"
SOURCE_DATA="$SOURCE_ROOT/data"
SOURCE_APPS="$SOURCE_ROOT/apps"
SOURCE_CONFIG="$SOURCE_ROOT/config"
SOURCE_ENV="$SOURCE_ROOT/production.env"
SOURCE_COMPOSE="/home/hannes/git/dockerApache/owncloud/latitude-compose.yml"
SOURCE_PROJECT="owncloud-staging"
SOURCE_WEB="owncloud-staging-owncloud-1"
SOURCE_DB="owncloud-staging-db-1"

MIRROR_HOST="mac2016"
MIRROR_USER="hannes"
MIRROR_REPO="/Users/hannes/git/dockerApache"
MIRROR_ROOT="/Volumes/SamsungT7_2T/ownCloudBackup/owncloud"
MIRROR_CODE="$MIRROR_ROOT/owncloud"
MIRROR_DATA="$MIRROR_ROOT/data"
MIRROR_APPS="$MIRROR_ROOT/apps"
MIRROR_SOURCE_CONFIG="$MIRROR_ROOT/source-config"
MIRROR_CODE_SOURCE_IMAGE="$MIRROR_ROOT/.owncloud-mirror-source-image"
MIRROR_COMPOSE="$MIRROR_REPO/apache/docker-compose.yml"
SSH_KEY="/home/hannes/.ssh/id_rsa"
KNOWN_HOSTS="/home/hannes/.ssh/known_hosts"
LOCK_DIR="/run/lock/owncloud-mirror-sync.lock"

TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
SOURCE_DUMP="$SOURCE_ROOT/backup/mirror-sync-$TIMESTAMP.dump"
MIRROR_DB_BACKUP="$MIRROR_ROOT/backups/pre-sync-$TIMESTAMP.dump"
CODE_STAGE="$MIRROR_ROOT/owncloud-code-$TIMESTAMP"
CODE_ROLLBACK="$MIRROR_ROOT/owncloud-pre-sync-$TIMESTAMP"
LOCK_ACQUIRED=0
SOURCE_MAINTENANCE_CHANGED=0

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

remote_compose() {
    remote "cd '$MIRROR_REPO' && docker compose --env-file apache/.env -f '$MIRROR_COMPOSE' --profile owncloud $1"
}

RSYNC_SSH="ssh -i $SSH_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS"

sync_data() {
    echo "Syncing ownCloud files to mac2016..."
    rsync -rltp --delete --partial \
        --iconv=ISO-8859-1,UTF-8 \
        --rsync-path=/usr/local/bin/rsync \
        --exclude=/.owncloud-mirror-managed \
        --exclude=/sessions/ \
        --info=stats1 \
        -e "$RSYNC_SSH" \
        "$SOURCE_DATA/" "$MIRROR_USER@$MIRROR_HOST:$MIRROR_DATA/"
}

sync_apps() {
    echo "Syncing writable ownCloud apps to mac2016..."
    rsync -rltp --delete --partial \
        --rsync-path=/usr/local/bin/rsync \
        --exclude=/.owncloud-mirror-managed \
        --info=stats1 \
        -e "$RSYNC_SSH" \
        "$SOURCE_APPS/" "$MIRROR_USER@$MIRROR_HOST:$MIRROR_APPS/"
}

sync_config() {
    echo "Syncing the Docker ownCloud configuration to mac2016..."
    remote "mkdir -p '$MIRROR_SOURCE_CONFIG'"
    rsync -rltp --delete --partial \
        --rsync-path=/usr/local/bin/rsync \
        --exclude=/.owncloud-mirror-managed \
        --info=stats1 \
        -e "$RSYNC_SSH" \
        "$SOURCE_CONFIG/" "$MIRROR_USER@$MIRROR_HOST:$MIRROR_SOURCE_CONFIG/"
    remote "touch '$MIRROR_SOURCE_CONFIG/.owncloud-mirror-managed' && chmod 0700 '$MIRROR_SOURCE_CONFIG' && find '$MIRROR_SOURCE_CONFIG' -type f -exec chmod 0600 {} +"
}

sync_code() {
    echo "Streaming ownCloud 10.16.7 application code from the running Docker image..."
    remote "mkdir '$CODE_STAGE'"
    docker exec "$SOURCE_WEB_ID" tar \
        --exclude=./config \
        --exclude=./custom \
        --exclude=./data \
        -C /var/www/owncloud -cf - . |
        ssh -i "$SSH_KEY" \
            -o IdentitiesOnly=yes \
            -o BatchMode=yes \
            -o ConnectTimeout=15 \
            -o StrictHostKeyChecking=yes \
            -o UserKnownHostsFile="$KNOWN_HOSTS" \
            "$MIRROR_USER@$MIRROR_HOST" \
            "export PATH='/Applications/OrbStack.app/Contents/MacOS/xbin:/usr/local/bin:/usr/bin:/bin'; tar -xpf - -C '$CODE_STAGE'"

    remote "set -e
        VERSION=\$(grep -oE \"OC_VersionString = '[0-9.]+\" '$CODE_STAGE/version.php' | grep -oE '[0-9.]+$')
        [ \"\$VERSION\" = '10.16.7' ]
        mkdir -p '$CODE_STAGE/config' '$CODE_STAGE/custom'
        touch '$CODE_STAGE/.owncloud-mirror-managed'
        if [ -e '$CODE_ROLLBACK' ]; then
            echo 'Rollback code path already exists; refusing to overwrite it.' >&2
            exit 1
        fi
        mv '$MIRROR_CODE' '$CODE_ROLLBACK'
        if ! mv '$CODE_STAGE' '$MIRROR_CODE'; then
            mv '$CODE_ROLLBACK' '$MIRROR_CODE'
            exit 1
        fi"
}

source_compose() {
    docker compose --env-file "$SOURCE_ENV" -f "$SOURCE_COMPOSE" "$@"
}

source_occ() {
    source_compose exec -T --user www-data owncloud /bin/bash -ec \
        'source /etc/owncloud.d/20-config.sh && exec /usr/bin/occ "$@"' owncloud-cli "$@"
}

cleanup() {
    local exit_code=$?
    trap - EXIT INT TERM
    if (( SOURCE_MAINTENANCE_CHANGED )); then
        echo "Restoring the production ownCloud from maintenance mode..." >&2
        if ! source_occ maintenance:mode --off; then
            echo "ERROR: Could not disable production ownCloud maintenance mode." >&2
            exit_code=1
        fi
    fi
    if (( LOCK_ACQUIRED )); then
        rmdir "$LOCK_DIR" || exit_code=1
    fi
    exit "$exit_code"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for command_name in docker rsync ssh mountpoint findmnt curl python3; do
    command -v "$command_name" >/dev/null || die "Required command is missing: $command_name"
done

[[ -f "$SOURCE_COMPOSE" && -f "$SOURCE_ENV" ]] || die "Latitude Docker ownCloud Compose files are missing."
[[ -d "$SOURCE_DATA" && -f "$SOURCE_DATA/.ocdata" ]] || die "Latitude Docker ownCloud data is unavailable."
[[ -d "$SOURCE_APPS" && -d "$SOURCE_CONFIG" && -f "$SOURCE_CONFIG/config.php" ]] ||
    die "Latitude Docker ownCloud apps or config are unavailable."
mountpoint -q /daten || die "/daten is not mounted."
[[ "$(findmnt -n -o FSTYPE --target /daten)" == "ext4" ]] ||
    die "Expected the internal production data disk to be ext4."

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    die "Another ownCloud sync or staging operation holds the lock: $LOCK_DIR"
fi
LOCK_ACQUIRED=1
mkdir -p "$SOURCE_ROOT/backup"
chmod 700 "$SOURCE_ROOT/backup"

SOURCE_WEB_ID="$(source_compose ps -q owncloud)"
SOURCE_DB_ID="$(source_compose ps -q db)"
[[ -n "$SOURCE_WEB_ID" && "$(docker inspect -f '{{.Name}}' "$SOURCE_WEB_ID" | sed 's#^/##')" == "$SOURCE_WEB" ]] ||
    die "The expected owncloud-staging web container is not running."
[[ -n "$SOURCE_DB_ID" && "$(docker inspect -f '{{.Name}}' "$SOURCE_DB_ID" | sed 's#^/##')" == "$SOURCE_DB" ]] ||
    die "The expected owncloud-staging PostgreSQL container is not running."
[[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$SOURCE_WEB_ID")" == "$SOURCE_PROJECT" ]] ||
    die "Production ownCloud is not from the expected Docker Compose project."
[[ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/mnt/data/files"}}{{.Source}}{{end}}{{end}}' "$SOURCE_WEB_ID")" == "$SOURCE_DATA" ]] ||
    die "Production ownCloud is not using the expected internal data directory."
[[ "$(docker inspect -f '{{.State.Health.Status}}' "$SOURCE_DB_ID")" == "healthy" ]] ||
    die "Production ownCloud PostgreSQL is not healthy."
SOURCE_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$SOURCE_WEB_ID")"
[[ "$SOURCE_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] ||
    die "Could not determine the immutable production ownCloud image ID."
SOURCE_VERSION="$(docker exec "$SOURCE_WEB_ID" grep -oE "OC_VersionString = '[0-9.]+" /var/www/owncloud/version.php | grep -oE '[0-9.]+$')"
[[ "$SOURCE_VERSION" == "10.16.7" ]] ||
    die "Expected Docker production ownCloud 10.16.7, found ${SOURCE_VERSION:-unknown}."
SOURCE_STATUS="$(curl -fsS --max-time 20 http://127.0.0.1:8093/status.php)" ||
    die "Production ownCloud is not healthy on loopback."
python3 -c 'import json,sys; s=json.loads(sys.argv[1]); assert s.get("installed") and s.get("version","").startswith("10.16.7") and not s.get("maintenance"), "Production ownCloud is not ready."' \
    "$SOURCE_STATUS" || die "Production ownCloud must be installed, upgraded, and out of maintenance mode."

[[ -r "$SSH_KEY" && -r "$KNOWN_HOSTS" ]] ||
    die "Latitude-to-mac2016 SSH key or pinned known_hosts file is unavailable."

for marker in \
    "$MIRROR_ROOT/.owncloud-mirror-managed" \
    "$MIRROR_CODE/.owncloud-mirror-managed" \
    "$MIRROR_DATA/.owncloud-mirror-managed"; do
    remote "test -f '$marker'" || die "Required managed mirror marker is missing: $marker"
done
remote "test -f '$MIRROR_DATA/.ocdata' && test -d '$MIRROR_CODE'" ||
    die "The existing mac2016 ownCloud mirror layout is incomplete."
REMOTE_RSYNC_VERSION="$(remote "/usr/local/bin/rsync --version" | sed -n '1s/.*version \([0-9][0-9]*\)\.\([0-9][0-9]*\).*/\1 \2/p')"
read -r REMOTE_RSYNC_MAJOR REMOTE_RSYNC_MINOR <<< "$REMOTE_RSYNC_VERSION"
[[ "$REMOTE_RSYNC_MAJOR" =~ ^[0-9]+$ ]] && (( REMOTE_RSYNC_MAJOR >= 3 )) ||
    die "mac2016 requires Homebrew rsync 3 or newer."

echo "Building the isolated ownCloud 10.16.7 mirror runtime on mac2016..."
remote_compose "config --quiet && build --pull owncloud"

echo "Checking mirror storage and saving its pre-upgrade database..."
remote "set -e
    /sbin/mount | /usr/bin/awk '\$3 == \"/Volumes/SamsungT7_2T\" { mounted = 1 } END { exit !mounted }'
    mkdir -p '$MIRROR_ROOT/backups' '$MIRROR_APPS'
    chmod 0700 '$MIRROR_ROOT/backups'
    touch '$MIRROR_APPS/.owncloud-mirror-managed'
    if [ ! -f '$MIRROR_DB_BACKUP' ]; then
        cd '$MIRROR_REPO'
        docker compose --env-file apache/.env -f '$MIRROR_COMPOSE' --profile owncloud exec -T owncloud-db pg_dump -Fc -U owncloud owncloud > '$MIRROR_DB_BACKUP'
        chmod 0600 '$MIRROR_DB_BACKUP'
    fi
    [ -s '$MIRROR_DB_BACKUP' ]"

echo "Putting the mac2016 standby in maintenance mode."
remote_compose "exec -T --user www-data owncloud php /var/www/owncloud/occ maintenance:mode --on"
remote_compose "stop owncloud"

sync_data
sync_apps
sync_config

echo "Enabling maintenance mode for the final consistent production snapshot."
SOURCE_MAINTENANCE_CHANGED=1
source_occ maintenance:mode --on
sync_data
sync_apps
sync_config

echo "Creating a restricted PostgreSQL snapshot from the production Docker database..."
source_compose exec -T db pg_dump --format=custom --no-owner --no-privileges \
    -U owncloud_stage -d owncloud_stage > "$SOURCE_DUMP"
chmod 0600 "$SOURCE_DUMP"
[[ -s "$SOURCE_DUMP" ]] || die "The production PostgreSQL snapshot is empty."

echo "Production snapshot complete; restoring the live service."
source_occ maintenance:mode --off
SOURCE_MAINTENANCE_CHANGED=0

MIRROR_CODE_IMAGE_ID="$(remote "cat '$MIRROR_CODE_SOURCE_IMAGE' 2>/dev/null || true")"
if [[ "$MIRROR_CODE_IMAGE_ID" != "$SOURCE_IMAGE_ID" ]]; then
    echo "The mac2016 code has not been verified against production image $SOURCE_IMAGE_ID."
    sync_code
fi

echo "Replacing the mac2016 ownCloud database with the production snapshot..."
remote_compose "exec -T owncloud-db psql -v ON_ERROR_STOP=1 -U postgres -d postgres -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='owncloud' AND pid <> pg_backend_pid();\""
remote_compose "exec -T owncloud-db dropdb --if-exists -U postgres owncloud"
remote_compose "exec -T owncloud-db createdb -U postgres -O owncloud owncloud"
cat "$SOURCE_DUMP" |
    ssh -i "$SSH_KEY" \
        -o IdentitiesOnly=yes \
        -o BatchMode=yes \
        -o ConnectTimeout=15 \
        -o StrictHostKeyChecking=yes \
        -o UserKnownHostsFile="$KNOWN_HOSTS" \
        "$MIRROR_USER@$MIRROR_HOST" \
        "export PATH='/Applications/OrbStack.app/Contents/MacOS/xbin:/usr/local/bin:/usr/bin:/bin'; cd '$MIRROR_REPO' && docker compose --env-file apache/.env -f '$MIRROR_COMPOSE' --profile owncloud exec -T owncloud-db pg_restore --exit-on-error --no-owner --no-privileges -U owncloud -d owncloud"

echo "Generating mirror-only config and starting ownCloud in standby maintenance mode..."
remote_compose "run --rm --no-deps --user www-data owncloud php /usr/local/bin/prepare-owncloud-mirror-config"
remote_compose "up -d --force-recreate owncloud"

MIRROR_STATUS="$(remote_compose "exec -T --user www-data owncloud php /var/www/owncloud/occ status --output=json")"
python3 -c 'import json,sys; s=json.loads(sys.argv[1]); assert s.get("installed") and s.get("version","").startswith("10.16.7") and s.get("maintenance"), "Mirror must be installed as 10.16.7 and remain in standby maintenance mode."' \
    "$MIRROR_STATUS" || die "The synchronized mac2016 ownCloud did not pass its standby status check."
remote_compose "exec -T apache apache2ctl -t"
remote "set -e
    printf '%s\n' '$SOURCE_IMAGE_ID' > '$MIRROR_CODE_SOURCE_IMAGE.tmp'
    chmod 0600 '$MIRROR_CODE_SOURCE_IMAGE.tmp'
    mv '$MIRROR_CODE_SOURCE_IMAGE.tmp' '$MIRROR_CODE_SOURCE_IMAGE'"

echo "OwnCloud 10.16.7 files, apps, config, and PostgreSQL are synchronized to mac2016."
echo "The mirror remains in maintenance mode and is not accepting writes; disable maintenance only during failover."
echo "Synchronized production image: $SOURCE_IMAGE_ID"
echo "Pre-upgrade mirror database backup: $MIRROR_DB_BACKUP"
echo "Last production database snapshot: $SOURCE_DUMP"
