#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C

if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: Run this mirror sync as root on latitude." >&2
    exit 1
fi

SOURCE_DATABASE="MXServer"
STAGING_DATABASE="MXServer_staging"
PREVIOUS_DATABASE="MXServer_previous"
SOURCE_PICTURES="/srv/postgres/data/mxinfo"
SOURCE_KEYFILE="/opt/glassfish-4.0/glassfish/domains/domain1/config/keyfile"
MIRROR_HOST="mac2016"
MIRROR_USER="hannes"
MIRROR_ROOT="/Volumes/SamsungT7_2T/MXServerMirror"
MIRROR_PICTURES="$MIRROR_ROOT/pictures"
MIRROR_KEYFILE="$MIRROR_ROOT/config/keyfile"
MIRROR_COMPOSE_FILE="/Users/hannes/git/dockerApache/apache/docker-compose.yml"
ACTIVATE_SCRIPT="/Users/hannes/git/dockerApache/mxserver/activate-proxy.sh"
SSH_KEY="/home/hannes/.ssh/id_rsa"
KNOWN_HOSTS="/home/hannes/.ssh/known_hosts"
LOCK_DIR="/run/lock/mxserver-mirror-sync.lock"

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

ssh_options=(
    -i "$SSH_KEY"
    -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o ConnectTimeout=15
    -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile="$KNOWN_HOSTS"
)

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    die "Another MXServer mirror sync is running or the lock exists: $LOCK_DIR"
fi

mirror_app_stopped=0
mirror_keyfile_staged=0
cleanup() {
    status=$?
    trap - EXIT
    if [[ "$mirror_keyfile_staged" -eq 1 ]]; then
        if ! remote "rm -f '$MIRROR_KEYFILE.new'"; then
            echo "ERROR: Could not remove the staged mirror keyfile." >&2
            status=1
        fi
    fi
    if [[ "$mirror_app_stopped" -eq 1 ]]; then
        if remote "docker compose -f '$MIRROR_COMPOSE_FILE' --profile mxserver up -d mxserver"; then
            echo "Restarted MXServer after an interrupted mirror sync." >&2
        else
            echo "ERROR: Could not restart MXServer after the interrupted sync." >&2
            status=1
        fi
    fi
    rmdir "$LOCK_DIR" || status=1
    exit "$status"
}
trap cleanup EXIT

[[ -d "$SOURCE_PICTURES" ]] || die "Source picture directory is unavailable: $SOURCE_PICTURES"
[[ -r "$SOURCE_KEYFILE" ]] || die "GlassFish file-realm keyfile is unavailable: $SOURCE_KEYFILE"
[[ -r "$SSH_KEY" && -r "$KNOWN_HOSTS" ]] || die "Latitude-to-mac2016 SSH key or known_hosts is unavailable."
command -v pg_dump >/dev/null || die "pg_dump is unavailable."
command -v rsync >/dev/null || die "rsync is unavailable on latitude."
command -v runuser >/dev/null || die "runuser is unavailable."

remote "test -f '$MIRROR_ROOT/.mxserver-mirror-managed' &&
        test -f '$MIRROR_PICTURES/.mxserver-mirror-managed' &&
        test -x /usr/local/bin/rsync" \
    || die "MXServer mirror markers or rsync 3+ are missing on mac2016."
remote "docker compose -f '$MIRROR_COMPOSE_FILE' --profile mxserver up -d mxserver-db mxserver-mail"
remote "for attempt in \$(seq 1 60); do
    docker compose -f '$MIRROR_COMPOSE_FILE' --profile mxserver exec -T mxserver-db pg_isready -U postgres -d postgres >/dev/null 2>&1 && exit 0
    sleep 2
done
echo 'MXServer mirror PostgreSQL did not become ready.' >&2
exit 1"

