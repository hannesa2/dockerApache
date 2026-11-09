#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="/media/hannes/T1/owncloud-upgrade-11.1"
ENV_FILE="$STATE/test.env"
COMPOSE="$SCRIPT_DIR/upgrade-test-compose.yml"
LOCK="/run/lock/owncloud-mirror-sync.lock"
LOCKED=0
DOWNLOAD_TMP=""
CONTACTS_SHA256="742b08df8e1d0c095ae12c141e0719d0d9ed694237739d6570e611bed9ba2d4b"
STAGED_ARCHIVE="/home/hannes/contacts-1.6.0.tar.gz"
log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }
compose() { docker compose --env-file "$ENV_FILE" -f "$COMPOSE" "$@"; }
verify_archive() {
    local archive="$1"
    [[ -s "$archive" ]] || return 1
    [[ "$(sha256sum "$archive" | awk '{print $1}')" == "$CONTACTS_SHA256" ]]
}
wait_status() {
    local url="$1"
    local version="$2"
    local body="$STATE/status-last.json"
    local code attempt
    for attempt in {1..240}; do
        code="$(curl --noproxy '*' --header 'Host: localhost:8094' \
            --silent --show-error --max-time 10 --output "$body" \
            --write-out '%{http_code}' "$url" 2>/dev/null)" || code="000"
        if [[ "$code" == 200 ]] && python3 -c '
import json, sys
s=json.load(open(sys.argv[1]))
sys.exit(0 if s.get("installed") and s.get("versionstring") == sys.argv[2]
         and not s.get("needsDbUpgrade") else 1)
' "$body" "$version"; then
            return 0
        fi
        if (( attempt % 12 == 0 )); then
            log "Still waiting for HTTP $version readiness; last HTTP status: $code"
            if [[ "$code" == 200 ]]; then
                head -c 500 "$body" >&2
                printf '\n' >&2
            fi
        fi
        [[ "$(docker inspect -f '{{.State.Running}}' owncloud-upgrade-test-owncloud-1 2>/dev/null || true)" == "true" ]] ||
            fail "Test web container exited. Recent startup output follows."
        sleep 5
    done
    docker logs --tail 80 owncloud-upgrade-test-owncloud-1 >&2 || true
    fail "Upgrade did not return HTTP $version from $url."
}
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if (( status != 0 && LOCKED )); then
        compose stop --timeout 60 || log "ERROR: Could not stop test containers." >&2
        log "Test state retained; production was not modified." >&2
    fi
    [[ -z "$DOWNLOAD_TMP" ]] || rm -f -- "$DOWNLOAD_TMP"
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
    fail "T1 files were reserved for the production backup; do not restart the old test."
mountpoint -q /media/hannes/T1 || fail "T1 is not mounted."
[[ -f "$ENV_FILE" && -f "$STATE/apps/contacts/appinfo/info.xml" &&
    -f "$STATE/source.dump" ]] || fail "Expected the existing failed upgrade test."
[[ -z "$(compose ps -q --status running)" ]] || fail "Stop the test before app recovery."
mkdir "$LOCK" || fail "Another ownCloud operation holds the lock."
LOCKED=1
compose config --quiet
curl -fsS --max-time 15 http://127.0.0.1:8093/status.php |
    python3 -c 'import json,sys; s=json.load(sys.stdin); assert s.get("versionstring") == "10.16.7" and not s.get("maintenance")'

ARCHIVE="$STATE/contacts-1.6.0.tar.gz"
if ! verify_archive "$ARCHIVE"; then
    if verify_archive "$STAGED_ARCHIVE"; then
        log "Using the staged, checksum-verified official Contacts 1.6.0 archive."
        install -o root -g root -m 0600 "$STAGED_ARCHIVE" "$ARCHIVE"
    else
        DOWNLOAD_TMP="$STATE/contacts-1.6.0.tar.gz.download.$$"
        log "Downloading official Contacts 1.6.0 outside the isolated test network."
        curl --retry 3 --retry-all-errors --retry-delay 5 -fLsS --max-time 180 \
            https://github.com/owncloud/marketplace/releases/download/contacts/contacts-1.6.0.tar.gz \
            -o "$DOWNLOAD_TMP"
        verify_archive "$DOWNLOAD_TMP" ||
            fail "Contacts release archive checksum mismatch; refusing to install it."
        mv -- "$DOWNLOAD_TMP" "$ARCHIVE"
        DOWNLOAD_TMP=""
    fi
