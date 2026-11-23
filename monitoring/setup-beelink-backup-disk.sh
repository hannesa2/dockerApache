#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

fail() { echo "ERROR: $*" >&2; exit 1; }
[[ $# == 1 && "$1" == --configure-existing ]] ||
    fail "Usage: sudo bash monitoring/setup-beelink-backup-disk.sh --configure-existing"
[[ "$(id -u)" == 0 && "$(hostname -s)" == beelink && -t 0 ]] ||
    fail "Run as root interactively on beelink."

disk=/dev/disk/by-id/ata-WDC_WD20SMZW-11JW8S0_WD-WXG1EA84H743
partition="${disk}-part1"
key=/etc/cryptsetup-keys.d/beelink-backup.key
mapper=/dev/mapper/beelink-backup
uuid=39874d63-f5a6-4903-ba29-7237e998f0c4
fs_uuid=fe91ecb5-c1a4-4e6c-9edf-f3c9c75cffe1

for cmd in cryptsetup lsblk findmnt systemctl flock python3 blkid; do
    command -v "$cmd" >/dev/null || fail "Missing command: $cmd"
done
exec 9>/run/lock/beelink-backup-disk-setup.lock
flock -n 9 || fail "Backup disk setup is already running."
[[ -b "$disk" && -b "$partition" && -b "$mapper" ]] ||
    fail "Expected disk and unlocked beelink-backup mapping must be present."
[[ ! -e "$key" && ! -L "$key" ]] || fail "Backup key already exists; inspect before retrying."
[[ ! -L /datenBackup ]] || fail "/datenBackup must not be a symlink."
if [[ -e /datenBackup ]]; then
    [[ -d /datenBackup && -z "$(ls -A /datenBackup)" ]] ||
        fail "/datenBackup must be an empty directory."
fi
! findmnt -rn -M /datenBackup >/dev/null || fail "/datenBackup is already mounted."
for config in /etc/crypttab /etc/fstab; do
    [[ -f "$config" && ! -L "$config" ]] || fail "Invalid configuration: $config"
    ! grep -Eq 'beelink-backup|/datenBackup|39874d63-f5a6-4903-ba29-7237e998f0c4' "$config" ||
        fail "Existing backup disk configuration in $config."
done
lsblk -sno TYPE "$(findmnt -rn -o SOURCE /)" | grep -qx crypt ||
    fail "The system filesystem containing the unlock key must be encrypted."
[[ "$(cryptsetup luksUUID "$partition")" == "$uuid" ]] || fail "Unexpected LUKS UUID."
[[ "$(blkid -s UUID -o value "$mapper")" == "$fs_uuid" &&
   "$(blkid -s TYPE -o value "$mapper")" == ext4 ]] || fail "Unexpected mapped filesystem."
backing="$(cryptsetup status beelink-backup | awk '$1 == "device:" {print $2}')"
[[ "$(readlink -f "$backing")" == "$(readlink -f "$partition")" ]] ||
    fail "Mapping belongs to a different device."
python3 - "$disk" <<'PY'
import json
import subprocess
import sys

device = json.loads(subprocess.check_output(
    ["lsblk", "--json", "--bytes", "-d", "-o",
     "TYPE,MODEL,SERIAL,SIZE,TRAN,ROTA", sys.argv[1]], text=True))["blockdevices"][0]
expected = ("disk", "WDC WD20SMZW-11JW8S0", "WD-WXG1EA84H743",
            2000365289472, "usb", True)
actual = (device["type"], device["model"].strip(), device["serial"],
          device["size"], device["tran"], bool(device["rota"]))
if actual != expected:
    raise SystemExit("ERROR: Unexpected disk identity; refusing configuration.")
PY

state="$(mktemp -d /root/beelink-backup-disk-setup.XXXXXXXX)"
trap 'status=$?; if [[ $status != 0 ]]; then echo "ERROR: Setup incomplete. Preserve $state and $key; inspect before retrying." >&2; fi' EXIT
cp -p /etc/fstab /etc/crypttab "$state/"
cryptsetup luksHeaderBackup "$partition" --header-backup-file "$state/luks-header-before.img"
install -d -o root -g root -m 0700 /etc/cryptsetup-keys.d
dd if=/dev/urandom of="$key" bs=64 count=1 status=none conv=excl
chmod 0400 "$key"
echo "Enter the existing LUKS passphrase to add the new key; the passphrase remains valid."
cryptsetup luksAddKey "$partition" "$key"
cryptsetup open --test-passphrase --key-file "$key" "$partition"
cryptsetup luksHeaderBackup "$partition" --header-backup-file "$state/luks-header-after.img"
install -d -o root -g root -m 0755 /datenBackup
printf 'beelink-backup UUID=%s %s luks,noauto,nofail,headless,timeout=5s,x-systemd.device-timeout=5s\n' \
    "$uuid" "$key" >> /etc/crypttab
printf '%s\n' '/dev/mapper/beelink-backup /datenBackup ext4 nofail,x-systemd.automount,x-systemd.device-timeout=10s,x-systemd.mount-timeout=30s 0 0' >> /etc/fstab
findmnt --verify --tab-file /etc/fstab
systemctl daemon-reload
systemctl start datenBackup.automount
systemctl start datenBackup.mount
[[ "$(findmnt -rn -M /datenBackup -t ext4 -o UUID)" == "$fs_uuid" ]] ||
    fail "Mounted filesystem UUID does not match expected value."
findmnt -M /datenBackup -o TARGET,SOURCE,FSTYPE,OPTIONS
echo "Existing encrypted backup disk is mounted at /datenBackup."
echo "Header backups and pre-change configuration: $state"
echo "Preserve the passphrase and back up the key and both headers securely off this host."
echo "Automatic unlock and boot without the disk must still be verified after a reboot."