echo "Syncing MXServer picture files..."
rsync -rlt --delete --partial \
    --exclude=/.mxserver-mirror-managed \
    --chmod=Du=rwx,Fu=rw \
    --rsync-path=/usr/local/bin/rsync \
    --info=progress2,stats1 \
    -e "ssh -i $SSH_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS" \
    "$SOURCE_PICTURES/" "$MIRROR_USER@$MIRROR_HOST:$MIRROR_PICTURES/"

mirror_keyfile_staged=1
scp -i "$SSH_KEY" \
    -o IdentitiesOnly=yes \
    -o BatchMode=yes \
    -o ConnectTimeout=15 \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$KNOWN_HOSTS" \
    "$SOURCE_KEYFILE" "$MIRROR_USER@$MIRROR_HOST:$MIRROR_KEYFILE.new"
remote "chmod 0600 '$MIRROR_KEYFILE.new'"
remote "docker compose -f '$MIRROR_COMPOSE_FILE' --profile mxserver exec -T mxserver-db \
    psql -v ON_ERROR_STOP=1 -U postgres -d postgres \
    -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='MXServer_staging' AND pid <> pg_backend_pid()\" \
    -c 'DROP DATABASE IF EXISTS \"MXServer_staging\"' \
    -c 'CREATE DATABASE \"MXServer_staging\" OWNER \"mxserver\"'"

echo "Streaming the MXServer PostgreSQL snapshot to mac2016..."
runuser -u postgres -- pg_dump \
    --format=plain \
    --no-owner \
    --no-privileges \
    --dbname="$SOURCE_DATABASE" |
    ssh "${ssh_options[@]}" "$MIRROR_USER@$MIRROR_HOST" \
        "export PATH='/Applications/OrbStack.app/Contents/MacOS/xbin:/usr/local/bin:/usr/bin:/bin'; \
         docker compose -f '$MIRROR_COMPOSE_FILE' --profile mxserver exec -T mxserver-db \
             psql --set ON_ERROR_STOP=1 -U mxserver -d MXServer_staging"

remote "docker compose -f '$MIRROR_COMPOSE_FILE' --profile mxserver stop mxserver"
mirror_app_stopped=1
remote "mv '$MIRROR_KEYFILE.new' '$MIRROR_KEYFILE'"
mirror_keyfile_staged=0

remote "set -e
docker compose -f '$MIRROR_COMPOSE_FILE' --profile mxserver exec -T mxserver-db \
    psql -v ON_ERROR_STOP=1 -U postgres -d postgres \
    -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('MXServer', 'MXServer_previous') AND pid <> pg_backend_pid()\" \
    -c 'DROP DATABASE IF EXISTS \"MXServer_previous\"' \
    -c 'ALTER DATABASE \"MXServer\" RENAME TO \"MXServer_previous\"'
if ! docker compose -f '$MIRROR_COMPOSE_FILE' --profile mxserver exec -T mxserver-db \
    psql -v ON_ERROR_STOP=1 -U postgres -d postgres \
    -c 'ALTER DATABASE \"MXServer_staging\" RENAME TO \"MXServer\"'; then
    docker compose -f '$MIRROR_COMPOSE_FILE' --profile mxserver exec -T mxserver-db \
        psql -v ON_ERROR_STOP=1 -U postgres -d postgres \
        -c 'ALTER DATABASE \"MXServer_previous\" RENAME TO \"MXServer\"' ||
        echo 'ERROR: Could not roll back the MXServer database rename.' >&2
    exit 1
fi
docker compose -f '$MIRROR_COMPOSE_FILE' --profile mxserver exec -T mxserver-db \
    psql -v ON_ERROR_STOP=1 -U postgres -d postgres \
    -c 'DROP DATABASE \"MXServer_previous\"'"

remote "docker compose -f '$MIRROR_COMPOSE_FILE' --profile mxserver up -d mxserver"
mirror_app_stopped=0
remote "'$ACTIVATE_SCRIPT'"

echo "MXServer database and pictures synchronized to mac2016."
