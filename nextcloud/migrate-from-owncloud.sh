#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# migrate-from-owncloud.sh
#
# Migrates users from an existing ownCloud installation to this Nextcloud stack:
#   1. Lists all ownCloud users
#   2. For each user:
#      a. Creates the user in Nextcloud (if not already existing)
#      b. Rsyncs files from ownCloud data dir → Nextcloud data dir
#      c. Exports contacts (.vcf) from ownCloud filesystem
#      d. Imports contacts into Nextcloud via CardDAV
#      e. Triggers a Nextcloud file scan so files appear in the UI
#
# Usage:
#   ./migrate-from-owncloud.sh
#
# Requirements on the host running this script:
#   - rsync
#   - curl
#   - Access to ownCloud data directory (sudo/root or same user)
#   - Docker Compose stack (nextcloud/) must be running
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && source "$SCRIPT_DIR/.env"

# ── Configuration ─────────────────────────────────────────────────────────────
OWNCLOUD_DATA_DIR="${OWNCLOUD_DATA_DIR:-/srv/www/vhosts/mxdocs/owncloud/data}"
OWNCLOUD_OCC="${OWNCLOUD_OCC:-/srv/www/vhosts/mxdocs/owncloud/occ}"
OWNCLOUD_URL="${OWNCLOUD_URL:-http://localhost}"
NEXTCLOUD_DATA_DIR="${NEXTCLOUD_DATA_DIR:-$SCRIPT_DIR/data}"
NEXTCLOUD_URL="${NEXTCLOUD_URL:-http://localhost:8082}"
NEXTCLOUD_ADMIN="${NEXTCLOUD_ADMIN_USER:-admin}"
NEXTCLOUD_ADMIN_PASS="${NEXTCLOUD_ADMIN_PASSWORD:-changeme_admin}"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
MIGRATION_LOG="$SCRIPT_DIR/migration-$(date +%Y%m%d_%H%M%S).log"

export LC_ALL=C
export LANG=C

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$MIGRATION_LOG"; }

log "==> Migration started"
log "    ownCloud data : $OWNCLOUD_DATA_DIR"
log "    Nextcloud data: $NEXTCLOUD_DATA_DIR/nextcloud/data"
log "    Log file      : $MIGRATION_LOG"
echo ""

# ── Sanity checks ─────────────────────────────────────────────────────────────
if [ ! -d "$OWNCLOUD_DATA_DIR" ]; then
    log "ERROR: ownCloud data directory not found: $OWNCLOUD_DATA_DIR"
    log "       Set OWNCLOUD_DATA_DIR in .env or at the top of this script."
    exit 1
fi

if ! docker compose -f "$COMPOSE_FILE" ps | grep -qiE "nextcloud.*(running|up)"; then
    log "ERROR: Nextcloud container is not running. Start it first with:"
    log "       docker compose up -d"
    exit 1
fi

# ── Get ownCloud user list ────────────────────────────────────────────────────
log "==> Getting ownCloud user list ..."

if [ -f "$OWNCLOUD_OCC" ]; then
    # Preferred: use occ
    USERS=$(sudo -u www-data php "$OWNCLOUD_OCC" user:list --output=json 2>/dev/null \
        | python3 -c "import sys,json; [print(k) for k in json.load(sys.stdin).keys()]")
else
    # Fallback: scan data directory for user folders (skip 'files_encryption', 'appdata_*')
    log "    occ not found, scanning data directory for users ..."
    USERS=$(find "$OWNCLOUD_DATA_DIR" -maxdepth 1 -mindepth 1 -type d \
        ! -name 'files_encryption' \
        ! -name 'appdata_*' \
        ! -name '__groupfolders' \
        -exec basename {} \;)
fi

log "    Found users: $(echo "$USERS" | tr '\n' ' ')"
echo ""

