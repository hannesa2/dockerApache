#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ $# == 1 && ( "$1" == --prepare-frozen || "$1" == --prepare-mirrors ) ]] ||
    fail "Usage: sudo bash monitoring/prepare-latitude-certificate-cutover.sh --prepare-frozen|--prepare-mirrors"
[[ "$(id -u)" == 0 && "$(hostname -s | tr '[:upper:]' '[:lower:]')" == latitude ]] ||
    fail "Run as root directly on Latitude."
mountpoint -q /daten || fail "Source /daten must be mounted."
exec 9>/run/lock/latitude-beelink-certificate-cutover.lock
flock -n 9 || fail "Another certificate preparation is running."
[[ "$(systemctl show apache2.service -p ActiveState --value)" == inactive ]] ||
    fail "Source Apache must already be stopped."
systemctl is-active --quiet snap.certbot.renew.timer ||
    fail "Latitude certificate renewal timer must remain active."
[[ "$(systemctl show snap.certbot.renew.service -p ActiveState --value)" == inactive ]] ||
    fail "Certificate renewal is currently running; retry after it finishes."
[[ "$(docker inspect -f '{{.State.Running}}' apache-apache-1)" == false ]] ||
    fail "Source Docker Apache must remain stopped."

lineages=(www.mxtracks.info dev.mxtracks.info-0002 git.mxtracks.info-0001
    homeassistant.mxtracks.info nextcloud.mxtracks.info-0002)
paths=(etc/letsencrypt/accounts)
if [[ "$1" == --prepare-mirrors ]]; then
    lineages=(dev16.mxtracks.info www16.mxtracks.info git16.mxtracks.info)
    paths=()
fi
for name in "${lineages[@]}"; do
    renewal="/etc/letsencrypt/renewal/$name.conf"
    [[ -f "$renewal" && ! -L "$renewal" ]] || fail "Missing renewal configuration: $name"
    grep -Eq '^authenticator *= *standalone *$' "$renewal" ||
        fail "Unexpected challenge method: $name"
    for file in fullchain.pem privkey.pem; do
        [[ -s "/etc/letsencrypt/live/$name/$file" ]] ||
            fail "Missing certificate file: $name/$file"
    done
    openssl x509 -in "/etc/letsencrypt/live/$name/fullchain.pem" \
        -checkend 604800 -noout || fail "Certificate expires within seven days: $name"
    paths+=("etc/letsencrypt/renewal/$name.conf"
        "etc/letsencrypt/live/$name" "etc/letsencrypt/archive/$name")
done
ssh_options=(-i /home/hannes/.ssh/id_rsa -o IdentitiesOnly=yes -o BatchMode=yes
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/home/hannes/.ssh/known_hosts
    -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=6)
target=/daten/latitude-migration/cutover-20261117
ssh -n "${ssh_options[@]}" hannes@beelink "set -eu
test \"\$(hostname -s)\" = beelink
test \"\$(findmnt -rn -M /daten -t ext4 -o UUID)\" = c0c42461-ae4c-4edb-a993-bba94a05ed6c
test ! -L '$target'
test \"\$(stat -c '%u:%a' '$target')\" = \"\$(id -u):700\"
test -f '$target/files-sync-complete.json'
"
work="$(mktemp -d /daten/beelink-cutover-certificates.XXXXXXXX)"
LOG="$work/preparation.log"
exec > >(tee -a "$LOG") 2>&1
log() { printf '%s %s\n' "$(date -Is)" "$*"; }
trap 'status=$?; if [[ $status != 0 ]]; then printf "ERROR: Preparation incomplete; inspect %s. Do not start source Apache; any runtime mask remains in place.\n" "$LOG" >&2; fi' EXIT

# The existing Certbot post-hook unconditionally starts this source writer.
log "Blocking automatic source Apache starts while preserving the Certbot timer."
systemctl mask --runtime apache2.service
[[ "$(systemctl show apache2.service -p LoadState --value)" == masked ]] ||
    fail "Apache runtime mask was not applied."
[[ "$(systemctl show apache2.service -p ActiveState --value)" == inactive ]] ||
    fail "Source Apache unexpectedly started."
log "Collecting selected certificate lineages privately ($1)."
tar -C / -czf "$work/letsencrypt.tar.gz" "${paths[@]}"
tar -tzf "$work/letsencrypt.tar.gz" >/dev/null
for name in "${lineages[@]}"; do
    printf '== %s ==\n' "$name" >> "$work/certificate-inventory.txt"
    openssl x509 -in "/etc/letsencrypt/live/$name/fullchain.pem" \
        -noout -subject -issuer -dates -fingerprint -sha256 >> "$work/certificate-inventory.txt"
done
(cd "$work" && sha256sum letsencrypt.tar.gz certificate-inventory.txt > SHA256SUMS)
remote="$target/$(basename "$work")"
rsync -a -e "ssh ${ssh_options[*]}" "$work/" "hannes@beelink:$remote/"
ssh -n "${ssh_options[@]}" hannes@beelink "set -eu
test \"\$(findmnt -rn -M /daten -t ext4 -o UUID)\" = c0c42461-ae4c-4edb-a993-bba94a05ed6c
cd '$remote'
sha256sum -c SHA256SUMS
umask 077
printf '%s\n' 'Private certificate transfer only; renewal ownership and public ports remain unchanged.' > transfer-complete
"
systemctl is-active --quiet snap.certbot.renew.timer ||
    fail "Latitude certificate renewal timer is no longer active."
log "Private certificates transferred and verified: beelink:$remote"
log "Latitude Certbot timer remains active. Router, destination apps, DNS and renewal ownership unchanged."
log "Source Apache is runtime-masked until reboot or explicit systemctl unmask --runtime apache2.service."
