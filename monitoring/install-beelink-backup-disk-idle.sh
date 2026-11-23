#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C

[[ "$(id -u)" == 0 && "$(hostname -s)" == beelink && -t 0 ]] || {
    echo "Run as root interactively on beelink." >&2; exit 1;
}
root="$(cd -- "$(dirname -- "$0")" && pwd -P)"
[[ "$(systemctl is-enabled hd-idle.service 2>/dev/null || true)" == masked ]] || {
    echo "hd-idle must remain masked to protect other drives." >&2; exit 1;
}
for file in /usr/local/libexec/beelink-backup-disk-idle.py \
            /etc/systemd/system/beelink-backup-disk-idle.service; do
    [[ ! -e "$file" && ! -L "$file" ]] || {
        echo "Refusing to overwrite existing file: $file" >&2; exit 1;
    }
done
install -o root -g root -m 0755 "$root/beelink-backup-disk-idle.py" /usr/local/libexec/beelink-backup-disk-idle.py
install -o root -g root -m 0644 "$root/beelink-backup-disk-idle.service" /etc/systemd/system/beelink-backup-disk-idle.service
systemd-analyze verify /etc/systemd/system/beelink-backup-disk-idle.service
systemctl daemon-reload
systemctl enable --now beelink-backup-disk-idle.service
systemctl is-active --quiet beelink-backup-disk-idle.service
systemctl status beelink-backup-disk-idle.service --no-pager -n 10
