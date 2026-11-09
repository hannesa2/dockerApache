#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="$SCRIPT_DIR/../apache/docker-compose.yml"
ARCHIVE="${CONTACTS_ARCHIVE:-$HOME/contacts-1.6.0.tar.gz}"
STAGE=""
HELPER=""
CHANGED=0
RELEASED=0
MODE="${1:---upgrade}"
compose() { docker compose -f "$COMPOSE" --profile owncloud "$@"; }
occ() { compose exec -T --user www-data owncloud php /var/www/owncloud/occ "$@"; }
log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }
cleanup() {
    local status=$?
    trap - EXIT
    if (( status != 0 && CHANGED && ! RELEASED )); then
        compose stop owncloud || log "ERROR: Could not stop mirror web container." >&2
        log "Mirror retained offline; inspect before restarting. No database backup was requested." >&2
    fi
    if [[ -n "$HELPER" ]]; then
        docker rm "$HELPER" >/dev/null || status=1
    fi
    exit "$status"
}
trap cleanup EXIT
[[ "$(hostname)" == mac2016* ]] || fail "Run on mac2016."
[[ "$(docker context show)" == orbstack ]] || fail "Expected OrbStack Docker context."
compose config --format json | python3 -c '
import json,sys
s=json.load(sys.stdin)["services"]["owncloud"]
assert any(v["target"] == "/mnt/data/files" and
    v["source"] == "/Volumes/SamsungT7_2T/ownCloudBackup/owncloud/data"
    for v in s["volumes"]), "Unexpected mirror data mount"
'
[[ "$MODE" == "--upgrade" || "$MODE" == "--resume" ]] && (( $# <= 1 )) ||
    fail "Usage: $0 [--upgrade|--resume]"
if [[ "$MODE" == "--upgrade" ]]; then
occ status --output=json | python3 -c '
import json,sys
assert json.load(sys.stdin).get("versionstring") == "10.16.7"
'
[[ "$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')" == \
    "742b08df8e1d0c095ae12c141e0719d0d9ed694237739d6570e611bed9ba2d4b" ]] ||
    fail "Verified Contacts 1.6.0 archive required."
python3 - "$ARCHIVE" <<'PY'
import sys, tarfile
from pathlib import PurePosixPath
with tarfile.open(sys.argv[1]) as archive:
    for member in archive:
        p = PurePosixPath(member.name)
        if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] != "contacts" or not (member.isdir() or member.isfile()):
            raise SystemExit("Unsafe Contacts archive")
PY
CODE="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/www/owncloud"}}{{.Source}}{{end}}{{end}}' apache-owncloud-1)"
[[ "$CODE" == /Volumes/SamsungT7_2T/ownCloudBackup/owncloud/owncloud ]] ||
    fail "Unexpected mirror code mount."
[[ ! -L "$CODE" && -f "$CODE/version.php" ]] || fail "Invalid mirror code tree."
log "Building PHP 8.3 / ownCloud 11.1 image before stopping the mirror."
compose build owncloud
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/owncloud-11.1.XXXXXX")"
HELPER="$(docker create --entrypoint /bin/true owncloud/server:11.1.0)"
docker cp "$HELPER:/var/www/owncloud/." "$STAGE/"
grep -q "11.1.0" "$STAGE/version.php" || fail "Staged ownCloud code is not 11.1.0."
tar -xzf "$ARCHIVE" -C "$STAGE/apps"
# Retain non-bundled apps for compatibility review; bundled apps use 11.1 code.
for app in "$CODE"/apps/*; do
    [[ -d "$app" ]] || continue
    name="$(basename "$app")"
    if [[ ! -e "$STAGE/apps/$name" ]]; then
        cp -R "$app" "$STAGE/apps/$name"
    fi
done
CHANGED=1
occ maintenance:mode --on
occ app:disable checksum
occ app:disable files_textviewer
occ app:disable gallery
compose stop owncloud
log "Replacing mirror code only; user files and database are not recopied."
rsync -a --delete --exclude=/config/ "$STAGE/" "$CODE/"
compose exec -T owncloud-db sh -c \
    'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$OWNCLOUD_DB_NAME"' \
    < "$SCRIPT_DIR/repair-test-homes.sql"
compose up -d --no-deps --pull never --force-recreate owncloud
[[ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/mnt/data/files"}}{{.Source}}{{end}}{{end}}' apache-owncloud-1)" == \
    /Volumes/SamsungT7_2T/ownCloudBackup/owncloud/data ]] ||
    fail "Recreated mirror does not have the required user-data mount."
for attempt in {1..30}; do
    if compose exec -T owncloud test -d /mnt/data/sessions; then
        break
    fi
    sleep 2
done
else
CODE="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/www/owncloud"}}{{.Source}}{{end}}{{end}}' apache-owncloud-1)"
[[ "$CODE" == /Volumes/SamsungT7_2T/ownCloudBackup/owncloud/owncloud ]] ||
    fail "Unexpected mirror code mount."
grep -q "11.1.0" "$CODE/version.php" || fail "Resume requires already-staged 11.1.0 code."
CHANGED=1
compose start owncloud
fi
occ app:disable gallery
occ maintenance:mode --off
status=0
occ upgrade || status=$?
[[ "$status" == 0 || "$status" == 3 ]] || fail "Mirror upgrade failed with status $status."
occ maintenance:mode --on
occ status --output=json | python3 -c '
import json,sys
assert json.load(sys.stdin).get("versionstring") == "11.1.0"
'
compose exec -T --user www-data owncloud php -r '
define("OC_CONSOLE",true);
require "/var/www/owncloud/lib/base.php";
$u=OC::$server->getUserManager()->get("hannes");
if (!$u || !is_dir($u->getHome()."/files/Bücher")) {
    fwrite(STDERR, "Mirror user files or expected UTF-8 paths are missing\n");
    exit(1);
}
'
occ app:list
RELEASED=1
occ maintenance:mode --off
curl --noproxy '*' -fsS --max-time 30 \
    --resolve www16.mxtracks.info:9443:127.0.0.1 \
    https://www16.mxtracks.info:9443/owncloud/status.php | python3 -c '
import json,sys
s=json.load(sys.stdin)
assert s.get("versionstring") == "11.1.0" and not s.get("maintenance") and not s.get("needsDbUpgrade")
'
log "mac2016 mirror is online on 11.1.0. No additional backup or Latitude sync performed."
log "Temporary code staging retained at $STAGE for targeted cleanup."
