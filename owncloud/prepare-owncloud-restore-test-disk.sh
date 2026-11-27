#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 077

fail() { echo "ERROR: $*" >&2; exit 1; }
[[ $# == 1 && "$1" == --erase-and-prepare ]] ||
    fail "Usage: sudo bash $0 --erase-and-prepare"
[[ "$(id -u)" == 0 && "$(hostname -s)" == beelink && -t 0 ]] ||
    fail "Run interactively as root on beelink."

disk=/dev/disk/by-id/usb-SABRENT_SABRENT_DD5641988396F-0:0
serial=S5SVNF0R428175W
name=owncloud-restore-test
mapper=/dev/mapper/owncloud-restore-test
target=/datenRestoreTest

for cmd in cryptsetup mkfs.ext4 wipefs lsblk findmnt smartctl python3 mount blockdev flock; do
    command -v "$cmd" >/dev/null || fail "Missing command: $cmd"
done
exec 9>/run/lock/owncloud-restore-test-disk.lock
flock -n 9 || fail "Another disk preparation is running."
[[ -b "$disk" && ! -e "$mapper" ]] ||
    fail "Expected disk missing or test mapping already exists."
[[ "$(lsblk -bdno SIZE "$disk")" == 1000204886016 &&
   "$(lsblk -dno TYPE "$disk")" == disk &&
   "$(lsblk -dno TRAN "$disk")" == usb ]] ||
    fail "Unexpected disk size, type or transport."

smartctl -i -j -d sat "$disk" |
    python3 -c 'import json,sys
d=json.load(sys.stdin)
if (d.get("model_name"), d.get("serial_number")) != (
    "Samsung SSD 870 QVO 1TB", "S5SVNF0R428175W"):
    raise SystemExit("ERROR: Unexpected SSD model or serial number.")'

check_unused() {
    local node
    [[ -z "$(lsblk -nr -o MOUNTPOINTS "$disk" | tr -d '[:space:]')" ]] ||
        fail "Disk or a descendant is mounted or used as swap."
    while IFS= read -r node; do
        [[ -z "$(ls -A "/sys/class/block/$node/holders")" ]] ||
            fail "$node has active device-mapper/RAID holders."
    done < <(lsblk -nr -o KNAME "$disk")
    [[ ! -L "$target" ]] || fail "Test mountpoint is a symlink."
    if [[ -e "$target" ]]; then
        [[ -d "$target" && -z "$(ls -A "$target")" ]] ||
            fail "Test mountpoint is not an empty directory."
    fi
    ! findmnt -rn -M "$target" >/dev/null ||
        fail "Test mountpoint is already mounted."
}
check_unused
lsblk -o NAME,SIZE,MODEL,SERIAL,FSTYPE,MOUNTPOINTS "$disk"
printf '\nALL existing partitions and APFS data on Samsung %s will be erased.\n' "$serial"
printf 'No production or backup disk will be modified.\n'
printf 'Type the SSD serial number to confirm: '
read -r answer
[[ "$answer" == "$serial" ]] || fail "Confirmation does not match; nothing erased."
check_unused

trap 'status=$?; if (( status != 0 )); then echo "ERROR: Preparation failed. Do not rerun blindly; inspect disk/mapping state first." >&2; fi' EXIT
wipefs --all "$disk"
blockdev --rereadpt "$disk"
printf '\nChoose and securely retain a NEW disk passphrase (not your sudo password).\n'
cryptsetup luksFormat --type luks2 "$disk"
cryptsetup open "$disk" "$name"
mkfs.ext4 -L oc-restore-test -m 0 "$mapper"
install -d -o root -g root -m 0700 "$target"
mount -o nodev,nosuid,noexec "$mapper" "$target"
chmod 0700 "$target"
[[ "$(findmnt -rn -M "$target" -o SOURCE)" == "$mapper" &&
   "$(findmnt -rn -M "$target" -o FSTYPE)" == ext4 ]] ||
    fail "Unexpected mounted test filesystem."
printf '\nPrepared encrypted restore-test disk (manual unlock; no fstab changes).\n'
cryptsetup luksUUID "$disk"
findmnt -M "$target" -o TARGET,SOURCE,FSTYPE,OPTIONS
df -h "$target"
printf '\nKeep the passphrase safe. Back up the LUKS header to a separate safe location.\n'
printf 'No ownCloud data has been restored yet.\n'
