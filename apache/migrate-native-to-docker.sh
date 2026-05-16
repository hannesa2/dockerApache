#!/usr/bin/env bash
# migrate-native-to-docker.sh
# ─────────────────────────────────────────────────────────────────────────────
# Migrate native Apache + MySQL websites into the Docker Apache stack.
#
# Migrates:
#   /srv/www/vhosts/dev.mxtracks  →  devcopy.mxtracks.info
#   /srv/www/vhosts/mxdocs        →  wwwcopy.mxtracks.info
#
# Run DIRECTLY on latitude (interactive SSH or local terminal – sudo required).
#
# Usage:
#   cd ~/git/dockerApache/apache
#   cp .env.example .env          # edit passwords
#   ./migrate-native-to-docker.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
export LC_ALL=C
export LANG=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && source "$SCRIPT_DIR/.env"

APACHE_DATA_DIR="${APACHE_DATA_DIR:-$SCRIPT_DIR/data}"
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-changeme_root}"
APACHE_HTTP_PORT="${APACHE_HTTP_PORT:-8081}"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG="$SCRIPT_DIR/migrate-${TIMESTAMP}.log"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
die() { log "ERROR: $*"; exit 1; }

# ─── Site definitions ─────────────────────────────────────────────────────────
# Columns (pipe-separated):
#   NATIVE_DOCROOT | WWW_SUBDIR | NATIVE_DB | NATIVE_DB_USER | NATIVE_DB_PASS | NEW_DOMAIN
#
# NATIVE_DB_USER needs at least SELECT + LOCK TABLES + SHOW VIEW + TRIGGER
# (standard WordPress user grants are sufficient for mysqldump).
declare -a SITES=(
  "/srv/www/vhosts/dev.mxtracks|dev.mxtracks|wordpress|wordpressuser|V€spa125|devcopy.mxtracks.info"
  "/srv/www/vhosts/mxdocs|mxdocs|wordpress_mxtracks|wpuser_mxtracks|Us€r.vespa125|wwwcopy.mxtracks.info"
)

log "================================================================"
log "  migrate-native-to-docker.sh  started $TIMESTAMP"
log "  Log:           $LOG"
log "  Data dir:      $APACHE_DATA_DIR"
log "  HTTP port:     $APACHE_HTTP_PORT"
log "================================================================"

# ─── 1. Start / rebuild Docker compose stack ──────────────────────────────────
log ""
log "==> 1. Starting Docker compose stack ..."
cd "$SCRIPT_DIR"
docker compose -f "$COMPOSE_FILE" up -d --build 2>&1 | tee -a "$LOG"

log "    Waiting for MySQL to become healthy (up to 60 s) ..."
for i in $(seq 1 30); do
  if docker compose -f "$COMPOSE_FILE" exec -T db \
       mysqladmin ping -h localhost -u root -p"$MYSQL_ROOT_PASSWORD" --silent 2>/dev/null; then
    break
  fi
  sleep 2
done
docker compose -f "$COMPOSE_FILE" exec -T db \
  mysqladmin ping -h localhost -u root -p"$MYSQL_ROOT_PASSWORD" --silent \
  || die "MySQL not ready after 60 s – check 'docker compose logs db'"
log "    MySQL is ready."

