#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="/daten/owncloud"
ENV_FILE="$ROOT/production.env"
DISK="/media/hannes/T1"
FILES="$DISK/owncloud-staging/files"
BACKUP="$DISK/owncloud-production-pre-11.1"
LOCK="/run/lock/owncloud-mirror-sync.lock"
IMAGE="owncloud/server:10.16.7"
LOCKED=0
CHANGED=0
RELEASED=0
MODE="${1:---preflight}"
log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }
compose() {
    OWNCLOUD_IMAGE="$IMAGE" docker compose --env-file "$ENV_FILE" \
        -f "$SCRIPT_DIR/latitude-compose.yml" "$@"
}
occ() {
    docker exec --user www-data owncloud-staging-owncloud-1 \
        php /var/www/owncloud/occ "$@"
}
status_matches() {
    python3 -c '
import json,sys
s=json.load(sys.stdin)
sys.exit(0 if s.get("installed") and s.get("versionstring") == sys.argv[1]
    and not s.get("needsDbUpgrade")
    and s.get("maintenance") is (sys.argv[2] == "on") else 1)
' "$1" "$2"
}
check_http() {
    curl --noproxy '*' -fsS --max-time 15 \
        --resolve www.mxtracks.info:443:127.0.0.1 "$1" |
        status_matches "$2" "$3"
}
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if (( status != 0 && CHANGED && ! RELEASED )); then
        docker stop --timeout 60 owncloud-staging-owncloud-1 >/dev/null ||
            log "ERROR: Could not stop production web container." >&2
        log "Production retained offline. Do not start the old image against the upgraded database." >&2
        log "Inspect $BACKUP/upgrade.log; restore DB/config/apps together before any rollback." >&2
    elif (( status != 0 && RELEASED )); then
        log "Production may accept writes. Inspect it; no automatic rollback is safe." >&2
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
[[ "$MODE" == "--preflight" || "$MODE" == "--apply" ]] && (( $# <= 1 )) ||
    fail "Usage: $0 [--preflight|--apply]"
for tool in docker curl python3 rsync tar mountpoint findmnt sha256sum; do
    command -v "$tool" >/dev/null || fail "Missing tool: $tool"
done
mountpoint -q /daten && mountpoint -q "$DISK" || fail "Both production and T1 must be mounted."
[[ "$(findmnt -n -o SOURCE -T "$ROOT")" != "$(findmnt -n -o SOURCE -T "$FILES")" ]] ||
    fail "Backup must be on a different filesystem."
for path in "$ROOT" "$ROOT/data" "$FILES"; do
    [[ "$(readlink -f "$path")" == "$path" ]] || fail "Unexpected symlink: $path"
done
[[ -f "$ROOT/data/.ocdata" && -f "$FILES/.ocdata" && -f "$ENV_FILE" ]] ||
    fail "Production or approved T1 backup files are incomplete."
[[ ! -e "$BACKUP" ]] || fail "A prior upgrade backup exists. Inspect it; do not repeat the upgrade."
[[ ! -e "$LOCK" ]] || fail "Another ownCloud operation holds the lock."
if [[ -f /etc/cron.d/owncloud-mirror-sync ]] &&
    grep -Eq '^[[:space:]]*[^#[:space:]].*owncloud-mirror-sync' /etc/cron.d/owncloud-mirror-sync; then
    fail "Disable the mirror sync job before cutover."
fi
apache2ctl -t
compose config --quiet
[[ "$(docker inspect -f '{{.Config.Image}}' owncloud-staging-owncloud-1)" == "$IMAGE" ]] ||
    fail "Expected production image 10.16.7."
for mapping in 'owncloud /mnt/data/files data' 'owncloud /mnt/data/config config' \
    'owncloud /mnt/data/apps apps' 'db /var/lib/postgresql/data database'; do
    read -r service destination directory <<< "$mapping"
    actual="$(docker inspect -f '{{range .Mounts}}{{.Destination}} {{.Source}}{{println}}{{end}}' \
        "owncloud-staging-$service-1" | awk -v d="$destination" '$1 == d {print $2}')"
    [[ "$actual" == "$ROOT/$directory" ]] || fail "Unexpected production $destination mount."
done
check_http http://127.0.0.1:8093/status.php 10.16.7 off
check_http https://www.mxtracks.info/owncloud/status.php 10.16.7 off
docker image inspect owncloud/server:11.1.0 >/dev/null
[[ "$(docker image inspect -f '{{.Id}}' owncloud/server:10.16.7)" == \
    "$(docker inspect -f '{{.Image}}' owncloud-staging-owncloud-1)" ]] ||
    fail "Local rollback image does not match the running production image."
ARCHIVE="/home/hannes/contacts-1.6.0.tar.gz"
[[ "$(sha256sum "$ARCHIVE" | awk '{print $1}')" == \
    "742b08df8e1d0c095ae12c141e0719d0d9ed694237739d6570e611bed9ba2d4b" ]] ||
    fail "Verified Contacts 1.6.0 archive is required."
python3 - "$ARCHIVE" <<'PY'
import sys,tarfile
from pathlib import PurePosixPath
with tarfile.open(sys.argv[1]) as archive:
    for member in archive:
        p=PurePosixPath(member.name)
        if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] != "contacts" or not (member.isdir() or member.isfile()):
            raise SystemExit("Unsafe Contacts archive")
PY
log "Read-only backup comparison (apply will refresh/delete approved T1 test files)."
comparison="$(rsync -aHAXn --numeric-ids --delete --stats "$ROOT/data/" "$FILES/")"
printf '%s\n' "$comparison"
bytes="$(awk '/^Total transferred file size:/ {gsub(/,/, "", $5); print $5}' <<< "$comparison")"
[[ "$bytes" =~ ^[0-9]+$ ]] || fail "Cannot calculate backup space."
metadata="$(du -sb "$ROOT/database" "$ROOT/config" "$ROOT/apps" | awk '{total+=$1} END {print total}')"
available="$(df -B1 --output=avail "$DISK" | tail -1 | tr -d ' ')"
(( available > bytes + 3 * metadata + 5 * 1024 * 1024 * 1024 )) ||
    fail "Insufficient T1 space for current backup and reserve."
log "Preflight passed. No production changes."
[[ "$MODE" == "--apply" ]] || exit 0

mkdir "$LOCK"
LOCKED=1
mkdir -m 0700 "$BACKUP"
exec > >(tee -a "$BACKUP/upgrade.log") 2>&1
cp "$ENV_FILE" "$BACKUP/production.env"
cp "$SCRIPT_DIR/latitude-compose.yml" "$BACKUP/latitude-compose.yml"
docker inspect owncloud-staging-owncloud-1 > "$BACKUP/old-container.json"
docker image inspect owncloud/server:10.16.7 > "$BACKUP/old-image.json"
if docker container inspect owncloud-upgrade-test-owncloud-1 >/dev/null 2>&1; then
    docker stop --timeout 60 owncloud-upgrade-test-owncloud-1
fi
log "Refreshing approved T1 files online; the previous test snapshot is being consumed."
rsync -aHAX --numeric-ids --delete "$ROOT/data/" "$FILES/"
CHANGED=1
occ maintenance:mode --on
docker stop --timeout 60 owncloud-staging-owncloud-1
log "Taking final offline file backup and consistent production database/config/apps snapshot."
rsync -aHAX --numeric-ids --delete "$ROOT/data/" "$FILES/"
remaining="$(rsync -aHAXn --numeric-ids --delete --itemize-changes "$ROOT/data/" "$FILES/")"
[[ -z "$remaining" ]] || fail "Final backup file comparison differs; production retained offline."
docker exec owncloud-staging-db-1 pg_dump -Fc -U owncloud_stage owncloud_stage > "$BACKUP/database.dump"
docker exec -i owncloud-staging-db-1 pg_restore --list < "$BACKUP/database.dump" > "$BACKUP/database.contents"
tar -czf "$BACKUP/config-apps.tar.gz" -C "$ROOT" config apps
tar -tzf "$BACKUP/config-apps.tar.gz" >/dev/null
sha256sum "$BACKUP/database.dump" "$BACKUP/config-apps.tar.gz" > "$BACKUP/SHA256SUMS"
printf '%s\n' "$FILES" > "$BACKUP/files-path"
touch "$BACKUP/backup-complete"
log "Independent T1 backup completed. Beginning production-only upgrade."

if [[ -d "$ROOT/apps/contacts" ]]; then
    [[ ! -e "$ROOT/contacts-pre-11.1" ]] || fail "Original Contacts backup already exists."
    mv "$ROOT/apps/contacts" "$ROOT/contacts-pre-11.1"
fi
tar --no-same-owner -xzf "$ARCHIVE" -C "$ROOT/apps"
chown -R 33:33 "$ROOT/apps/contacts"
docker exec -i owncloud-staging-db-1 psql -X -v ON_ERROR_STOP=1 \
    -U owncloud_stage -d owncloud_stage < "$SCRIPT_DIR/repair-test-homes.sql"
IMAGE="owncloud/server:11.1.0"
compose run --rm --no-deps --pull never --entrypoint /bin/bash owncloud -ec '
for file in /etc/entrypoint.d/*.sh; do source "$file"; done
source /etc/owncloud.d/20-config.sh
/usr/bin/occ app:disable checksum
/usr/bin/occ app:disable files_textviewer
/usr/bin/occ maintenance:mode --off
status=0
/usr/bin/occ upgrade || status=$?
if [[ "$status" != 0 && "$status" != 3 ]]; then
    echo "ERROR: Production upgrade failed: $status" >&2
    exit "$status"
fi
/usr/bin/occ maintenance:mode --on
'
compose up -d --no-deps --pull never --force-recreate owncloud
ready=0
for attempt in {1..120}; do
    if check_http http://127.0.0.1:8093/status.php 11.1.0 on; then
        ready=1
        break
    fi
    sleep 5
done
(( ready )) || fail "Upgraded production did not become ready in maintenance."
check_http https://www.mxtracks.info/owncloud/status.php 11.1.0 on
occ app:list
docker exec -i --user www-data owncloud-staging-owncloud-1 php <<'PHP'
<?php
require '/var/www/owncloud/lib/base.php';
$user = OC::$server->getUserManager()->get('hannes');
if ($user === null || $user->getHome() !== '/mnt/data/files/hannes') {
    throw new RuntimeException('Production user home is not relocated');
}
OC::$server->getUserSession()->setUser($user);
OC_Util::setupFS('hannes');
$folder = OC::$server->getRootFolder()->getUserFolder('hannes');
$db = OC::$server->getDatabaseConnection();
foreach (['image/jpeg', 'application/pdf', 'video/quicktime', 'video/mp4'] as $mime) {
    $row = $db->executeQuery(
        "SELECT f.fileid, f.path FROM oc_filecache f
         JOIN oc_storages s ON s.numeric_id = f.storage
         JOIN oc_mimetypes m ON m.id = f.mimetype
         WHERE s.id = ? AND m.mimetype = ? AND f.path LIKE 'files/%'
         ORDER BY f.fileid LIMIT 1", ['home::hannes', $mime]
    )->fetchAssociative();
    if (!$row) {
        throw new RuntimeException("Missing validation file for $mime");
    }
    $node = $folder->get(substr($row['path'], strlen('files/')));
    if ((int)$node->getId() !== (int)$row['fileid']) {
        throw new RuntimeException("File ID mismatch for $mime");
    }
    $stream = $node->fopen('rb');
    if (!is_resource($stream)) {
        throw new RuntimeException("Cannot open $mime");
    }
    $bytes = fread($stream, 64);
    fclose($stream);
    if ($bytes === false || strlen($bytes) === 0) {
        throw new RuntimeException("Cannot read $mime");
    }
    echo "$mime readable; file ID preserved\n";
}
PHP
python3 - "$ENV_FILE" <<'PY'
import os,sys
p=sys.argv[1]
lines=open(p).read().splitlines()
lines=[line for line in lines if not line.startswith("OWNCLOUD_IMAGE=")]
tmp=p+".upgrade-tmp"
with open(tmp,"x") as f:
    f.write("\n".join(lines)+"\nOWNCLOUD_IMAGE=owncloud/server:11.1.0\n")
os.chmod(tmp,0o600)
os.replace(tmp,p)
PY
RELEASED=1
occ maintenance:mode --off
ready=0
for attempt in {1..30}; do
    if check_http http://127.0.0.1:8093/status.php 11.1.0 off &&
        check_http https://www.mxtracks.info/owncloud/status.php 11.1.0 off; then
        ready=1
        break
    fi
    sleep 2
done
(( ready )) || fail "Production release not confirmed; no automatic rollback after possible writes."
touch "$BACKUP/upgrade-complete"
log "Production 11.1.0 is online; deployment version persisted in production.env."
log "Keep T1 backup disconnected from test operations. Backup: $BACKUP"
log "Host PHP packages were not changed."
