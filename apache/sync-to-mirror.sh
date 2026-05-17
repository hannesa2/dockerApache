#!/usr/bin/env bash
# sync-to-mirror.sh
# ─────────────────────────────────────────────────────────────────────────────
# Sync Apache + WordPress stack  source (latitude) → mirror (mac2016)
#
# Per site it:
#   1. Streams MySQL dump  source → mirror via SSH pipe (no temp file)
#   2. Rsyncs web root     source → mirror incrementally (--delete)
#   3. Patches wp-config.php on mirror (WP_HOME, WP_SITEURL, DB_HOST, HTTPS)
#   4. Writes a vhost config for the new mirror domain
#   5. Reloads the mirror Apache container
#
# Sites:
#   /srv/www.docker/www/dev.mxtracks  (DB: wordpress)           → dev16.mxtracks.info
#   /srv/www.docker/www/mxdocs        (DB: wordpress_mxtracks)  → www16.mxtracks.info
#
# Run DIRECTLY on latitude (interactive session – rsync uses SSH):
#   cd ~/git/dockerApache/apache
#   ./sync-to-mirror.sh
#
# Post-setup on mac2016 (one-time):
#   - Point dev16.mxtracks.info + www16.mxtracks.info DNS to mac2016's LAN IP
#   - Set up a reverse-proxy (macOS built-in Apache, Caddy, or similar) →
#     http://127.0.0.1:MIRROR_HTTP_PORT/
#   - Get SSL certs: certbot certonly --standalone / --webroot -d dev16.mxtracks.info
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
export LC_ALL=C
export LANG=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && source "$SCRIPT_DIR/.env"

# ── Source (latitude) ─────────────────────────────────────────────────────────
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
APACHE_DATA_DIR="${APACHE_DATA_DIR:-$SCRIPT_DIR/data}"
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-changeme_root}"
APACHE_HTTP_PORT="${APACHE_HTTP_PORT:-8085}"

# ── Mirror (mac2016) ──────────────────────────────────────────────────────────
MIRROR_HOST="${MIRROR_HOST:-mac2016}"
MIRROR_USER="${MIRROR_USER:-hannes}"
MIRROR_SSH_PORT="${MIRROR_SSH_PORT:-22}"
MIRROR_APACHE_DIR="${MIRROR_APACHE_DIR:-~/git/dockerApache/apache}"
MIRROR_DATA_DIR="${MIRROR_DATA_DIR:-$MIRROR_APACHE_DIR/data}"
MIRROR_MYSQL_ROOT_PASSWORD="${MIRROR_MYSQL_ROOT_PASSWORD:-changeme_root}"
MIRROR_HTTP_PORT="${MIRROR_HTTP_PORT:-8080}"
MIRROR_EXTRA_PATH="${MIRROR_EXTRA_PATH:-/usr/local/bin}"
MIRROR_SHELL="${MIRROR_SHELL:-zsh}"

# ── Site definitions ──────────────────────────────────────────────────────────
# SUBDIR | DB_NAME | DB_USER | DB_PASS | MIRROR_DOMAIN | WP_SUBPATH
declare -a SITES=(
  "dev.mxtracks|wordpress|wordpressuser|V€spa125|dev16.mxtracks.info|/wordpress"
  "mxdocs|wordpress_mxtracks|wpuser_mxtracks|Us€r.vespa125|www16.mxtracks.info|/wordpress"
)

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG="$SCRIPT_DIR/sync-mirror-${TIMESTAMP}.log"
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
die() { log "ERROR: $*"; exit 1; }

# ── Helper: run a command on the mirror host ──────────────────────────────────
remote() {
    ssh -p "$MIRROR_SSH_PORT" "${MIRROR_USER}@${MIRROR_HOST}" \
        "export PATH='${MIRROR_EXTRA_PATH}:/usr/local/bin:/usr/bin:/bin'; $1"
}

log "════════════════════════════════════════════════════════════"
log "  sync-to-mirror.sh  started $TIMESTAMP"
log "  Source : $(hostname) ($APACHE_DATA_DIR)"
log "  Mirror : ${MIRROR_USER}@${MIRROR_HOST} (${MIRROR_APACHE_DIR})"
log "  Log    : $LOG"
log "════════════════════════════════════════════════════════════"

