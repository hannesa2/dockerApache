#!/bin/bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

state_dir="/home/hannes/.local/state/gitlab-mac-backup"
pending_file="$state_dir/pending-id"
mkdir -p "$state_dir"

exec 9>"$state_dir/lock"
if ! flock -n 9; then
    echo "Another GitLab backup transfer is already running"
    exit 0
fi

if [ -s "$pending_file" ]; then
    IFS= read -r backup_id < "$pending_file"
    if [[ ! "$backup_id" =~ ^git16dr_[0-9]{8}T[0-9]{6}Z_19\.4\.1$ ]]; then
        echo "Invalid pending backup identifier" >&2
        exit 1
    fi
else
    backup_id="git16dr_$(date -u +%Y%m%dT%H%M%SZ)_19.4.1"
    printf '%s\n' "$backup_id" > "$pending_file.tmp"
    mv -f "$pending_file.tmp" "$pending_file"
fi

backup_file="${backup_id}_gitlab_backup.tar"
if ! docker exec gitlab test -s "/var/opt/gitlab/backups/$backup_file"; then
    docker exec gitlab gitlab-backup create "BACKUP=$backup_id"
fi

docker exec gitlab tar -tf "/var/opt/gitlab/backups/$backup_file" >/dev/null
docker exec gitlab tar -cf - \
    -C /var/opt/gitlab/backups "$backup_file" \
    -C /etc/gitlab gitlab.rb gitlab-secrets.json |
    ssh \
        -i /home/hannes/.ssh/gitlab-mac-backup \
        -o IdentitiesOnly=yes \
        -o BatchMode=yes \
        -o StrictHostKeyChecking=yes \
        -o UserKnownHostsFile=/home/hannes/.ssh/known_hosts \
        hannes@mac2016 "backup $backup_id"

docker exec gitlab rm -f "/var/opt/gitlab/backups/$backup_file"
rm -f "$pending_file"
echo "GitLab backup $backup_id transferred to mac2016"
