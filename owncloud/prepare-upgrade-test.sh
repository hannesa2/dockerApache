#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="/daten/owncloud"
DISK="/media/hannes/T1"
FILES="$DISK/owncloud-staging/files"
STATE="$DISK/owncloud-upgrade-11.1"
ENV_FILE="$STATE/test.env"
COMPOSE="$SCRIPT_DIR/upgrade-test-compose.yml"
LOCK="/run/lock/owncloud-mirror-sync.lock"
LOCKED=0
SOURCE_STOPPED=0
MAINTENANCE_CHANGED=0
log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }
test_compose() {
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE" "$@"
}
source_occ() {
    docker exec --user www-data owncloud-staging-owncloud-1 php /var/www/owncloud/occ "$@"
}
source_status() {
    local response
    response="$(curl -fsS --max-time 15 http://127.0.0.1:8093/status.php)" ||
        fail "Production status request failed."
    python3 -c '
import json, sys
s = json.load(sys.stdin)
assert s.get("installed") and s.get("version") == "10.16.7.0"
assert not s.get("needsDbUpgrade") and not s.get("maintenance")
' <<< "$response"
}
wait_source_normal() {
    local attempt response
    local url="${1:-http://127.0.0.1:8093/status.php}"
    local version="${2:-10.16.7}"
    local host="${3:-}"
    local -a headers=()
    [[ -z "$host" ]] || headers=(--header "Host: $host")
    for attempt in {1..30}; do
        if response="$(curl --noproxy '*' "${headers[@]}" -fsS --max-time 5 "$url" 2>/dev/null)" &&
            python3 -c '
import json, sys
s = json.load(sys.stdin)
sys.exit(0 if s.get("installed") and s.get("versionstring") == sys.argv[1]
         and not s.get("needsDbUpgrade") and not s.get("maintenance") else 1)
' "$version" <<< "$response"; then
            return 0
        fi
        sleep 2
    done
    fail "ownCloud at $url did not leave maintenance within one minute."
}
wait_source() {
    local attempt
    for attempt in {1..120}; do
        if curl -fsS --max-time 5 http://127.0.0.1:8093/status.php >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
    done
    return 1
}
restore_source() {
    if (( SOURCE_STOPPED )); then
        docker start owncloud-staging-owncloud-1
        wait_source || fail "Production did not restart within ten minutes."
        SOURCE_STOPPED=0
    fi
    if (( MAINTENANCE_CHANGED )); then
        source_occ maintenance:mode --off
        wait_source_normal
        MAINTENANCE_CHANGED=0
    fi
}
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if (( SOURCE_STOPPED || MAINTENANCE_CHANGED )); then
        log "Restoring production after snapshot."
        if ! restore_source; then
            log "ERROR: Production recovery failed. Inspect owncloud-staging-owncloud-1." >&2
            status=1
        fi
    fi
    if (( status != 0 )) && [[ -f "$ENV_FILE" ]]; then
        if ! test_compose stop; then
            log "ERROR: Could not stop the failed test stack." >&2
        fi
        log "Partial test state retained at $STATE; do not blindly rerun preparation." >&2
    fi
    if (( LOCKED )); then
        rmdir "$LOCK" || status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

(( EUID == 0 )) || fail "Run directly on latitude with sudo."
[[ ! -e /media/hannes/T1/owncloud-production-pre-11.1 ]] ||
    fail "T1 files were reserved for the production backup; do not reuse them for a test."
MODE="${1:---preflight}"
[[ "$MODE" == "--preflight" || "$MODE" == "--prepare" || "$MODE" == "--resume-snapshot" ]] ||
    fail "Usage: $0 [--preflight|--prepare|--resume-snapshot]"
