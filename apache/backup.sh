#!/usr/bin/env bash
# backup.sh – Backup MySQL database + web root + vhost configs
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && source "$SCRIPT_DIR/.env"

BACKUP_ROOT="${1:-${BACKUP_DIR:-$SCRIPT_DIR/backups}}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_PATH="$BACKUP_ROOT/$TIMESTAMP"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-changeme_root}"
APACHE_DATA_DIR="${APACHE_DATA_DIR:-$SCRIPT_DIR/data}"
read -r -a databases <<< "${MYSQL_BACKUP_DATABASES:-wordpress wordpress_mxtracks}"
[[ "${#databases[@]}" -gt 0 ]] || { echo "ERROR: No backup databases specified." >&2; exit 1; }
seen_databases=()
for database in "${databases[@]}"; do
    [[ "$database" =~ ^[a-zA-Z0-9_]+$ ]] || { echo "ERROR: Invalid database name: $database" >&2; exit 1; }
    for other in "${seen_databases[@]:-}"; do
        [[ "$database" != "$other" ]] || { echo "ERROR: Duplicate database: $database" >&2; exit 1; }
    done
    seen_databases+=("$database")
done
umask 077

# Use POSIX locale – always available on macOS and Linux, silences tar/locale warnings
export LC_ALL=C
export LANG=C

echo "==> Backup started: $TIMESTAMP"
echo "    Backup path : $BACKUP_PATH"
mkdir -p "$BACKUP_ROOT"
mkdir "$BACKUP_PATH"

# ── 1. MySQL dump (root user + --no-tablespaces avoids PROCESS privilege) ─────
for database in "${databases[@]}"; do
    echo "==> Dumping MySQL database '$database' ..."
    docker compose -f "$COMPOSE_FILE" exec -T db \
        mysqldump \
            -u root -p"$MYSQL_ROOT_PASSWORD" \
            --no-tablespaces \
            --single-transaction \
            --routines --events --triggers \
            --databases "$database" \
        | gzip > "$BACKUP_PATH/mysql_${database}.sql.gz"
    gzip -t "$BACKUP_PATH/mysql_${database}.sql.gz"
    echo "    Saved: mysql_${database}.sql.gz"
done

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

printf '%s\n' "${databases[@]}" > "$BACKUP_PATH/databases.txt"
printf 'apache-backup-v2\n' > "$BACKUP_PATH/backup-complete"
echo "==> Backup complete → $BACKUP_PATH"
du -sh "$BACKUP_PATH"