# ── 0. Verify source Docker stack is up ──────────────────────────────────────
log ""
log "==> 0. Checking source Docker stack ..."
docker compose -f "$COMPOSE_FILE" ps --services --filter status=running \
    | grep -q 'db' || die "Source MySQL container is not running. Run: docker compose up -d"
log "    Source stack OK."

# ── 1. Verify / start mirror Docker stack ─────────────────────────────────────
log ""
log "==> 1. Checking mirror Docker stack on ${MIRROR_HOST} ..."
if ! remote "docker info >/dev/null 2>&1"; then
    log "    Docker Desktop not running on mirror. Attempting to start ..."
    remote "open -a Docker 2>/dev/null || true"
    log "    Waiting up to 60s for Docker to start ..."
    for i in $(seq 1 12); do
        sleep 5
        remote "docker info >/dev/null 2>&1" && break || true
    done
    remote "docker info >/dev/null 2>&1" \
        || die "Docker not available on mirror after 60s. Start Docker Desktop manually on ${MIRROR_HOST}."
fi

# Expand Docker's /var tmpfs on old LinuxKit-based Docker Desktop (mac2016).
# The default size is ~50% of VM RAM (≈4.6 GB) which is too small for two image stacks.
# This is safe and non-destructive; it resets when Docker Desktop restarts, so we run it every time.
log "    Expanding Docker VM /var tmpfs to 7 GB on mirror (LinuxKit workaround) ..."
remote "docker run --rm --privileged --pid=host alpine nsenter -t 1 -m -- mount -o remount,size=7G /var 2>/dev/null || true"

# Start mirror compose stack if not running (no --build: image must exist on mirror;
# first-time setup requires running 'docker compose up -d --build' interactively on mac2016)
log "    Ensuring mirror compose stack is up ..."
remote "cd '$MIRROR_APACHE_DIR' && docker compose up -d 2>&1" | tee -a "$LOG" || true

log "    Waiting for mirror MySQL to be healthy ..."
for i in $(seq 1 30); do
    remote "cd '$MIRROR_APACHE_DIR' && \
        docker compose exec -T db mysqladmin ping -h localhost \
            -u root -p'${MIRROR_MYSQL_ROOT_PASSWORD}' --silent 2>/dev/null" \
        && break || true
    sleep 2
done
remote "cd '$MIRROR_APACHE_DIR' && \
    docker compose exec -T db mysqladmin ping -h localhost \
        -u root -p'${MIRROR_MYSQL_ROOT_PASSWORD}' --silent 2>/dev/null" \
    || die "Mirror MySQL not ready after 60s – check: ssh ${MIRROR_HOST} 'cd ${MIRROR_APACHE_DIR} && docker compose logs db'"
log "    Mirror MySQL ready."

# ── Process each site ─────────────────────────────────────────────────────────
for site_def in "${SITES[@]}"; do
    IFS='|' read -r SUBDIR DB_NAME DB_USER DB_PASS MIRROR_DOMAIN WP_SUBPATH <<< "$site_def"
    SOURCE_DOCROOT="$APACHE_DATA_DIR/www/$SUBDIR"
    MIRROR_DOCROOT="$MIRROR_DATA_DIR/www/$SUBDIR"

    log ""
    log "──────────────────────────────────────────────────────────────"
    log "  Site   : $MIRROR_DOMAIN"
    log "  Source : $SOURCE_DOCROOT"
    log "  Mirror : ${MIRROR_USER}@${MIRROR_HOST}:${MIRROR_DOCROOT}"
    log "──────────────────────────────────────────────────────────────"

    # ── 2. Stream MySQL dump source → mirror ─────────────────────────────────
    log ""
    log "==> 2. Streaming DB '$DB_NAME' source → mirror ..."

    # Prepare DB on mirror first (before the pipe, so we don't consume stdin)
    remote "cd '$MIRROR_APACHE_DIR' && \
        docker compose exec -T db mysql -u root -p'${MIRROR_MYSQL_ROOT_PASSWORD}' <<SQL
