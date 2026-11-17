#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ $# == 1 && "$1" == --prepare-isolated ]] ||
    fail "Usage: sudo bash monitoring/prepare-beelink-runtime.sh --prepare-isolated"
[[ "$(id -u)" == 0 && "$(hostname -s | tr '[:upper:]' '[:lower:]')" == beelink ]] ||
    fail "Run as root directly on beelink."
seed=/daten/latitude-migration
runtime=/daten/beelink-runtime
expected_uuid=c0c42461-ae4c-4edb-a993-bba94a05ed6c
for command in docker findmnt flock rsync tar python3; do
    command -v "$command" >/dev/null || fail "Missing command: $command"
done
ls /daten >/dev/null
[[ "$(findmnt -rn -M /daten -t ext4 -o UUID)" == "$expected_uuid" ]] ||
    fail "The expected encrypted T7 is not mounted."
exec 9>/run/lock/beelink-runtime-prepare.lock
flock -n 9 || fail "Another runtime preparation is running."
[[ ! -L "$seed" && -f "$seed/seed-complete" &&
    "$(cat "$seed/.latitude-seed")" == latitude-to-beelink-v1 ]] ||
    fail "Expected a completed Latitude seed."
[[ -f /daten/beelink-staging/database-report.json ]] ||
    fail "Restore and verify the isolated databases first."
[[ -s "$seed/metadata/native-runtimes.tar.gz" ]] ||
    fail "Transfer the source Java runtimes and native startup configuration first."
tar -tzf "$seed/metadata/native-runtimes.tar.gz" >/dev/null
[[ ! -L "$runtime" ]] || fail "Runtime root must not be a symlink."
if [[ -e "$runtime" ]]; then
    [[ -d "$runtime" && "$(stat -c '%u:%a' "$runtime")" == 0:700 &&
        -f "$runtime/.beelink-runtime" &&
        "$(cat "$runtime/.beelink-runtime")" == beelink-runtime-v1 ]] ||
        fail "Refusing an unmanaged runtime directory."
else
    mkdir -m 0700 "$runtime"
    printf '%s\n' beelink-runtime-v1 > "$runtime/.beelink-runtime"
fi
exec > >(tee -a "$runtime/prepare.log") 2>&1
log() { printf '%s %s\n' "$(date -Is)" "$*"; }
trap 'status=$?; if [[ $status != 0 ]]; then echo "ERROR: Preparation incomplete; inspect /daten/beelink-runtime/prepare.log. No host services or schedules were installed."; fi' EXIT

# Fail closed if a subsequent application-start step has already used these files.
python3 - "$runtime" <<'PY'
import json
import subprocess
import sys

root = sys.argv[1]
ids = subprocess.check_output(["docker", "ps", "-q"], text=True).split()
if ids:
    for container in json.loads(subprocess.check_output(["docker", "inspect", *ids])):
        for mount in container["Mounts"]:
            source = mount.get("Source", "")
            if source == root or source.startswith(root + "/"):
                raise SystemExit("ERROR: A running container already uses the runtime tree: " + container["Name"])
PY
if [[ -f "$runtime/files-prepared" ]]; then
    log "Runtime files are already prepared; no file changes made."
    exit 0
fi
if [[ ! -e "$runtime/rootfs" ]]; then
    [[ -d "$seed/files" && ! -L "$seed/files" ]] ||
        fail "The live-file seed is missing."
    [[ "$(stat -c %d "$seed/files")" == "$(stat -c %d "$runtime")" ]] ||
        fail "Source and runtime must be on the same filesystem for an atomic move."
    log "Promoting the live-file seed on the same filesystem; no second bulk copy."
    mv -T "$seed/files" "$runtime/rootfs"
else
    [[ -d "$runtime/rootfs" && ! -L "$runtime/rootfs" && ! -e "$seed/files" ]] ||
        fail "Ambiguous live-file location; inspect before proceeding."
fi
[[ -d "$runtime/rootfs/daten/owncloud/data" &&
    -d "$runtime/rootfs/opt/glassfish-4.0" &&
    -d "$runtime/rootfs/srv/www.docker/www" ]] ||
    fail "Expected application files are missing."
printf '%s\n' "$runtime/rootfs" > "$seed/files-promoted"

log "Restoring numeric ownership, modes, ACLs and xattrs from fake-super metadata in place."
# The sender decodes fake-super; the local receiver applies real metadata.
# Both paths refer to the same files, so file contents are not retransferred.
rsync -aHAX --fake-super -M--super --info=stats1 \
    "$runtime/rootfs/" "$runtime/rootfs/"
python3 - "$runtime/rootfs" <<'PY'
import os
import sys

def check(path):
    if not os.path.islink(path):
        if any(name.startswith("user.rsync.%") for name in os.listxattr(path)):
            raise SystemExit("ERROR: Undecoded fake-super metadata remains: " + path)

for root, directories, files in os.walk(sys.argv[1]):
    check(root)
    for name in directories + files:
        check(os.path.join(root, name))
PY

log "Unpacking host configuration privately; nothing is installed into /etc or /opt."
mkdir -p "$runtime/host-configuration"
tar --acls --xattrs --numeric-owner \
    -xzf "$seed/metadata/host-configuration.tar.gz" -C "$runtime/host-configuration"
tar --numeric-owner -xzf "$seed/metadata/native-runtimes.tar.gz" -C "$runtime/rootfs" \
    usr/lib/jvm/java-8-openjdk-amd64 usr/lib/jvm/java-11-openjdk-amd64
tar --numeric-owner -xzf "$seed/metadata/native-runtimes.tar.gz" -C "$runtime/host-configuration" \
    etc/java-8-openjdk etc/java-11-openjdk etc/ssl/certs/java etc/wildfly etc/init.d/glassfish
printf '%s\n' 'Files prepared only; isolated application startup and final reconciliation are still required.' \
    > "$runtime/files-prepared"
log "Runtime files prepared at $runtime. Production, Home Assistant, existing cameras and schedules unchanged."