# ─── Process each site ────────────────────────────────────────────────────────
for site_def in "${SITES[@]}"; do
  IFS='|' read -r NATIVE_DOCROOT WWW_SUBDIR NATIVE_DB NATIVE_DB_USER NATIVE_DB_PASS NEW_DOMAIN <<< "$site_def"
  DOCKER_DOCROOT="$APACHE_DATA_DIR/www/$WWW_SUBDIR"

  log ""
  log "──────────────────────────────────────────────────────────────"
  log "  Site  : $NEW_DOMAIN"
  log "  Source: $NATIVE_DOCROOT"
  log "  Target: $DOCKER_DOCROOT"
  log "──────────────────────────────────────────────────────────────"

  # ── 2. Rsync web root ──────────────────────────────────────────────────────
  log ""
  log "==> 2. Rsyncing web root ..."
  sudo mkdir -p "$DOCKER_DOCROOT"
  sudo rsync -av --delete \
    --exclude='.git/' \
    --exclude='.svn/' \
    --exclude='*/node_modules/' \
    --exclude='*/tmp/' \
    --exclude='*/cache/' \
    "$NATIVE_DOCROOT/" "$DOCKER_DOCROOT/" 2>&1 \
    | tee -a "$LOG" \
    | awk '/\/$/ || /^sending/ || /^sent /' || true
  log "    Rsync done."

  # Fix ISO-8859-1 filenames to UTF-8 (if convmv is installed)
  if command -v convmv &>/dev/null; then
    log "    Running convmv to fix Latin-1 filenames ..."
    sudo convmv -f iso-8859-1 -t utf-8 -r --notest "$DOCKER_DOCROOT" 2>&1 \
      | grep -v 'Skipping' | tee -a "$LOG" || true
  fi

  # Make sure www-data inside the container can read everything
  log "    Setting ownership → www-data:www-data ..."
  sudo chown -R www-data:www-data "$DOCKER_DOCROOT"
  log "    Ownership set."

  # ── 3. Dump native MySQL database ─────────────────────────────────────────
  DUMP_FILE="$SCRIPT_DIR/migrate-${NATIVE_DB}-${TIMESTAMP}.sql.gz"
  log ""
  log "==> 3. Dumping native DB '$NATIVE_DB' → $(basename "$DUMP_FILE") ..."
  mysqldump \
    -u "$NATIVE_DB_USER" \
    -p"$NATIVE_DB_PASS" \
    --no-tablespaces \
    --single-transaction \
    --routines \
    --triggers \
    "$NATIVE_DB" \
    | gzip > "$DUMP_FILE"
  log "    Dump size: $(du -sh "$DUMP_FILE" | cut -f1)"

  # ── 4. Create DB + user in Docker MySQL and import dump ───────────────────
  log ""
  log "==> 4. Creating DB '$NATIVE_DB' in Docker MySQL and importing ..."
  docker compose -f "$COMPOSE_FILE" exec -T db \
    mysql -u root -p"$MYSQL_ROOT_PASSWORD" <<SQL