DROP DATABASE IF EXISTS \\\`${DB_NAME}\\\`;
CREATE DATABASE \\\`${DB_NAME}\\\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${DB_USER}'@'%' IDENTIFIED BY '${DB_PASS}';
GRANT ALL PRIVILEGES ON \\\`${DB_NAME}\\\`.* TO '${DB_USER}'@'%';
FLUSH PRIVILEGES;
SQL"

    # Stream the dump
    docker compose -f "$COMPOSE_FILE" exec -T db \
        mysqldump -u root -p"${MYSQL_ROOT_PASSWORD}" \
            --no-tablespaces --single-transaction \
            --routines --triggers \
            "$DB_NAME" \
        | ssh -p "$MIRROR_SSH_PORT" "${MIRROR_USER}@${MIRROR_HOST}" \
            "export PATH='${MIRROR_EXTRA_PATH}:/usr/local/bin:/usr/bin:/bin'; \
             cd '$MIRROR_APACHE_DIR' && \
             docker compose exec -T db mysql \
                 -u root -p'${MIRROR_MYSQL_ROOT_PASSWORD}' '${DB_NAME}'"
    log "    DB synced."

    # ── 3. Rsync web root source → mirror ────────────────────────────────────
    log ""
    log "==> 3. Rsyncing web root source → mirror ..."
    remote "mkdir -p '$MIRROR_DOCROOT'"
    rsync -av --delete \
        -e "ssh -p $MIRROR_SSH_PORT" \
        --exclude='.git/' \
        --exclude='*/node_modules/' \
        --exclude='*/tmp/' \
        --exclude='*/cache/' \
        "$SOURCE_DOCROOT/" \
        "${MIRROR_USER}@${MIRROR_HOST}:${MIRROR_DOCROOT}/" \
        2>&1 | tee -a "$LOG" \
        | awk '/\/$/ || /^sending/ || /^sent /' || true
    log "    Rsync done."

    # ── 4. Patch wp-config.php on mirror ─────────────────────────────────────
    log ""
    log "==> 4. Patching wp-config.php on mirror ..."
    for WP_CFG_REL in "wp-config.php" "wordpress/wp-config.php"; do
        MIRROR_WP_CFG="${MIRROR_DOCROOT}/${WP_CFG_REL}"
        remote "[ -f '${MIRROR_WP_CFG}' ] || exit 0
            # DB_HOST → docker service name
            sed -i \"s/'DB_HOST', 'localhost'/'DB_HOST', 'db'/g\" '${MIRROR_WP_CFG}'
            # WP_HOME / WP_SITEURL → mirror domain
            sed -i \
                -e \"s|'WP_SITEURL', 'https://[^']*'|'WP_SITEURL', 'https://${MIRROR_DOMAIN}${WP_SUBPATH}'|g\" \
                -e \"s|'WP_HOME', 'https://[^']*'|'WP_HOME', 'https://${MIRROR_DOMAIN}${WP_SUBPATH}'|g\" \
                -e \"s|'WP_SITEURL', 'http://[^']*'|'WP_SITEURL', 'https://${MIRROR_DOMAIN}${WP_SUBPATH}'|g\" \
                -e \"s|'WP_HOME', 'http://[^']*'|'WP_HOME', 'https://${MIRROR_DOMAIN}${WP_SUBPATH}'|g\" \
                '${MIRROR_WP_CFG}'
            # HTTPS-behind-proxy detection (idempotent)
            grep -q 'HTTP_X_FORWARDED_PROTO' '${MIRROR_WP_CFG}' && exit 0
            sed -i \"s|<?php|<?php\n// Detect HTTPS behind reverse proxy\nif (isset(\\\$_SERVER['HTTP_X_FORWARDED_PROTO']) \&\& 'https' === \\\$_SERVER['HTTP_X_FORWARDED_PROTO']) {\n    \\\$_SERVER['HTTPS'] = 'on';\n}|\" \
                '${MIRROR_WP_CFG}'
            echo 'Patched: ${MIRROR_WP_CFG}'" \
        | tee -a "$LOG" || true
    done

    # ── 5. Fix WordPress siteurl/home in mirror DB ────────────────────────────
    log ""
    log "==> 5. Updating WordPress siteurl/home in mirror DB ..."
    remote "cd '$MIRROR_APACHE_DIR' && \
        docker compose exec -T db mysql \
            -u root -p'${MIRROR_MYSQL_ROOT_PASSWORD}' '${DB_NAME}' \
            -e \"UPDATE wp_options
                    SET option_value = REPLACE(option_value,
                        TRIM(TRAILING '${WP_SUBPATH}' FROM option_value),
                        'https://${MIRROR_DOMAIN}')
                 WHERE option_name IN ('siteurl','home');
                 SELECT option_name, option_value FROM wp_options WHERE option_name IN ('siteurl','home');\" \
        2>/dev/null" | tee -a "$LOG" || true

    # Direct set (more reliable than REPLACE on unknown source URL)
    remote "cd '$MIRROR_APACHE_DIR' && \
        docker compose exec -T db mysql \
            -u root -p'${MIRROR_MYSQL_ROOT_PASSWORD}' '${DB_NAME}' \
            -e \"UPDATE wp_options
                    SET option_value = 'https://${MIRROR_DOMAIN}${WP_SUBPATH}'
                 WHERE option_name IN ('siteurl','home');\" \
        2>/dev/null" | tee -a "$LOG" || true
    log "    DB URLs updated."

    # ── 6. Write mirror vhost config ──────────────────────────────────────────
    log ""
    log "==> 6. Writing mirror vhost config for ${MIRROR_DOMAIN} ..."
    MIRROR_VHOST_DIR="$MIRROR_DATA_DIR/vhosts"
    remote "mkdir -p '$MIRROR_VHOST_DIR'
cat > '${MIRROR_VHOST_DIR}/${MIRROR_DOMAIN}.conf' <<'VHOST'
<VirtualHost *:80>
    ServerName ${MIRROR_DOMAIN}
    DocumentRoot /var/www/html/${SUBDIR}
    DirectoryIndex index.php index.html

    <Directory /var/www/html/${SUBDIR}>
        Options Indexes SymLinksIfOwnerMatch
        AllowOverride All
        Require all granted
    </Directory>

    ErrorLog  /var/log/apache2/${MIRROR_DOMAIN}-error.log
    CustomLog /var/log/apache2/${MIRROR_DOMAIN}-access.log combined
</VirtualHost>
VHOST
echo 'Vhost written: ${MIRROR_VHOST_DIR}/${MIRROR_DOMAIN}.conf'" | tee -a "$LOG" || true
done

# ── 7. Reload mirror Apache ───────────────────────────────────────────────────
log ""
log "==> 7. Reloading mirror Apache ..."
remote "cd '$MIRROR_APACHE_DIR' && \
    docker compose exec -T apache apache2ctl graceful 2>&1 || \
    docker compose restart apache 2>&1" | tee -a "$LOG" || true
log "    Mirror Apache reloaded."

# ── Summary ───────────────────────────────────────────────────────────────────
log ""
log "════════════════════════════════════════════════════════════"
log "  Sync complete!  Log: $LOG"
log "════════════════════════════════════════════════════════════"
log ""
log "MIRROR DOMAINS"
for site_def in "${SITES[@]}"; do
    IFS='|' read -r _ _ _ _ MIRROR_DOMAIN WP_SUBPATH <<< "$site_def"
    log "  https://${MIRROR_DOMAIN}${WP_SUBPATH}"
done
log ""
log "POST-SETUP on ${MIRROR_HOST} (one-time, if not done yet)"
log "──────────────────────────────────────────────────────────"
log "  Docker container serves HTTP on port ${MIRROR_HTTP_PORT}."
log "  To expose with HTTPS via native macOS / reverse proxy:"
log ""
for site_def in "${SITES[@]}"; do
    IFS='|' read -r _ _ _ _ MIRROR_DOMAIN _ <<< "$site_def"
    log "  # ${MIRROR_DOMAIN}:"
    log "  sudo certbot certonly --standalone -d ${MIRROR_DOMAIN}   # or --webroot"
    log "  # Then add a reverse-proxy vhost (see native-proxy-*.conf pattern)"
done
log ""
log "  WP-CLI search-replace (run after DNS is live, once per site):"
for site_def in "${SITES[@]}"; do
    IFS='|' read -r SUBDIR _ _ _ MIRROR_DOMAIN WP_SUBPATH <<< "$site_def"
    log "  ssh ${MIRROR_HOST} \"cd ${MIRROR_APACHE_DIR} && \\"
    log "    docker compose exec --user www-data apache wp \\"
    log "      --path=/var/www/html/${SUBDIR}${WP_SUBPATH} \\"
    log "      search-replace 'https://devcopy.mxtracks.info' 'https://${MIRROR_DOMAIN}' \\"
    log "      --all-tables --skip-columns=guid\""
done
log ""
log "  Cron (nightly sync from latitude):"
log "  0 3 * * * $(realpath "$0") >> /var/log/apache-mirror-\$(date +\\%Y\\%m\\%d).log 2>&1"

