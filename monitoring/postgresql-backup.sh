#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

backup_root="${POSTGRES_BACKUP_ROOT:-/daten/sicherung.postgresql/dump}"
mount_target="${POSTGRES_BACKUP_MOUNT:-/daten}"
container="${POSTGRES_OWNCLOUD_CONTAINER:-owncloud-staging-db-1}"

fail() { echo "ERROR: $*" >&2; exit 1; }

mountpoint -q "$mount_target" || fail "Backup filesystem is not mounted: $mount_target"
mkdir -p "$backup_root"
[[ -d "$backup_root" && ! -L "$backup_root" ]] || fail "Invalid backup directory: $backup_root"
exec 9>"$backup_root/.postgresql-backup.lock"
flock -n 9 || fail "Another PostgreSQL backup is already running."

managed="$backup_root/daily"
mkdir -p "$managed"
[[ -d "$managed" && ! -L "$managed" ]] || fail "Invalid managed directory: $managed"
chmod 700 "$managed"
staging="$(mktemp -d "$managed/.incomplete.XXXXXXXX")"
cleanup() {
    if [[ -n "$staging" && -d "$staging" && ! -L "$staging" ]]; then
        rm -rf -- "$staging"
    fi
}
trap cleanup EXIT

dump_host() {
    local database="$1" file="$staging/host-$1.dump"
    echo "Backing up host database: $database"
    pg_dump -w -h localhost -U hannes -d "$database" -Fc -f "$file"
    verify "$file"
}

verify() {
    local file="$1" entries tables
    [[ -s "$file" ]] || fail "Empty dump: $file"
    entries="$(pg_restore -l "$file")" || fail "Cannot read archive: $file"
    pg_restore -f /dev/null "$file" || fail "Cannot read all archive data: $file"
    tables="$(printf '%s\n' "$entries" | grep -c ' TABLE DATA ' || true)"
    echo "Verified archive: $(basename "$file") ($(wc -c < "$file" | tr -d ' ') bytes; $tables table-data entries)"
    if [[ "$tables" -eq 0 ]]; then
        echo "WARNING: No table-data entries in $(basename "$file")."
    fi
}

for database in MXServer camDoor camHinten camPi3; do
    dump_host "$database"
done

echo "Backing up Docker ownCloud database: $container / owncloud_stage"
docker exec "$container" pg_dump -w -U owncloud_stage -d owncloud_stage -Fc \
    > "$staging/docker-owncloud_stage.dump"
verify "$staging/docker-owncloud_stage.dump"

timestamp="$(date '+%Y%m%d_%H%M%S')"
destination="$managed/$timestamp"
[[ ! -e "$destination" && ! -L "$destination" ]] || fail "Backup already exists: $destination"
printf 'Completed: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" > "$staging/backup-complete"
mv -T -- "$staging" "$destination"
staging=""
echo "Completed: $destination"
echo "The postgres maintenance database is excluded."
echo "Database-only backup: ownCloud files, configuration, and PostgreSQL roles are not included."

expired_list="$(mktemp "$managed/.expired.XXXXXXXX")"
if ! find "$managed" -mindepth 1 -maxdepth 1 -type d \
    -regextype posix-extended -regex '.*/[0-9]{8}_[0-9]{6}' -mtime +29 -print0 > "$expired_list"; then
    rm -f -- "$expired_list"
    fail "Could not enumerate expired backups."
fi
while IFS= read -r -d '' old; do
    [[ ! -L "$old" && -f "$old/backup-complete" && ! -L "$old/backup-complete" ]] || continue
    echo "Removing expired managed backup: $old"
    rm -rf -- "$old"
done < "$expired_list"
rm -f -- "$expired_list"