CREATE DATABASE IF NOT EXISTS \`${NATIVE_DB}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${NATIVE_DB_USER}'@'%' IDENTIFIED BY '${NATIVE_DB_PASS}';
GRANT ALL PRIVILEGES ON \`${NATIVE_DB}\`.* TO '${NATIVE_DB_USER}'@'%';
FLUSH PRIVILEGES;
SQL
  zcat "$DUMP_FILE" \
    | docker compose -f "$COMPOSE_FILE" exec -T db \
        mysql -u root -p"$MYSQL_ROOT_PASSWORD" "$NATIVE_DB"
  log "    Import done."

  # ── 5. Patch wp-config.php: localhost → db ────────────────────────────────
  log ""
  log "==> 5. Patching wp-config.php DB_HOST ..."
  for wp_config in \
      "$DOCKER_DOCROOT/wp-config.php" \
      "$DOCKER_DOCROOT/wordpress/wp-config.php"; do
    if [ -f "$wp_config" ]; then
      sudo sed -i "s/'DB_HOST', 'localhost'/'DB_HOST', 'db'/g" "$wp_config"
      log "    Patched: $wp_config"
    fi
  done

  # ── 6. Write Docker vhost config ──────────────────────────────────────────
  log ""
  log "==> 6. Writing Docker vhost config ..."
  sudo mkdir -p "$APACHE_DATA_DIR/vhosts"
  VHOST_FILE="$APACHE_DATA_DIR/vhosts/${NEW_DOMAIN}.conf"
  sudo tee "$VHOST_FILE" > /dev/null <<VHOST
<VirtualHost *:80>
    ServerName ${NEW_DOMAIN}
    DocumentRoot /var/www/html/${WWW_SUBDIR}
    DirectoryIndex index.php index.html

    <Directory /var/www/html/${WWW_SUBDIR}>
        Options Indexes SymLinksIfOwnerMatch
        AllowOverride All
        Require all granted
    </Directory>

    ErrorLog  /var/log/apache2/${NEW_DOMAIN}-error.log
    CustomLog /var/log/apache2/${NEW_DOMAIN}-access.log combined
</VirtualHost>
VHOST
  log "    Written: $VHOST_FILE"

  # ── 7. Write native-Apache reverse-proxy snippet ──────────────────────────
  log ""
  log "==> 7. Writing native reverse-proxy snippet ..."
  PROXY_SNIPPET="$SCRIPT_DIR/native-proxy-${NEW_DOMAIN}.conf"
  cat > "$PROXY_SNIPPET" <<PROXY
# ─── Reverse proxy: ${NEW_DOMAIN} → Docker container ───
# Add this to /etc/apache2/sites-available/${NEW_DOMAIN}.conf
# then:
#   sudo a2enmod proxy proxy_http
#   sudo a2ensite ${NEW_DOMAIN}.conf
#   certbot --apache -d ${NEW_DOMAIN}   # get SSL cert + HTTPS redirect
#   sudo apache2ctl configtest && sudo systemctl reload apache2

<VirtualHost *:80>
    ServerName ${NEW_DOMAIN}
    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:${APACHE_HTTP_PORT}/
    ProxyPassReverse / http://127.0.0.1:${APACHE_HTTP_PORT}/
    # Certbot will insert the HTTPS redirect automatically
</VirtualHost>
PROXY
  log "    Written: $PROXY_SNIPPET"
done

# ─── 8. Reload Docker Apache to pick up new vhosts ────────────────────────────
log ""
log "==> 8. Reloading Docker Apache ..."
docker compose -f "$COMPOSE_FILE" exec -T apache apache2ctl graceful 2>&1 \
  | tee -a "$LOG" \
  || { log "    graceful reload failed – restarting apache container ..."; \
       docker compose -f "$COMPOSE_FILE" restart apache 2>&1 | tee -a "$LOG"; }
log "    Apache reloaded."

# ─── Summary ──────────────────────────────────────────────────────────────────
log ""
log "================================================================"
log "  Migration complete!   Log: $LOG"
log "================================================================"
log ""
log "NEXT STEPS"
log "──────────"
log ""
log "1. Enable proxy modules on native Apache (once only):"
log "     sudo a2enmod proxy proxy_http"
log ""
log "2. Install reverse-proxy vhosts (generated above):"
for site_def in "${SITES[@]}"; do
  IFS='|' read -r _ _ _ _ _ NEW_DOMAIN <<< "$site_def"
  PROXY_SNIPPET="$SCRIPT_DIR/native-proxy-${NEW_DOMAIN}.conf"
  log "     sudo cp '$PROXY_SNIPPET' /etc/apache2/sites-available/${NEW_DOMAIN}.conf"
  log "     sudo a2ensite ${NEW_DOMAIN}.conf"
done
log ""
log "3. Get SSL certificates for the copy domains:"
for site_def in "${SITES[@]}"; do
  IFS='|' read -r _ _ _ _ _ NEW_DOMAIN <<< "$site_def"
  log "     sudo certbot --apache -d ${NEW_DOMAIN}"
done
log ""
log "4. Reload native Apache:"
log "     sudo apache2ctl configtest && sudo systemctl reload apache2"
log ""
log "5. Fix WordPress site URLs (run once per site after DNS is live):"
for site_def in "${SITES[@]}"; do
  IFS='|' read -r _ WWW_SUBDIR _ _ _ NEW_DOMAIN <<< "$site_def"
  for wp_subpath in "" "/wordpress"; do
    log "     # if WordPress lives at /var/www/html/${WWW_SUBDIR}${wp_subpath}:"
    log "     docker compose exec apache wp --path=/var/www/html/${WWW_SUBDIR}${wp_subpath} \\"
    log "       search-replace 'https://<old-domain>' 'https://${NEW_DOMAIN}' --all-tables --skip-columns=guid"
  done
done
log ""
log "6. Verify:"
for site_def in "${SITES[@]}"; do
  IFS='|' read -r _ _ _ _ _ NEW_DOMAIN <<< "$site_def"
  log "     curl -Lv http://${NEW_DOMAIN}/"
done
log ""