fi
verify_archive "$ARCHIVE" || fail "Contacts release archive checksum mismatch; refusing to install it."
python3 - "$ARCHIVE" <<'PY'
import sys, tarfile
from pathlib import PurePosixPath
with tarfile.open(sys.argv[1]) as archive:
    for member in archive:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "contacts":
            raise SystemExit("Unsafe or unexpected Contacts archive path")
        if not (member.isdir() or member.isfile()):
            raise SystemExit("Unsupported Contacts archive member")
PY
if [[ ! -e "$STATE/contacts-pre-11.1" ]]; then
    mv "$STATE/apps/contacts" "$STATE/contacts-pre-11.1"
fi
tar --no-same-owner -xzf "$ARCHIVE" -C "$STATE/apps"
chown -R 33:33 "$STATE/apps/contacts"
log "Starting test dependencies and disabling only the two incompatible test apps."
compose up -d --pull never --wait --wait-timeout 180 db redis
compose exec -T db psql -X -v ON_ERROR_STOP=1 \
    -U owncloud_stage -d owncloud_stage < "$SCRIPT_DIR/repair-test-homes.sql"
compose run --rm --no-deps --pull never --entrypoint /bin/bash owncloud -ec '
for file in /etc/entrypoint.d/*.sh; do source "$file"; done
source /etc/owncloud.d/20-config.sh
/usr/bin/occ app:disable checksum
/usr/bin/occ app:disable files_textviewer
/usr/bin/occ app:disable onlyoffice
/usr/bin/occ maintenance:mode --off
status=0
/usr/bin/occ upgrade || status=$?
if [[ "$status" != 0 && "$status" != 3 ]]; then
    echo "ERROR: Isolated upgrade failed with exit status $status." >&2
    exit "$status"
fi
'
compose up -d --pull never owncloud
TEST_IP="$(docker inspect -f '{{with index .NetworkSettings.Networks "owncloud-upgrade-test_isolated"}}{{.IPAddress}}{{end}}' \
    owncloud-upgrade-test-owncloud-1)"
[[ -n "$TEST_IP" ]] || fail "Test has no isolated-network address."
TEST_URL="http://$TEST_IP:8080/status.php"
log "Waiting up to twenty minutes for test upgrade completion."
wait_status "$TEST_URL" 11.1.0
compose exec -T --user www-data owncloud php /var/www/owncloud/occ maintenance:mode --off
for attempt in {1..30}; do
    if curl --noproxy '*' --header 'Host: localhost:8094' -fsS --max-time 5 \
        "$TEST_URL" -o "$STATE/status-last.json" &&
        python3 -c '
import json,sys
s=json.load(open(sys.argv[1]))
sys.exit(0 if s.get("versionstring") == "11.1.0" and not s.get("maintenance")
         and not s.get("needsDbUpgrade") else 1)
' "$STATE/status-last.json"; then
        break
    fi
    sleep 2
done
python3 -c '
import json,sys
s=json.load(open(sys.argv[1]))
assert s.get("versionstring") == "11.1.0" and not s.get("maintenance") and not s.get("needsDbUpgrade")
' "$STATE/status-last.json" || fail "Test has not left maintenance mode."
compose exec -T --user www-data owncloud php /var/www/owncloud/occ app:list
curl -fsS --max-time 15 http://127.0.0.1:8093/status.php |
    python3 -c 'import json,sys; s=json.load(sys.stdin); assert s.get("versionstring") == "10.16.7" and not s.get("maintenance")'
log "Test 11.1.0 is ready. From your client: ssh -N -L 127.0.0.1:8094:$TEST_IP:8080 latitude"
log "Open http://localhost:8094 and verify contacts and files; production remains 10.16.7."
