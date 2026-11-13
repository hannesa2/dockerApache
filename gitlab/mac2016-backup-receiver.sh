#!/bin/bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077
export PATH="/Applications/OrbStack.app/Contents/MacOS/xbin:/usr/local/bin:/usr/bin:/bin"

trap 'status=$?; printf "ERROR: receiver command failed (status %d) at line %d: %s\n" "$status" "$LINENO" "$BASH_COMMAND" >&2; exit "$status"' ERR

case "${SSH_ORIGINAL_COMMAND:-}" in
    "backup git16dr_"*)
        backup_id="${SSH_ORIGINAL_COMMAND#backup }"
        ;;
    *)
        echo "Rejected backup request" >&2
        exit 64
        ;;
esac

if [[ ! "$backup_id" =~ ^git16dr_[0-9]{8}T[0-9]{6}Z_[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Rejected invalid backup identifier" >&2
    exit 64
fi

backup_root="${GITLAB_BACKUP_DIR:-$HOME/server/gitlab/backups}"
data_root="${GITLAB_DATA_DIR:-$HOME/server/gitlab}"
state_dir="$HOME/.local/state/gitlab-mac-sync"
if [ ! -d "$backup_root" ] || [ -L "$backup_root" ]; then
    echo "Backup destination is unavailable: $backup_root" >&2
    exit 69
fi

mkdir -p "$state_dir"
if ! mkdir "$state_dir/lock" 2>/dev/null; then
    echo "Another GitLab mirror restore is running, or its lock remains: $state_dir/lock" >&2
    exit 75
fi

staging_dir=""
work_dir="$backup_root/.sync"
container_work="/gitlab-backup-archives/.sync"
services_stopped=0
restore_started=0
work_created=0
cleanup() {
    status=$?
    trap - EXIT
    if [ "$services_stopped" -eq 1 ]; then
        if [ "$restore_started" -eq 1 ]; then
            for service in nginx puma sidekiq; do
                docker exec gitlab16 gitlab-ctl stop "$service" || status=1
            done
            echo "ERROR: GitLab restore failed; nginx, puma and sidekiq remain stopped. Retry the sync; do not serve partially restored data." >&2
        else
            secrets_restored=1
            if docker exec gitlab16 test -f "$container_work/previous-secrets.json"; then
                if ! docker exec gitlab16 cp "$container_work/previous-secrets.json" /etc/gitlab/gitlab-secrets.json ||
                    ! docker exec gitlab16 gitlab-ctl reconfigure; then
                    echo "ERROR: Could not restore the mirror's previous secrets; application services remain stopped." >&2
                    secrets_restored=0
                    status=1
                fi
            fi
            if [ "$secrets_restored" -eq 1 ]; then
                for service in nginx puma sidekiq; do
                    docker exec gitlab16 gitlab-ctl start "$service" || status=1
                done
            fi
        fi
    fi
    if [ "$work_created" -eq 1 ]; then
        docker exec gitlab16 rm -rf -- "$container_work" || status=1
    fi
    if [ -n "$staging_dir" ] && [ -d "$staging_dir" ]; then
        rm -rf -- "$staging_dir" || status=1
    fi
    rmdir "$state_dir/lock" || status=1
    exit "$status"
}
trap cleanup EXIT

mirror_version="$(docker exec gitlab16 cat /opt/gitlab/embedded/service/gitlab-rails/VERSION)"
if [[ ! "$mirror_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
    [ "$(docker inspect --format '{{.Config.Image}} {{.State.Running}}' gitlab16)" != "gitlab/gitlab-ce:$mirror_version-ce.0 true" ]; then
    echo "Expected a running, version-pinned GitLab CE mirror; refusing restore." >&2
    exit 65
fi
mounts="$(docker inspect --format '{{range .Mounts}}{{println .Source .Destination}}{{end}}' gitlab16)"
for mount in "$backup_root /gitlab-backup-archives" "$data_root/config /etc/gitlab" "$data_root/data /var/opt/gitlab"; do
    if ! printf '%s\n' "$mounts" | grep -Fxq "$mount"; then
        echo "GitLab mirror storage is inconsistent; refusing restore." >&2
        exit 65
    fi
done
if ! docker exec gitlab16 sh -c '
    if [ -e "$1" ] || [ -L "$1" ]; then
        [ -d "$1" ] && [ ! -L "$1" ] || exit 1
        entries="$(find "$1" -mindepth 1 -maxdepth 1 -print)" || exit 1
        [ -z "$entries" ]
    fi
' sh "$container_work"; then
    echo "Previous temporary restore workspace is not empty: $work_dir; inspect it before retrying." >&2
    exit 75
fi

free_kb="$(/bin/df -Pk "$backup_root" | /usr/bin/awk 'NR == 2 {print $4}')"
if [ -z "$free_kb" ] || [ "$free_kb" -lt $((50 * 1024 * 1024)) ]; then
    echo "Refusing backup transfer: less than 50 GiB is available" >&2
    exit 70
fi

staging_dir="$(/usr/bin/mktemp -d "$backup_root/.incoming.XXXXXX")"
bundle="$staging_dir/bundle.tar"
backup_file="${backup_id}_gitlab_backup.tar"

/bin/cat > "$bundle"

members="$(/usr/bin/tar -tf "$bundle")"
expected="$(printf '%s\ngitlab.rb\ngitlab-secrets.json' "$backup_file")"
if [ "$members" != "$expected" ]; then
    echo "Rejected unexpected backup bundle contents" >&2
    exit 65
fi

if ! /usr/bin/tar -tvf "$bundle" |
    /usr/bin/awk 'substr($0, 1, 1) != "-" { invalid = 1 } END { exit invalid }'; then
    echo "Rejected non-regular backup bundle entries" >&2
    exit 65
fi

/usr/bin/tar -xf "$bundle" -C "$staging_dir"
if ! /usr/bin/tar -tf "$staging_dir/$backup_file" |
    /usr/bin/awk '$0 == "backup_information.yml" { found = 1 } END { exit !found }'; then
    echo "Rejected incomplete GitLab backup archive" >&2
    exit 65
fi

/bin/rm -f "$bundle"

docker exec gitlab16 sh -c 'if [ -d "$1" ]; then rmdir "$1"; fi' sh "$container_work"
/bin/mv "$staging_dir" "$work_dir"
staging_dir=""
work_created=1
docker exec gitlab16 chmod 0711 /gitlab-backup-archives
docker exec gitlab16 chown -R git:git "$container_work"
docker exec gitlab16 chmod 0700 "$container_work"
docker exec gitlab16 chmod 0600 "$container_work/$backup_file" "$container_work/gitlab-secrets.json"

# Version validation and unpacking finish before stopping the mirror.
docker exec gitlab16 gitlab-rake gitlab:backup:verify "BACKUP=$backup_id"
docker exec gitlab16 cp /etc/gitlab/gitlab-secrets.json "$container_work/previous-secrets.json"
services_stopped=1
for service in nginx puma sidekiq; do
    echo "Stopping GitLab $service before applying source secrets." >&2
    docker exec gitlab16 gitlab-ctl stop "$service"
done
docker exec gitlab16 cp "$container_work/gitlab-secrets.json" /etc/gitlab/gitlab-secrets.json
echo "Reconfiguring GitLab with source encryption secrets." >&2
docker exec gitlab16 gitlab-ctl reconfigure
for service in nginx puma sidekiq; do
    echo "Stopping GitLab $service before restoring the snapshot." >&2
    docker exec gitlab16 gitlab-ctl stop "$service"
done

printf '%s\n' "$backup_id" > "$state_dir/restore-in-progress"
restore_started=1
echo "Restoring GitLab snapshot $backup_id." >&2
docker exec gitlab16 gitlab-backup restore "BACKUP=$backup_id" force=yes
docker exec gitlab16 gitlab-ctl reconfigure
docker exec gitlab16 gitlab-ctl restart

ready=0
for attempt in $(seq 1 120); do
    if docker exec gitlab16 curl --fail --silent --max-time 10 \
        -H 'Host: git16.mxtracks.info' http://127.0.0.1/-/readiness >/dev/null; then
        ready=1
        break
    fi
    sleep 5
done
if [ "$ready" -ne 1 ]; then
    for service in nginx puma sidekiq; do
        docker exec gitlab16 gitlab-ctl stop "$service"
    done
    echo "GitLab did not become ready after restore; inspect its logs." >&2
    exit 1
fi
services_stopped=0
rm -f "$state_dir/restore-in-progress"
printf '%s\n' "$backup_id" > "$state_dir/last-restored-id"
echo "GitLab mirror restored from $backup_id; temporary archive will be removed."
