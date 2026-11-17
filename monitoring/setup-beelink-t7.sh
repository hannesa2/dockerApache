#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

fail() { echo "ERROR: $*" >&2; exit 1; }
[[ $# == 1 && "$1" == --erase-confirmed-t7 ]] ||
    fail "Usage: sudo bash monitoring/setup-beelink-t7.sh --erase-confirmed-t7"
[[ "$(id -u)" == 0 && "$(hostname -s)" == beelink ]] || fail "Run as root on beelink."
disk=/dev/disk/by-id/usb-Samsung_PSSD_T7_S5T7NJ0R101857H-0:0
key=/etc/cryptsetup-keys.d/beelink-t7.key
mapper=/dev/mapper/beelink-t7
for cmd in cryptsetup mkfs.ext4 lsblk findmnt wipefs udevadm systemctl flock python3; do
    command -v "$cmd" >/dev/null || fail "Missing command: $cmd"
done
exec 9>/run/lock/beelink-t7-setup.lock
flock -n 9 || fail "Another T7 setup is running."
[[ -b "$disk" && ! -e "$key" && ! -L "$key" && ! -e "$mapper" ]] ||
    fail "Disk missing, or key/mapping already exists. Inspect before retrying."
for config in /etc/crypttab /etc/fstab; do
    [[ -f "$config" && ! -L "$config" ]] || fail "Invalid configuration: $config"
    ! grep -Eq 'beelink-t7|^[^#]*[[:space:]]/daten([[:space:]]|$)' "$config" ||
        fail "Existing T7 or /daten configuration in $config."
done
[[ ! -L /daten ]] || fail "/daten must not be a symlink."
if [[ -e /daten ]]; then
    [[ -d /daten && -z "$(ls -A /daten)" ]] || fail "/daten is not an empty directory."
fi
! findmnt -rn -M /daten >/dev/null || fail "/daten is already mounted."
root_source="$(findmnt -rn -o SOURCE /)"
lsblk -sno TYPE "$root_source" | grep -qx crypt ||
    fail "The system filesystem containing the unlock key must be encrypted."
python3 - "$disk" <<'PY'
import json
import subprocess
import sys

disk = sys.argv[1]
tree = json.loads(subprocess.check_output(
    ["lsblk", "--json", "--tree", "--bytes", "-o",
     "PATH,TYPE,MODEL,SERIAL,SIZE,TRAN,MOUNTPOINTS", disk], text=True))["blockdevices"]
if len(tree) != 1:
    raise SystemExit("ERROR: Ambiguous device.")
device = tree[0]
if (device["type"], device["model"].strip(), device["serial"], device["size"], device["tran"]) != (
        "disk", "PSSD T7", "H758101R0JN7T5S", 2000398934016, "usb"):
    raise SystemExit("ERROR: Device identity or capacity changed; refusing to erase.")
def check(node):
    if any(node.get("mountpoints") or []):
        raise SystemExit("ERROR: Target disk or a child device is mounted.")
    if node["type"] not in ("disk", "part"):
        raise SystemExit("ERROR: Target has an active mapping.")
    for child in node.get("children", []):
        check(child)
check(device)
PY
state="$(mktemp -d /root/beelink-t7-setup.XXXXXXXX)"
log="$state/setup.log"
exec > >(tee -a "$log") 2>&1
trap 'status=$?; if [[ $status != 0 ]]; then echo "ERROR: Setup incomplete. Preserve the key and inspect $state; do not rerun or format blindly."; fi' EXIT
echo "$(date -Is) Preparing Samsung T7; all previous contents will be discarded."
cp -p /etc/fstab /etc/crypttab "$state/"
install -d -o root -g root -m 0700 /etc/cryptsetup-keys.d
dd if=/dev/urandom of="$key" bs=64 count=1 status=none conv=excl
chmod 0400 "$key"
wipefs --all "$disk"
udevadm settle
cryptsetup luksFormat --type luks2 --batch-mode --key-file "$key" "$disk"
uuid="$(cryptsetup luksUUID "$disk")"
cryptsetup luksHeaderBackup "$disk" --header-backup-file "$state/luks-header.img"
cryptsetup open --type luks --key-file "$key" "$disk" beelink-t7
mkfs.ext4 -L beelink-t7 -m 0 "$mapper"
install -d -o root -g root -m 0755 /daten
printf 'beelink-t7 UUID=%s %s luks,noauto,nofail,headless,timeout=5s,x-systemd.device-timeout=5s\n' \
    "$uuid" "$key" >> /etc/crypttab
printf '%s\n' '/dev/mapper/beelink-t7 /daten ext4 nofail,x-systemd.automount,x-systemd.device-timeout=10s,x-systemd.mount-timeout=15s 0 2' >> /etc/fstab
findmnt --verify --tab-file /etc/fstab
cryptsetup close beelink-t7
systemctl daemon-reload
systemctl start daten.automount
systemctl start daten.mount
[[ "$(findmnt -rn -M /daten -t ext4 -o SOURCE)" == "$mapper" ]] || fail "Unexpected mounted source."
chown hannes:hannes /daten
testfile="$(mktemp /daten/.t7-write-test.XXXXXXXX)"
printf 'beelink-t7 write test\n' > "$testfile"
sync -f "$testfile"
grep -qx 'beelink-t7 write test' "$testfile"
rm -- "$testfile"
cryptsetup status beelink-t7
findmnt -M /daten
echo "$(date -Is) T7 ready at /daten. Unlock key: $key; recovery header and logs: $state."
echo "Back up the key and header securely off this host. No recovery passphrase exists."
echo "A reboot and a boot with the T7 absent still need to be verified."
