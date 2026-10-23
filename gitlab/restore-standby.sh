#!/bin/bash
set -euo pipefail

docker="${DOCKER:-$HOME/.orbstack/bin/docker}"
compose_file="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/docker-compose.yml"
data_root="${GITLAB_DATA_DIR:-/Volumes/SamsungT5_1T/gitlab}"
backup_root="${GITLAB_BACKUP_DIR:-$data_root/backups}"
restore_marker="$data_root/.standby-restore-in-progress"

backup_id="${1:-}"
if [ -z "$backup_id" ]; then
    backup_id="$(find "$backup_root" -mindepth 1 -maxdepth 1 -type d \
        -name 'git16dr_*_19.4.1' -print | sort | tail -n 1)"
    backup_id="${backup_id##*/}"
fi

if [[ ! "$backup_id" =~ ^git16dr_[0-9]{8}T[0-9]{6}Z_19\.4\.1$ ]]; then
    echo "ERROR: No valid GitLab 19.4.1 backup identifier supplied or found." >&2
    exit 64
fi

backup_dir="$backup_root/$backup_id"
backup_file="$backup_dir/${backup_id}_gitlab_backup.tar"
secrets_file="$backup_dir/gitlab-secrets.json"
if [ ! -s "$backup_file" ] || [ ! -s "$secrets_file" ]; then
    echo "ERROR: Backup archive or GitLab secrets file is missing for $backup_id." >&2
    exit 1
fi

if ! tar -tf "$backup_file" | grep -qx 'backup_information.yml'; then
    echo "ERROR: GitLab backup archive is incomplete." >&2
    exit 1
fi

for path in "$data_root/config" "$data_root/logs" "$data_root/data"; do
    if [ ! -d "$path" ]; then
        echo "ERROR: Expected GitLab data directory is missing: $path" >&2
        exit 1
    fi
    if [ ! -e "$restore_marker" ] &&
        find "$path" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
        echo "ERROR: Refusing to restore over non-empty directory: $path" >&2
        exit 1
    fi
done

free_kb="$(df -Pk "$data_root" | awk 'NR == 2 {print $4}')"
if [ -z "$free_kb" ] || [ "$free_kb" -lt $((200 * 1024 * 1024)) ]; then
    echo "ERROR: Refusing restore with less than 200 GiB of free space." >&2
    exit 1
fi

install -m 0600 "$secrets_file" "$data_root/config/gitlab-secrets.json"
if [ ! -e "$restore_marker" ]; then
    : > "$restore_marker"
    chmod 600 "$restore_marker"
fi

echo "Starting the pinned GitLab 19.4.1 standby container..."
"$docker" --context orbstack compose -f "$compose_file" up -d

echo "Preparing GitLab configuration..."
"$docker" --context orbstack compose -f "$compose_file" \
    exec -T gitlab gitlab-ctl reconfigure
"$docker" --context orbstack compose -f "$compose_file" \
    exec -T gitlab gitlab-ctl restart

echo "Waiting for GitLab services to initialize..."
ready=0
for _ in $(seq 1 180); do
    if "$docker" --context orbstack compose -f "$compose_file" \
        exec -T gitlab gitlab-ctl status >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 5
done
if [ "$ready" -ne 1 ]; then
    echo "ERROR: GitLab did not become ready; inspect container logs before retrying." >&2
    exit 1
fi

echo "Stopping GitLab application services before restore..."
"$docker" --context orbstack compose -f "$compose_file" exec -T gitlab gitlab-ctl stop puma
"$docker" --context orbstack compose -f "$compose_file" exec -T gitlab gitlab-ctl stop sidekiq

echo "Restoring backup $backup_id..."
printf 'yes\nyes\n' | "$docker" --context orbstack compose -f "$compose_file" exec -i gitlab \
    gitlab-backup restore "BACKUP=$backup_id"

echo "Restarting GitLab services..."
"$docker" --context orbstack compose -f "$compose_file" exec -T gitlab gitlab-ctl reconfigure
"$docker" --context orbstack compose -f "$compose_file" exec -T gitlab gitlab-ctl restart
"$docker" --context orbstack compose -f "$compose_file" exec -T gitlab gitlab-ctl status

rm -f "$restore_marker"
echo "GitLab standby restored from $backup_id."
