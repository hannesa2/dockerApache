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

# Fix "Failed to set default locale" on macOS / minimal Linux environments
export LANG=C.UTF-8
export LC_ALL=C.UTF-8

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
tar -czf "$BACKUP_PATH/www.tar.gz" -C "$APACHE_DATA_DIR" www
echo "    Saved: www.tar.gz"

# ── 3. Vhost configs ──────────────────────────────────────────────────────────
echo "==> Archiving vhost configs ..."
tar -czf "$BACKUP_PATH/vhosts.tar.gz" -C "$APACHE_DATA_DIR" vhosts
echo "    Saved: vhosts.tar.gz"

echo "==> Backup complete → $BACKUP_PATH"
du -sh "$BACKUP_PATH"

