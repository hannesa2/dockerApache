#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# sync-to-mirror.sh  –  Live sync Nextcloud (source) → mirror server
#
# Streams data directly source → mirror without a temporary archive on disk:
#   1. PostgreSQL dump  → piped via SSH → mirror psql restore
#   2. Nextcloud config → tar pipe via SSH → mirror tar extract
#   3. Nextcloud data   → rsync over SSH (incremental, no temp files)
#
# Usage:
#   ./sync-to-mirror.sh [mirror-host] [mirror-nextcloud-dir]
#
# Examples:
#   ./sync-to-mirror.sh mirror.example.com /opt/nextcloud
#   MIRROR_HOST=192.168.1.50 ./sync-to-mirror.sh
#
# Requirements:
#   - SSH key-based access to the mirror host (no password prompt)
#   - Docker Compose running on both source and mirror
#   - rsync installed on both hosts
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && source "$SCRIPT_DIR/.env"

# ── Configuration ─────────────────────────────────────────────────────────────
MIRROR_HOST="${1:-${MIRROR_HOST:-}}"
MIRROR_DIR="${2:-${MIRROR_NEXTCLOUD_DIR:-/opt/nextcloud}}"
# Data root on the mirror – mirrors NEXTCLOUD_DATA_DIR on the mirror host.
# Defaults to MIRROR_DIR for setups where compose and data share the same parent.
MIRROR_DATA_DIR="${MIRROR_DATA_DIR:-$MIRROR_DIR}"
MIRROR_USER="${MIRROR_USER:-$(whoami)}"
MIRROR_SSH_PORT="${MIRROR_SSH_PORT:-22}"
MIRROR_SHELL="${MIRROR_SHELL:-zsh}"
MIRROR_EXTRA_PATH="${MIRROR_EXTRA_PATH:-/usr/local/bin}"

COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
POSTGRES_DB="${POSTGRES_DB:-nextcloud}"
POSTGRES_USER="${POSTGRES_USER:-nextcloud}"
NEXTCLOUD_DATA_DIR="${NEXTCLOUD_DATA_DIR:-$SCRIPT_DIR/data}"

LOG="$SCRIPT_DIR/sync-mirror-$(date +%Y%m%d_%H%M%S).log"
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

# ── Validate ──────────────────────────────────────────────────────────────────
if [ -z "$MIRROR_HOST" ]; then
    echo "ERROR: No mirror host specified."
    echo "Usage: $0 <mirror-host> [mirror-nextcloud-dir]"
    echo "   or: set MIRROR_HOST in .env"
    exit 1
fi

# Use login shell on mirror so PATH includes docker/docker compose
# MIRROR_SHELL=zsh for macOS, bash for Linux
SSH="ssh -p $MIRROR_SSH_PORT ${MIRROR_USER}@${MIRROR_HOST} $MIRROR_SHELL -l -c"
RSYNC_SSH="ssh -p $MIRROR_SSH_PORT"

# Wrapper: prepend MIRROR_EXTRA_PATH so docker is always found.
# Does NOT use a login shell to avoid .zprofile/.bashrc side effects.
# Uses explicit -f path so docker compose always finds its config.
MIRROR_COMPOSE="$MIRROR_DIR/docker-compose.yml"
remote() {
    ssh -p "$MIRROR_SSH_PORT" "${MIRROR_USER}@${MIRROR_HOST}" \
        "export PATH='${MIRROR_EXTRA_PATH}:/usr/local/bin:/usr/bin:/bin'; $1"
}

log "==> Sync started"
log "    Source       : $(hostname) → $NEXTCLOUD_DATA_DIR"
log "    Mirror       : ${MIRROR_USER}@${MIRROR_HOST}:${MIRROR_DIR}"
log "    Mirror data  : ${MIRROR_USER}@${MIRROR_HOST}:${MIRROR_DATA_DIR}"
log "    Log          : $LOG"
echo ""

# Test SSH connectivity
if ! remote 'docker info > /dev/null 2>&1'; then
    log "ERROR: Cannot reach docker on mirror host."
    log "       Check MIRROR_SHELL ($MIRROR_SHELL) and MIRROR_EXTRA_PATH ($MIRROR_EXTRA_PATH)"
    log "       Test manually: ssh ${MIRROR_USER}@${MIRROR_HOST} $MIRROR_SHELL -l -c 'export PATH=${MIRROR_EXTRA_PATH}:\$PATH; docker info'"
    exit 1
fi

# ── 1. Enable maintenance mode on source ─────────────────────────────────────
log "==> Enabling maintenance mode on source ..."
docker compose -f "$COMPOSE_FILE" exec -T --user www-data nextcloud \
    php occ maintenance:mode --on 2>/dev/null || true