(( $# <= 1 )) || fail "Usage: $0 [--preflight|--prepare|--resume-snapshot]"
for tool in docker rsync mountpoint findmnt curl python3 openssl ss; do
    command -v "$tool" >/dev/null || fail "Missing tool: $tool"
done
mountpoint -q /daten || fail "/daten is not mounted."
mountpoint -q "$DISK" || fail "T1 must be mounted for the isolated test."
[[ "$(findmnt -n -o FSTYPE -T "$DISK")" == "ext4" ]] || fail "Expected ext4 T1."
[[ -f "$SOURCE/data/.ocdata" && -f "$FILES/.ocdata" &&
    -f "$SOURCE/config/config.php" && -f "$SOURCE/production.env" ]] ||
    fail "Production or old T1 file data is incomplete."
if [[ "$MODE" == "--resume-snapshot" ]]; then
    [[ -s "$STATE/source.dump" && -f "$STATE/config/config.php" &&
        -d "$STATE/apps" && -f "$ENV_FILE" && -f "$STATE/prepare.log" &&
        ! -e "$STATE/database" && ! -e "$STATE/runtime" ]] ||
        fail "Cannot resume: completed snapshot required and test must not have started."
    grep -q 'Taking final consistent snapshot with the production web container stopped.' "$STATE/prepare.log" ||
        fail "Snapshot log does not confirm the stopped-source snapshot phase."
else
    [[ ! -e "$STATE" ]] || fail "Test state already exists; do not repeat preparation."
fi
for path in "$SOURCE" "$SOURCE/data" "$FILES"; do
    [[ "$(readlink -f "$path")" == "$path" ]] || fail "Unexpected symlink: $path"
done
[[ -z "$(docker ps -aq --filter label=com.docker.compose.project=owncloud-upgrade-test)" ]] ||
    fail "A previous upgrade test already exists."
[[ -z "$(ss -H -ltn 'sport = :8094')" ]] || fail "Test port 8094 is already in use."
if [[ -f /etc/cron.d/owncloud-mirror-sync ]] &&
    grep -Eq '^[[:space:]]*[^#[:space:]].*owncloud-mirror-sync' /etc/cron.d/owncloud-mirror-sync; then
    fail "Disable mirror sync during test preparation."
fi
mkdir "$LOCK" || fail "Another ownCloud operation holds $LOCK."
LOCKED=1
for mapping in 'owncloud /mnt/data/files data' 'owncloud /mnt/data/config config' \
    'owncloud /mnt/data/apps apps' 'db /var/lib/postgresql/data database'; do
    read -r service destination directory <<< "$mapping"
    actual="$(docker inspect -f '{{range .Mounts}}{{.Destination}} {{.Source}}{{println}}{{end}}' \
        "owncloud-staging-$service-1" | awk -v d="$destination" '$1 == d {print $2}')"
    [[ "$actual" == "$SOURCE/$directory" ]] || fail "Unexpected production mount: $destination"
done
source_status
docker exec --user www-data owncloud-staging-owncloud-1 php -r '
include "/var/www/owncloud/config/config.php";
if (!empty($CONFIG["objectstore"])) {
    fwrite(STDERR, "Object-store production cannot be cloned by copying local files.\n");
    exit(1);
}
'
if [[ "$MODE" != "--resume-snapshot" ]]; then
log "Read-only comparison; --prepare will update/delete old T1 files to match current production."
comparison="$(rsync -aHAXn --numeric-ids --delete --stats "$SOURCE/data/" "$FILES/")"
printf '%s\n' "$comparison"
bytes="$(printf '%s\n' "$comparison" |
    awk '/^Total transferred file size:/ {gsub(/,/, "", $5); print $5}')"
[[ "$bytes" =~ ^[0-9]+$ ]] || fail "Cannot determine changed file size."
db_bytes="$(du -sb "$SOURCE/database" | awk '{print $1}')"
available="$(df -B1 --output=avail "$DISK" | tail -1 | tr -d ' ')"
(( available > 2 * bytes + 3 * db_bytes + 10 * 1024 * 1024 * 1024 )) ||
    fail "Not enough T1 space for changed files, database, dump, and reserve."
TEST_DB_PASSWORD=validation-only TEST_STATE_ROOT="$STATE" TEST_FILES="$FILES" \
    docker compose -f "$COMPOSE" config --quiet
log "Preflight passed; production unchanged."
[[ "$MODE" == "--prepare" ]] || exit 0
fi

for image in owncloud/server:11.1.0 postgres:16-alpine redis:7-alpine; do
    if docker image inspect "$image" >/dev/null 2>&1; then
        log "Using locally available image: $image"
    else
        log "Pulling missing image: $image"
        docker pull "$image"
    fi
done
docker run --rm --pull never --network none --entrypoint php owncloud/server:11.1.0 -r \
    'exit(PHP_MAJOR_VERSION === 8 && PHP_MINOR_VERSION === 3 ? 0 : 1);'
if [[ "$MODE" != "--resume-snapshot" ]]; then
mkdir -m 0700 "$STATE"
LOG="$STATE/prepare.log"
exec > >(tee -a "$LOG") 2>&1
printf 'TEST_STATE_ROOT=%s\nTEST_FILES=%s\nTEST_DB_PASSWORD=%s\n' \
    "$STATE" "$FILES" "$(openssl rand -hex 32)" > "$ENV_FILE"
test_compose config --quiet
log "Refreshing old T1 files while production is online. T1 is no longer the old rollback snapshot."
rsync -aHAX --numeric-ids --delete --stats "$SOURCE/data/" "$FILES/"
MAINTENANCE_CHANGED=1
source_occ maintenance:mode --on
SOURCE_STOPPED=1
docker stop --timeout 120 owncloud-staging-owncloud-1
log "Taking final consistent snapshot with the production web container stopped."
rsync -aHAX --numeric-ids --delete --stats "$SOURCE/data/" "$FILES/"
docker exec owncloud-staging-db-1 pg_dump -Fc -U owncloud_stage owncloud_stage > "$STATE/source.dump"
docker exec -i owncloud-staging-db-1 pg_restore --list < "$STATE/source.dump" >/dev/null
for directory in config apps; do
    mkdir "$STATE/$directory"
    rsync -aHAX --numeric-ids "$SOURCE/$directory/" "$STATE/$directory/"
done
restore_source
else
    test_compose config --quiet
    docker exec -i owncloud-staging-db-1 pg_restore --list < "$STATE/source.dump" >/dev/null
    docker run --rm --pull never --network none --read-only \
        --mount "type=bind,source=$STATE/config,target=/snapshot,readonly" \
        --entrypoint php owncloud/server:11.1.0 -r '
include "/snapshot/config.php";
if (empty($CONFIG["installed"]) || ($CONFIG["maintenance"] ?? false) !== true ||
    !str_starts_with($CONFIG["version"] ?? "", "10.16.7")) {
    fwrite(STDERR, "Snapshot is not the unmodified 10.16.7 maintenance snapshot.\n");
    exit(1);
}'
    LOG="$STATE/prepare.log"
    exec > >(tee -a "$LOG") 2>&1
    log "Resuming the completed snapshot only; production and T1 files are not recopied."
fi
log "Production is online again. All following operations affect only the isolated test."

docker run --rm -i --pull never --network none --env-file "$ENV_FILE" \
    --mount "type=bind,source=$STATE/config,target=/test-config" \
    --entrypoint php owncloud/server:11.1.0 <<'PHP'
<?php
include '/test-config/config.php';
if (!isset($CONFIG) || !is_array($CONFIG) || empty($CONFIG['installed'])) {
    fwrite(STDERR, "Source config is not an installed ownCloud.\n");
    exit(1);
}
foreach (['overwritehost', 'overwriteprotocol', 'overwritewebroot',
    'overwritecondaddr', 'trusted_proxies'] as $key) {
    unset($CONFIG[$key]);
}
$CONFIG['overwrite.cli.url'] = 'http://localhost:8094';
$CONFIG['trusted_domains'] = ['localhost:8094', 'localhost', '127.0.0.1'];
$CONFIG['dbhost'] = 'db';
$CONFIG['dbname'] = 'owncloud_stage';
$CONFIG['dbuser'] = 'owncloud_stage';
$CONFIG['dbpassword'] = getenv('TEST_DB_PASSWORD');
$CONFIG['datadirectory'] = '/mnt/data/files';
$CONFIG['redis'] = ['host' => 'redis', 'port' => 6379];
$CONFIG['maintenance'] = false;
$CONFIG['apps_paths'] = [
    ['path' => '/var/www/owncloud/apps', 'url' => '/apps', 'writable' => false],
    ['path' => '/var/www/owncloud/custom', 'url' => '/custom', 'writable' => true],
];
if (file_put_contents('/test-config/config.php', "<?php\n\$CONFIG = " .
    var_export($CONFIG, true) . ";\n") === false) {
    fwrite(STDERR, "Could not write isolated config.\n");
    exit(1);
}
if (file_exists('/test-config/overwrite.config.php') &&
    !unlink('/test-config/overwrite.config.php')) {
    fwrite(STDERR, "Could not remove source environment override.\n");
    exit(1);
}
PHP
test_compose up -d --pull never --wait --wait-timeout 180 db redis
test_compose exec -T db pg_restore --exit-on-error --no-owner \
    -U owncloud_stage -d owncloud_stage < "$STATE/source.dump"
test_compose exec -T db psql -X -v ON_ERROR_STOP=1 \
    -U owncloud_stage -d owncloud_stage < "$SCRIPT_DIR/repair-test-homes.sql"
test_compose up -d --pull never owncloud
TEST_IP="$(docker inspect -f '{{with index .NetworkSettings.Networks "owncloud-upgrade-test_isolated"}}{{.IPAddress}}{{end}}' \
    owncloud-upgrade-test-owncloud-1)"
[[ -n "$TEST_IP" ]] || fail "Test has no isolated-network address."
TEST_URL="http://$TEST_IP:8080/status.php"
log "Waiting up to twenty minutes for the isolated 11.1 upgrade; detailed errors remain in test container logs."
ready=0
for attempt in {1..240}; do
    response="$(curl --noproxy '*' --header 'Host: localhost:8094' \
        -fsS --max-time 5 "$TEST_URL" 2>/dev/null)" || response=""
    if [[ -n "$response" ]] && python3 -c '
import json, sys
s = json.load(sys.stdin)
sys.exit(0 if s.get("installed") and s.get("versionstring") == "11.1.0"
         and not s.get("needsDbUpgrade") else 1)
' <<< "$response"; then
        ready=1
        break
    fi
    [[ "$(docker inspect -f '{{.State.Running}}' owncloud-upgrade-test-owncloud-1)" == "true" ]] ||
        fail "Upgrade test container exited. Inspect its logs."
    sleep 5
done
(( ready )) || fail "Upgrade did not become ready. Inspect test logs; production remains 10.16.7."
test_compose exec -T --user www-data owncloud php /var/www/owncloud/occ maintenance:mode --off
wait_source_normal "$TEST_URL" 11.1.0 localhost:8094
response="$(curl --noproxy '*' --header 'Host: localhost:8094' -fsS --max-time 15 "$TEST_URL")" ||
    fail "Test status request failed after disabling maintenance."
python3 -c '
import json, sys
s = json.load(sys.stdin)
assert s.get("installed") and s.get("versionstring") == "11.1.0"
assert not s.get("needsDbUpgrade") and not s.get("maintenance")
' <<< "$response"
test_compose exec -T --user www-data owncloud php /var/www/owncloud/occ status --output=json
test_compose exec -T --user www-data owncloud php /var/www/owncloud/occ app:list
source_status
log "Test access from your client: ssh -N -L 127.0.0.1:8094:$TEST_IP:8080 latitude"
log "Open http://localhost:8094. Production remains 10.16.7 on internal storage."
log "Internet, mail, external storage and OnlyOffice are intentionally unreachable from the isolated test."
log "This is not a production cutover. Test accounts, encrypted files, contacts, downloads, uploads and app compatibility."
