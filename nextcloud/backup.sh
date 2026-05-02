#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# backup.sh  –  Backup Nextcloud PostgreSQL database + data/config files
#
# Usage:
#   ./backup.sh [backup-directory]
#
# If no backup directory is supplied the script uses BACKUP_DIR from .env,
# or falls back to ./backups.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env if present
[ -f "$SCRIPT_DIR/.env" ] && source "$SCRIPT_DIR/.env"

BACKUP_ROOT="${1:-${BACKUP_DIR:-$SCRIPT_DIR/backups}}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_PATH="$BACKUP_ROOT/$TIMESTAMP"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

POSTGRES_DB="${POSTGRES_DB:-nextcloud}"
POSTGRES_USER="${POSTGRES_USER:-nextcloud}"
NEXTCLOUD_DATA_DIR="${NEXTCLOUD_DATA_DIR:-$SCRIPT_DIR/data}"

echo "==> Backup started: $TIMESTAMP"
echo "    Backup path : $BACKUP_PATH"
mkdir -p "$BACKUP_PATH"

# ── 1. PostgreSQL dump ────────────────────────────────────────────────────────
echo "==> Dumping PostgreSQL database '$POSTGRES_DB' ..."
docker compose -f "$COMPOSE_FILE" exec -T db \
    pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" \
    | gzip > "$BACKUP_PATH/postgres_${POSTGRES_DB}.sql.gz"
echo "    Saved: postgres_${POSTGRES_DB}.sql.gz"

# ── 2. Nextcloud config & custom apps ────────────────────────────────────────
echo "==> Archiving Nextcloud config ..."
tar -czf "$BACKUP_PATH/nextcloud_config.tar.gz" \
    -C "$NEXTCLOUD_DATA_DIR/nextcloud" config custom_apps
echo "    Saved: nextcloud_config.tar.gz"

# ── 3. Nextcloud user data (can be large) ────────────────────────────────────
echo "==> Archiving Nextcloud user data (this may take a while) ..."
tar -czf "$BACKUP_PATH/nextcloud_data.tar.gz" \
    -C "$NEXTCLOUD_DATA_DIR/nextcloud" data
echo "    Saved: nextcloud_data.tar.gz"

# ── Done ─────────────────────────────────────────────────────────────────────
echo "==> Backup complete → $BACKUP_PATH"
du -sh "$BACKUP_PATH"