# ── 2. Enable maintenance mode on mirror (if Nextcloud is running there) ──────
# Suppress all output: on first sync html is empty so occ fails – that's ok.
log "==> Enabling maintenance mode on mirror (if running) ..."
remote "docker compose -f '$MIRROR_COMPOSE' exec -T --user www-data nextcloud php occ maintenance:mode --on >/dev/null 2>&1 || true" || true

# ── 3. Stream PostgreSQL dump directly to mirror ──────────────────────────────
log "==> Streaming PostgreSQL '$POSTGRES_DB' source → mirror ..."

# Step 3a: prepare the target DB on mirror (runs before the dump pipe so it
#          does NOT accidentally consume any of the pg_dump stdin stream)
remote "docker compose -f '$MIRROR_COMPOSE' exec -T db psql -U '$POSTGRES_USER' postgres \
    -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
         WHERE datname='$POSTGRES_DB' AND pid <> pg_backend_pid();\" \
    -c \"DROP DATABASE IF EXISTS \\\"$POSTGRES_DB\\\";\" \
    -c \"CREATE DATABASE \\\"$POSTGRES_DB\\\" OWNER \\\"$POSTGRES_USER\\\";\""

# Step 3b: stream the dump directly into the freshly created DB
# --no-owner --no-acl strips ownership/privilege statements so roles that
# existed only on the source (e.g. legacy "oc_admin") don't cause errors.
docker compose -f "$COMPOSE_FILE" exec -T db \
    pg_dump -U "$POSTGRES_USER" --no-owner --no-acl "$POSTGRES_DB" \
    | ssh -p "$MIRROR_SSH_PORT" "${MIRROR_USER}@${MIRROR_HOST}" \
        "export PATH='${MIRROR_EXTRA_PATH}:/usr/local/bin:/usr/bin:/bin'; \
         docker compose -f '$MIRROR_COMPOSE' exec -T db \
             psql -U '$POSTGRES_USER' '$POSTGRES_DB'"
log "    Database synced."

# ── 4. Stream config + custom_apps via tar pipe ───────────────────────────────
log "==> Streaming Nextcloud config → mirror ..."
sudo env LANG=C LC_ALL=C tar -czC "$NEXTCLOUD_DATA_DIR/nextcloud" config custom_apps \
    | ssh -p "$MIRROR_SSH_PORT" "${MIRROR_USER}@${MIRROR_HOST}" \
        "export PATH='${MIRROR_EXTRA_PATH}:/usr/local/bin:/usr/bin:/bin'; \
         mkdir -p '$MIRROR_DATA_DIR/nextcloud' && \
         rm -rf '$MIRROR_DATA_DIR/nextcloud/config' '$MIRROR_DATA_DIR/nextcloud/custom_apps' && \
         tar -xzC '$MIRROR_DATA_DIR/nextcloud'"

