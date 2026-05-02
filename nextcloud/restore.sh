#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# restore.sh  –  Restore Nextcloud PostgreSQL database + data/config files
#
# Usage:
#   ./restore.sh <backup-directory>
#
# <backup-directory> is the timestamped folder created by backup.sh, e.g.:
#   ./restore.sh ./backups/20260501_120000
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env if present
[ -f "$SCRIPT_DIR/.env" ] && source "$SCRIPT_DIR/.env"

BACKUP_PATH="${1:-}"
if [ -z "$BACKUP_PATH" ] || [ ! -d "$BACKUP_PATH" ]; then
    echo "ERROR: Please supply a valid backup directory as the first argument."
    echo "Usage: $0 <backup-directory>"
    exit 1
fi

COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
POSTGRES_DB="${POSTGRES_DB:-nextcloud}"
POSTGRES_USER="${POSTGRES_USER:-nextcloud}"
NEXTCLOUD_DATA_DIR="${NEXTCLOUD_DATA_DIR:-$SCRIPT_DIR/data}"

echo "==> Restore from: $BACKUP_PATH"
echo ""
read -r -p "WARNING: This will OVERWRITE the current database and files. Continue? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# ── 1. Restore PostgreSQL ─────────────────────────────────────────────────────
DB_DUMP="$BACKUP_PATH/postgres_${POSTGRES_DB}.sql.gz"
if [ -f "$DB_DUMP" ]; then
    echo "==> Enabling Nextcloud maintenance mode ..."
    docker compose -f "$COMPOSE_FILE" exec -T nextcloud \
        php occ maintenance:mode --on 2>/dev/null || true

    echo "==> Dropping and re-creating database '$POSTGRES_DB' ..."
    docker compose -f "$COMPOSE_FILE" exec -T db \
        psql -U "$POSTGRES_USER" -c "DROP DATABASE IF EXISTS \"$POSTGRES_DB\";"
    docker compose -f "$COMPOSE_FILE" exec -T db \
        psql -U "$POSTGRES_USER" -c "CREATE DATABASE \"$POSTGRES_DB\" OWNER \"$POSTGRES_USER\";"

    echo "==> Restoring database from $DB_DUMP ..."
    gunzip -c "$DB_DUMP" | docker compose -f "$COMPOSE_FILE" exec -T db \
        psql -U "$POSTGRES_USER" "$POSTGRES_DB"
    echo "    Database restored."
else
    echo "WARNING: No database dump found at $DB_DUMP – skipping database restore."
fi

# ── 2. Restore config & custom apps ──────────────────────────────────────────
CONFIG_ARCHIVE="$BACKUP_PATH/nextcloud_config.tar.gz"
if [ -f "$CONFIG_ARCHIVE" ]; then
    echo "==> Restoring Nextcloud config and custom apps ..."
    rm -rf "$NEXTCLOUD_DATA_DIR/nextcloud/config" \
           "$NEXTCLOUD_DATA_DIR/nextcloud/custom_apps"
    tar -xzf "$CONFIG_ARCHIVE" -C "$NEXTCLOUD_DATA_DIR/nextcloud"
    echo "    Config restored."
else
    echo "WARNING: $CONFIG_ARCHIVE not found – skipping."
fi

# ── 3. Restore user data ──────────────────────────────────────────────────────
DATA_ARCHIVE="$BACKUP_PATH/nextcloud_data.tar.gz"
if [ -f "$DATA_ARCHIVE" ]; then
    echo "==> Restoring Nextcloud user data (this may take a while) ..."
    rm -rf "$NEXTCLOUD_DATA_DIR/nextcloud/data"
    tar -xzf "$DATA_ARCHIVE" -C "$NEXTCLOUD_DATA_DIR/nextcloud"
    echo "    User data restored."
else
    echo "WARNING: $DATA_ARCHIVE not found – skipping."
fi

# ── 4. Disable maintenance mode & repair ─────────────────────────────────────
echo "==> Running Nextcloud upgrade/repair checks ..."
docker compose -f "$COMPOSE_FILE" exec -T nextcloud php occ upgrade 2>/dev/null || true
docker compose -f "$COMPOSE_FILE" exec -T nextcloud php occ maintenance:repair 2>/dev/null || true

echo "==> Disabling maintenance mode ..."
docker compose -f "$COMPOSE_FILE" exec -T nextcloud php occ maintenance:mode --off

echo "==> Restore complete."

