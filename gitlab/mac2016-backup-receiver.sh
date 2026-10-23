#!/bin/bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

case "${SSH_ORIGINAL_COMMAND:-}" in
    "backup git16dr_"*)
        backup_id="${SSH_ORIGINAL_COMMAND#backup }"
        ;;
    *)
        echo "Rejected backup request" >&2
        exit 64
        ;;
esac

if [[ ! "$backup_id" =~ ^git16dr_[0-9]{8}T[0-9]{6}Z_19\.4\.1$ ]]; then
    echo "Rejected invalid backup identifier" >&2
    exit 64
fi

backup_root="/Volumes/SamsungT5_1T/gitlab/backups"
if [ ! -d "$backup_root" ]; then
    echo "Backup destination is not mounted: $backup_root" >&2
    exit 69
fi

free_kb="$(/bin/df -Pk "$backup_root" | /usr/bin/awk 'NR == 2 {print $4}')"
if [ -z "$free_kb" ] || [ "$free_kb" -lt $((200 * 1024 * 1024)) ]; then
    echo "Refusing backup transfer: less than 200 GiB would remain available" >&2
    exit 70
fi

staging_dir="$(/usr/bin/mktemp -d "$backup_root/.incoming.XXXXXX")"
trap 'rm -rf "$staging_dir"' EXIT
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

destination="$backup_root/$backup_id"
if [ -e "$destination" ]; then
    if [ ! -L "$backup_root/$backup_file" ]; then
        /bin/ln -s "$backup_id/$backup_file" "$backup_root/$backup_file"
    fi
    echo "Backup already received: $backup_id"
    exit 0
fi

/bin/mv "$staging_dir/gitlab.rb" "$staging_dir/source-gitlab.rb"
/bin/chmod 600 "$staging_dir/$backup_file" "$staging_dir/source-gitlab.rb" \
    "$staging_dir/gitlab-secrets.json"
/bin/mv "$staging_dir" "$destination"
trap - EXIT
/bin/ln -s "$backup_id/$backup_file" "$backup_root/$backup_file"

count=0
while IFS= read -r old_backup; do
    case "${old_backup##*/}" in
        git16dr_[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z_19.4.1)
            count=$((count + 1))
            if [ "$count" -gt 3 ]; then
                /bin/rm -rf -- "$old_backup"
                backup_file="${old_backup##*/}_gitlab_backup.tar"
                if [ -L "$backup_root/$backup_file" ]; then
                    /bin/rm -- "$backup_root/$backup_file"
                fi
            fi
            ;;
    esac
done < <(/usr/bin/find "$backup_root" -mindepth 1 -maxdepth 1 -type d -name 'git16dr_*' -print | /usr/bin/sort -r)

echo "Stored GitLab backup $backup_id"
