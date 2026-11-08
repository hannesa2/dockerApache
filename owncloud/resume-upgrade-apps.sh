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
log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }
compose() { docker compose --env-file "$ENV_FILE" -f "$COMPOSE" "$@"; }
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if (( status != 0 && LOCKED )); then
        compose stop || log "ERROR: Could not stop test containers." >&2
        log "Test state retained; production was not modified." >&2
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
log "Downloading official Contacts 1.6.0 outside the isolated test network."
curl -fLsS --max-time 180 \
    https://github.com/owncloud/marketplace/releases/download/contacts/contacts-1.6.0.tar.gz \
    -o "$ARCHIVE"
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
compose run --rm --no-deps --pull never --entrypoint /bin/bash owncloud -ec '
for file in /etc/entrypoint.d/*.sh; do source "$file"; done
source /etc/owncloud.d/20-config.sh
/usr/bin/occ app:disable checksum
/usr/bin/occ app:disable files_textviewer
'
compose up -d --pull never owncloud
log "Waiting up to twenty minutes for test upgrade completion."
ready=0
for attempt in {1..240}; do
    response="$(curl -fsS --max-time 5 http://127.0.0.1:8094/status.php 2>/dev/null)" || response=""
    if [[ -n "$response" ]] && python3 -c '
import json, sys
s=json.load(sys.stdin)
sys.exit(0 if s.get("installed") and s.get("versionstring") == "11.1.0"
         and not s.get("needsDbUpgrade") else 1)
' <<< "$response"; then
        ready=1
        break
    fi
    [[ "$(docker inspect -f '{{.State.Running}}' owncloud-upgrade-test-owncloud-1)" == "true" ]] ||
        fail "Test web container exited; inspect its logs."
    sleep 5
done
(( ready )) || fail "Upgrade timed out; inspect test logs."
compose exec -T --user www-data owncloud php /var/www/owncloud/occ maintenance:mode --off
normal=0
for attempt in {1..30}; do
    response="$(curl -fsS --max-time 5 http://127.0.0.1:8094/status.php)"
    if python3 -c '
import json,sys
s=json.load(sys.stdin)
sys.exit(0 if s.get("versionstring") == "11.1.0" and not s.get("maintenance")
         and not s.get("needsDbUpgrade") else 1)
' <<< "$response"; then
        normal=1
        break
    fi
    sleep 2
done
(( normal )) || fail "Test remains in maintenance."
compose exec -T --user www-data owncloud php /var/www/owncloud/occ app:list
curl -fsS --max-time 15 http://127.0.0.1:8093/status.php |
    python3 -c 'import json,sys; s=json.load(sys.stdin); assert s.get("versionstring") == "10.16.7" and not s.get("maintenance")'
log "Test 11.1.0 is ready on localhost:8094. Verify contacts and files; production remains 10.16.7."
