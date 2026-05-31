#!/usr/bin/env bash
# backup.sh – Backup MySQL database + web root + vhost configs
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && source "$SCRIPT_DIR/.env"

BACKUP_ROOT="${1:-${BACKUP_DIR:-$SCRIPT_DIR/backups}}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_PATH="$BACKUP_ROOT/$TIMESTAMP"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
MYSQL_DATABASE="${MYSQL_DATABASE:-webapp}"
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-changeme_root}"
APACHE_DATA_DIR="${APACHE_DATA_DIR:-$SCRIPT_DIR/data}"

# Use POSIX locale – always available on macOS and Linux, silences tar/locale warnings
export LC_ALL=C
export LANG=C

echo "==> Backup started: $TIMESTAMP"
echo "    Backup path : $BACKUP_PATH"
mkdir -p "$BACKUP_PATH"

# ── 1. MySQL dump (root user + --no-tablespaces avoids PROCESS privilege) ─────
echo "==> Dumping MySQL database '$MYSQL_DATABASE' ..."
docker compose -f "$COMPOSE_FILE" exec -T db \
    mysqldump \
        -u root -p"$MYSQL_ROOT_PASSWORD" \
        --no-tablespaces \
        --single-transaction \
        --routines \
        --triggers \
        "$MYSQL_DATABASE" \
    | gzip > "$BACKUP_PATH/mysql_${MYSQL_DATABASE}.sql.gz"
echo "    Saved: mysql_${MYSQL_DATABASE}.sql.gz"

# ── 2. Web root ───────────────────────────────────────────────────────────────
echo "==> Archiving web root ..."
sudo LANG=C tar -cvzf "$BACKUP_PATH/www.tar.gz" -C "$APACHE_DATA_DIR" \
    --exclude='www/.git' \
    --exclude='www/.gitignore' \
    --exclude='www/.svn' \
    --exclude='www/*/.git' \
    --exclude='www/*/node_modules' \
    --exclude='www/*/.cache' \
    --exclude='www/*/tmp' \
    --exclude='www/*/cache' \
    www | { grep '/$' || true; }
echo "    Saved: www.tar.gz"

# ── 3. Vhost configs ──────────────────────────────────────────────────────────
echo "==> Archiving vhost configs ..."
sudo LANG=C tar -cvzf "$BACKUP_PATH/vhosts.tar.gz" -C "$APACHE_DATA_DIR" \
    --exclude='vhosts/.git' \
    vhosts | { grep '/$' || true; }
echo "    Saved: vhosts.tar.gz"

echo "==> Backup complete → $BACKUP_PATH"
du -sh "$BACKUP_PATH"

