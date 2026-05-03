#!/usr/bin/env bash
# restore.sh – Restore MySQL database + web root + vhost configs
set -euo pipefail

# Use POSIX locale – always available on macOS and Linux, silences tar/locale warnings
export LC_ALL=C
export LANG=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && source "$SCRIPT_DIR/.env"

BACKUP_PATH="${1:-}"
if [ -z "$BACKUP_PATH" ] || [ ! -d "$BACKUP_PATH" ]; then
    echo "ERROR: Please supply a valid backup directory."
    echo "Usage: $0 <backup-directory>"
    exit 1
fi

COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
MYSQL_DATABASE="${MYSQL_DATABASE:-webapp}"
MYSQL_USER="${MYSQL_USER:-webapp}"
MYSQL_PASSWORD="${MYSQL_PASSWORD:-changeme_db}"
APACHE_DATA_DIR="${APACHE_DATA_DIR:-$SCRIPT_DIR/data}"

echo "==> Restore from: $BACKUP_PATH"
read -r -p "WARNING: This will OVERWRITE current data. Continue? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# ── 1. MySQL ──────────────────────────────────────────────────────────────────
DB_DUMP="$BACKUP_PATH/mysql_${MYSQL_DATABASE}.sql.gz"
if [ -f "$DB_DUMP" ]; then
    echo "==> Restoring MySQL database '$MYSQL_DATABASE' ..."
    docker compose -f "$COMPOSE_FILE" exec -T db \
        mysql -u root -p"${MYSQL_ROOT_PASSWORD:-changeme_root}" 2>/dev/null \
        -e "DROP DATABASE IF EXISTS \`$MYSQL_DATABASE\`; CREATE DATABASE \`$MYSQL_DATABASE\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; GRANT ALL ON \`$MYSQL_DATABASE\`.* TO '$MYSQL_USER'@'%';"
    gunzip -c "$DB_DUMP" | docker compose -f "$COMPOSE_FILE" exec -T db \
        mysql -u "$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" 2>/dev/null
    echo "    Database restored."
else
    echo "WARNING: $DB_DUMP not found – skipping."
fi

# ── 2. Web root ───────────────────────────────────────────────────────────────
WWW_ARCHIVE="$BACKUP_PATH/www.tar.gz"
if [ -f "$WWW_ARCHIVE" ]; then
    echo "==> Restoring web root ..."
    rm -rf "$APACHE_DATA_DIR/www"
    LANG=C tar -xzf "$WWW_ARCHIVE" -C "$APACHE_DATA_DIR"
    echo "    Web root restored."
fi

# ── 3. Vhost configs ──────────────────────────────────────────────────────────
VHOST_ARCHIVE="$BACKUP_PATH/vhosts.tar.gz"
if [ -f "$VHOST_ARCHIVE" ]; then
    echo "==> Restoring vhost configs ..."
    rm -rf "$APACHE_DATA_DIR/vhosts"
    LANG=C tar -xzf "$VHOST_ARCHIVE" -C "$APACHE_DATA_DIR"
    docker compose -f "$COMPOSE_FILE" restart apache
    echo "    Vhosts restored and Apache restarted."
fi

echo "==> Restore complete."