# ── Process each user ─────────────────────────────────────────────────────────
for USER in $USERS; do
    # Skip system/admin users
    if [[ "$USER" == "admin" ]]; then
        log "==> Skipping system user: $USER"
        continue
    fi
    log "──────────────────────────────────────────"
    log "==> Processing user: $USER"

    OC_USER_DIR="$OWNCLOUD_DATA_DIR/$USER"
    NC_USER_DIR="$NEXTCLOUD_DATA_DIR/nextcloud/data/$USER"

    # Determine source files path – ownCloud uses <user>/files/, older installs may differ
    if sudo test -d "$OC_USER_DIR/files"; then
        OC_FILES_DIR="$OC_USER_DIR/files"
    elif sudo test -d "$OC_USER_DIR" && [ -n "$(sudo ls -A "$OC_USER_DIR" 2>/dev/null)" ]; then
        # No 'files' subdir – treat the user dir itself as the source
        OC_FILES_DIR="$OC_USER_DIR"
        log "    NOTE: No 'files/' subdir found, using $OC_USER_DIR directly"
    else
        log "    No data directory found at $OC_USER_DIR – skipping."
        log "    Available dirs: $(sudo ls "$OWNCLOUD_DATA_DIR/" 2>/dev/null | tr '\n' ' ')"
        continue
    fi

    # ── a. Create user in Nextcloud ───────────────────────────────────────────
    if docker compose -f "$COMPOSE_FILE" exec --user www-data nextcloud \
            php occ user:list 2>/dev/null | grep -q "^  - $USER:"; then
        log "    User already exists in Nextcloud, skipping creation."
    else
        log "    Creating user '$USER' in Nextcloud ..."
        TEMP_PASS="Migrate.$USER"
        OC_PASS="$TEMP_PASS" docker compose -f "$COMPOSE_FILE" exec \
            -e OC_PASS --user www-data nextcloud \
            php occ user:add --display-name="$USER" --password-from-env "$USER" \
            >> "$MIGRATION_LOG" 2>&1 || true
        log "    ⚠ Temporary password set to: $TEMP_PASS (change via web UI or occ user:resetpassword $USER)"
    fi

    # ── b. Rsync files ────────────────────────────────────────────────────────
    log "    Rsyncing files from ownCloud → Nextcloud (--delete removes files not in ownCloud) ..."
    sudo mkdir -p "$NC_USER_DIR/files"
    sudo rsync -av --progress --delete \
        --exclude='.ocdata' \
        --exclude='.htaccess' \
        "$OC_FILES_DIR/" \
        "$NC_USER_DIR/files/" \
        2>&1 | tee -a "$MIGRATION_LOG" | grep -E '(^sending|^deleting|/$|^sent)' || true
    log "    Files rsynced (files not in ownCloud source have been removed)."

    # ── b2. Fix non-UTF8 filenames (Latin-1 → UTF-8) ─────────────────────────
    # Old ownCloud/Windows filenames may contain ISO-8859-1 chars (ü,ö,ä,…)
    # that PostgreSQL rejects. convmv renames them to proper UTF-8 in-place.
    log "    Fixing non-UTF8 filenames with convmv ..."
    if command -v convmv &>/dev/null; then
        sudo convmv -f iso-8859-1 -t utf-8 -r --notest \
            "$NC_USER_DIR/files/" 2>&1 \
            | grep -v "^Skipping" \
            | tee -a "$MIGRATION_LOG" || true
        log "    convmv done."
    else
        log "    WARNING: convmv not found – skipping filename encoding fix."
        log "             Install with: sudo apt install convmv"
        log "             Then re-run or fix manually before the file scan."
    fi

    # Fix ownership so www-data inside the container can read the files
    sudo chown -R www-data:root "$NC_USER_DIR" 2>/dev/null || \
        sudo chown -R 33:0 "$NC_USER_DIR" 2>/dev/null || true

    # ── c. Export contacts from ownCloud filesystem ───────────────────────────
    VCF_OUT="$SCRIPT_DIR/migration-contacts-${USER}.vcf"
    log "    Extracting contacts from ownCloud data directory ..."
    sudo find "$OC_USER_DIR" -path "*/addressbooks/*" -name "*.vcf" \
        -exec cat {} \; > "$VCF_OUT" 2>/dev/null || true

    if [ -s "$VCF_OUT" ]; then
        CONTACT_COUNT=$(grep -c "^BEGIN:VCARD" "$VCF_OUT" || echo 0)
        log "    Found $CONTACT_COUNT contacts → $VCF_OUT"

        # ── d. Import contacts into Nextcloud via CardDAV ─────────────────────
        log "    Importing contacts into Nextcloud ..."

        # Split multi-vcard file and import each individually
        python3 - "$VCF_OUT" "$USER" "$NEXTCLOUD_URL" "$NEXTCLOUD_ADMIN" "$NEXTCLOUD_ADMIN_PASS" << 'PYEOF'
import sys, re, subprocess, urllib.request, urllib.parse, base64, uuid

vcf_file, user, nc_url, admin, admin_pass = sys.argv[1:]

with open(vcf_file, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

cards = re.findall(r'BEGIN:VCARD.*?END:VCARD', content, re.DOTALL)
print(f"    Importing {len(cards)} contacts for {user} ...")

credentials = base64.b64encode(f"{admin}:{admin_pass}".encode()).decode()
base_url = f"{nc_url}/remote.php/dav/addressbooks/users/{user}/contacts"

ok, fail = 0, 0
for card in cards:
    uid = str(uuid.uuid4())
    url = f"{base_url}/{uid}.vcf"
    req = urllib.request.Request(
        url,
        data=card.encode('utf-8'),
        method='PUT',
        headers={
            'Authorization': f'Basic {credentials}',
            'Content-Type': 'text/vcard; charset=utf-8',
        }
    )
    try:
        with urllib.request.urlopen(req) as resp:
            ok += 1
    except Exception as e:
        fail += 1
        print(f"    WARN: failed to import a contact: {e}")

print(f"    Contacts imported: {ok} ok, {fail} failed")
PYEOF
    else
        log "    No contacts found for $USER."
        rm -f "$VCF_OUT"
    fi

    # ── e. Trigger Nextcloud file scan ────────────────────────────────────────
    log "    Scanning files in Nextcloud for user '$USER' ..."
    docker compose -f "$COMPOSE_FILE" exec --user www-data nextcloud \
        php occ files:scan "$USER" >> "$MIGRATION_LOG" 2>&1
    log "    File scan complete."

    log "==> Done with user: $USER"
    echo ""
done

log "══════════════════════════════════════════"
log "==> Migration complete."
log "    Full log: $MIGRATION_LOG"
log ""
log "Next steps:"
log "  1. Ask each user to log in and verify their files and contacts"
log "  2. Reset passwords: docker compose exec --user www-data nextcloud php occ user:resetpassword <username>"
log "  3. Remove migration contact files: rm -f $SCRIPT_DIR/migration-contacts-*.vcf"

