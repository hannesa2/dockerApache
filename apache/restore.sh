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
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-changeme_root}"
APACHE_DATA_DIR="${APACHE_DATA_DIR:-$SCRIPT_DIR/data}"

[[ -f "$BACKUP_PATH/backup-complete" && "$(cat "$BACKUP_PATH/backup-complete")" == "apache-backup-v2" ]] || {
    echo "ERROR: Missing complete v2 backup marker; legacy or incomplete backups require manual restoration." >&2
    exit 1
}
[[ -f "$BACKUP_PATH/databases.txt" ]] || { echo "ERROR: Backup database list missing." >&2; exit 1; }
databases=()
while IFS= read -r database || [[ -n "$database" ]]; do
    [[ "$database" =~ ^[a-zA-Z0-9_]+$ ]] || { echo "ERROR: Invalid database name in backup." >&2; exit 1; }
    databases+=("$database")
    [[ -s "$BACKUP_PATH/mysql_${database}.sql.gz" ]] || { echo "ERROR: Missing dump for $database." >&2; exit 1; }
    gzip -t "$BACKUP_PATH/mysql_${database}.sql.gz"
done < "$BACKUP_PATH/databases.txt"
[[ "${#databases[@]}" -gt 0 ]] || { echo "ERROR: Backup contains no databases." >&2; exit 1; }
for archive in www.tar.gz vhosts.tar.gz; do
    [[ -s "$BACKUP_PATH/$archive" ]] || { echo "ERROR: Missing archive: $archive." >&2; exit 1; }
    tar -tzf "$BACKUP_PATH/$archive" >/dev/null
done

echo "==> Restore from: $BACKUP_PATH"
for database in "${databases[@]}"; do
    read -r -p "WARNING: Replace MySQL database '$database'? [y/N] " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Skipped database '$database'."; continue; }
    echo "==> Restoring MySQL database '$database' ..."
    docker compose -f "$COMPOSE_FILE" exec -T db \
        mysql -u root -p"$MYSQL_ROOT_PASSWORD" \
        -e "DROP DATABASE IF EXISTS \`$database\`;" </dev/null
    gunzip -c "$BACKUP_PATH/mysql_${database}.sql.gz" | docker compose -f "$COMPOSE_FILE" exec -T db \
        mysql -u root -p"$MYSQL_ROOT_PASSWORD"
    echo "    Database '$database' restored."
done

# ── 2. Web root ───────────────────────────────────────────────────────────────
WWW_ARCHIVE="$BACKUP_PATH/www.tar.gz"
read -r -p "WARNING: Replace web root and vhost configs? [y/N] " confirm
if [[ "$confirm" =~ ^[Yy]$ ]]; then
    echo "==> Restoring web root ..."
    previous_suffix="$(date +%Y%m%d_%H%M%S)"
    [[ ! -e "$APACHE_DATA_DIR/www.pre-restore-$previous_suffix" &&
       ! -e "$APACHE_DATA_DIR/vhosts.pre-restore-$previous_suffix" ]] || {
        echo "ERROR: Pre-restore directory already exists." >&2; exit 1;
    }
    sudo mv "$APACHE_DATA_DIR/www" "$APACHE_DATA_DIR/www.pre-restore-$previous_suffix"
    sudo LANG=C tar -xzf "$WWW_ARCHIVE" -C "$APACHE_DATA_DIR"
    echo "    Web root restored."
    echo "==> Restoring vhost configs ..."
    sudo mv "$APACHE_DATA_DIR/vhosts" "$APACHE_DATA_DIR/vhosts.pre-restore-$previous_suffix"
    sudo LANG=C tar -xzf "$BACKUP_PATH/vhosts.tar.gz" -C "$APACHE_DATA_DIR"
    docker compose -f "$COMPOSE_FILE" restart apache
    echo "    Vhosts restored and Apache restarted."
    echo "    Previous directories retained with suffix .pre-restore-$previous_suffix."
else
    echo "Skipped web root and vhost configs."
fi

echo "==> Restore selections completed."