# ── 4b. Patch config.php on mirror ────────────────────────────────────────────
# Remove source-specific redirect overrides and update DB credentials so the
# mirror connects to its own PostgreSQL.
# Uses grep -v (universally available) + perl env-var trick (no quoting hell).
log "    Patching mirror config.php (DB creds, removing redirect overrides) ..."
MIRROR_CFG="$MIRROR_DATA_DIR/nextcloud/config/config.php"
ssh -p "$MIRROR_SSH_PORT" "${MIRROR_USER}@${MIRROR_HOST}" \
    "export PATH='${MIRROR_EXTRA_PATH}:/usr/local/bin:/usr/bin:/bin'
     CFG='${MIRROR_CFG}'
     [ -f \"\$CFG\" ] || { echo 'WARNING: config.php not found on mirror, skipping patch'; exit 0; }
     # 1. Remove source-specific redirect / overwrite keys
     TMPF=\$(mktemp)
     grep -v -E \"'(overwritehost|overwriteprotocol|overwritewebroot)'\" \"\$CFG\" > \"\$TMPF\" && mv \"\$TMPF\" \"\$CFG\"
     # 2+3. Patch dbuser and dbpassword – pass values via env vars to avoid quoting issues
     NC_DB_USER='${POSTGRES_USER}' NC_DB_PASS='${POSTGRES_PASSWORD}' perl -i -pe '
         s|(.\x27dbuser\x27\s*=>\s*\x27)[^\x27]*(\x27)|\$1\$ENV{NC_DB_USER}\$2|;
         s|(.\x27dbpassword\x27\s*=>\s*\x27)[^\x27]*(\x27)|\$1\$ENV{NC_DB_PASS}\$2|;
     ' \"\$CFG\"
     echo 'config.php patched OK'
    "
log "    Config synced."

# ── 5. Rsync user data incrementally (no temp files, no compression overhead) ─
log "==> Rsyncing user data source → mirror (incremental) ..."
sudo rsync -av --delete \
    -e "ssh -p $MIRROR_SSH_PORT" \
    "$NEXTCLOUD_DATA_DIR/nextcloud/data/" \
    "${MIRROR_USER}@${MIRROR_HOST}:${MIRROR_DATA_DIR}/nextcloud/data/" \
    2>&1 | tee -a "$LOG" | grep -E '(^sending|^deleting|/$|^sent|^total)'
log "    User data synced."

# ── 6. Down + up mirror Nextcloud so html/ is (re-)populated by the entrypoint ─
# "restart" does NOT re-run the image's init script – only "down && up -d" does.
log "==> Stopping mirror Nextcloud container ..."
remote "docker compose -f '$MIRROR_COMPOSE' down --remove-orphans 2>/dev/null || true"

log "==> Starting mirror Nextcloud container (html will be re-populated) ..."
remote "docker compose -f '$MIRROR_COMPOSE' up -d"

# Wait up to 20 min for the entrypoint to finish installing Nextcloud into html/
# macOS Docker Desktop bind-mounts are slow – copying the Nextcloud web root can
# take 10+ minutes on first run or after a full re-pull.
MAX_WAIT=240   # iterations
WAIT_SEC=5
log "    Waiting for mirror Nextcloud to finish initialising (max ${MAX_WAIT} * ${WAIT_SEC} s = $((MAX_WAIT * WAIT_SEC / 60)) min) ..."
MIRROR_UP=false
for i in $(seq 1 $MAX_WAIT); do
    sleep $WAIT_SEC
    # Check if the nextcloud container is up AND html/lib/versioncheck.php exists
    VERSION_CHECK=$(remote "docker compose -f '$MIRROR_COMPOSE' exec -T nextcloud \
        test -f /var/www/html/lib/versioncheck.php && echo OK || echo MISSING" 2>/dev/null || echo MISSING)
    if [ "$VERSION_CHECK" = "OK" ]; then
        log "    Mirror Nextcloud is up and html/ is populated (${i} * ${WAIT_SEC} s = $((i * WAIT_SEC)) s)."
        MIRROR_UP=true
        break
    fi
    # Print progress every 12 iterations (= 1 minute)
    if (( i % 12 == 0 )); then
        ELAPSED=$(( i * WAIT_SEC ))
        log "    Still initialising ... ${ELAPSED}s elapsed (html/lib/versioncheck.php not yet present)"
    fi
done

if [ "$MIRROR_UP" != "true" ]; then
    log "WARNING: Mirror did not finish initialising within $(( MAX_WAIT * WAIT_SEC / 60 )) minutes."
    log "         The Nextcloud entrypoint is still copying files into the bind-mounted html/ directory."
    log "         This is expected on macOS Docker Desktop with a fresh or re-pulled image."
    log "         Suggestions:"
    log "           1. Wait a few more minutes then re-run occ manually:"
    log "              ssh ${MIRROR_USER}@${MIRROR_HOST} 'cd $MIRROR_DIR && docker compose exec --user www-data nextcloud php occ upgrade'"
    log "           2. Check mirror logs: ssh ${MIRROR_USER}@${MIRROR_HOST} 'cd $MIRROR_DIR && docker compose logs nextcloud'"
fi

log "==> Running occ upgrade + repair on mirror ..."
remote "docker compose -f '$MIRROR_COMPOSE' exec -T --user www-data nextcloud php occ upgrade 2>/dev/null || true && \
        docker compose -f '$MIRROR_COMPOSE' exec -T --user www-data nextcloud php occ maintenance:repair 2>/dev/null || true"

# Ensure localhost and the mirror hostname are trusted on the mirror instance
log "==> Setting trusted_domains on mirror (localhost + $MIRROR_HOST) ..."
remote "docker compose -f '$MIRROR_COMPOSE' exec -T --user www-data nextcloud \
    php occ config:system:set trusted_domains 0 --value='localhost' 2>/dev/null || true"
remote "docker compose -f '$MIRROR_COMPOSE' exec -T --user www-data nextcloud \
    php occ config:system:set trusted_domains 1 --value='$MIRROR_HOST' 2>/dev/null || true"

log "==> Disabling maintenance mode on mirror ..."
remote "docker compose -f '$MIRROR_COMPOSE' exec -T --user www-data nextcloud php occ maintenance:mode --off 2>/dev/null || true"

# ── 7. Disable maintenance mode on source ────────────────────────────────────
log "==> Disabling maintenance mode on source ..."
docker compose -f "$COMPOSE_FILE" exec -T --user www-data nextcloud \
    php occ maintenance:mode --off

log "══════════════════════════════════════════"
log "==> Sync complete. Log: $LOG"
log ""
log "Tip: Add to crontab for nightly sync:"
log "  0 3 * * * /path/to/nextcloud/sync-to-mirror.sh >> /var/log/nc-mirror.log 2>&1"

