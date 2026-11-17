#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ $# == 1 && "$1" == --sync-frozen ]] ||
    fail "Usage: sudo bash monitoring/reconcile-latitude-files.sh --sync-frozen"
[[ "$(id -u)" == 0 && "$(hostname -s | tr '[:upper:]' '[:lower:]')" == latitude ]] ||
    fail "Run as root directly on Latitude."
mountpoint -q /daten || fail "Source /daten must be mounted."
exec 9>/run/lock/latitude-beelink-cutover-files.lock
flock -n 9 || fail "Another file reconciliation is running."
ssh_options=(-i /home/hannes/.ssh/id_rsa -o IdentitiesOnly=yes -o BatchMode=yes
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/home/hannes/.ssh/known_hosts
    -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=6)
remote() { ssh -n "${ssh_options[@]}" hannes@beelink "$@"; }
rsync_shell="ssh ${ssh_options[*]}"
worker=beelink-file-reconcile
apps=(beelink-apps-owncloud-app-1 beelink-wordpress-apache-1
    beelink-matomo-matomo-1 beelink-java-web-glassfish-1
    beelink-java-web-wildfly-1 beelink-artifacts-artifacts-1)
for service in apache2 php8.3-fpm php8.4-fpm glassfish wildfly; do
    [[ "$(systemctl show "$service" -p ActiveState --value)" == inactive ]] ||
        fail "Source writer must be inactive: $service"
done
for container in apache-apache-1 owncloud-staging-owncloud-1 gitlab-runner; do
    [[ "$(docker inspect -f '{{.State.Running}}' "$container")" == false ]] ||
        fail "Source app must remain stopped: $container"
done
for path in /daten/owncloud/data /daten/owncloud/apps /srv/postgres/data/mxinfo \
    /srv/www/vhosts/dev.mxtracks /srv/www/vhosts/mxdocs /daten/githubArtifactsPy; do
    [[ -d "$path" && ! -L "$path" ]] || fail "Missing or symlinked source directory: $path"
done
remote "python3 - <<'PY'
import json, subprocess
def out(*args):
    return subprocess.check_output(args, text=True).strip()
assert out('findmnt','-rn','-M','/daten','-t','ext4','-o','UUID') == 'c0c42461-ae4c-4edb-a993-bba94a05ed6c'
assert out('systemctl','is-active','beelink-production.service') == 'active'
expected = {
 'beelink-apps-owncloud-app-1': {
  '/mnt/data/files':'/daten/beelink-runtime/rootfs/daten/owncloud/data',
  '/mnt/data':'/daten/beelink-runtime/apps/owncloud/volume'},
 'beelink-wordpress-apache-1': {'/var/www/html':'/daten/beelink-runtime/wordpress/www'},
 'beelink-matomo-matomo-1': {'/var/www/html':'/daten/beelink-runtime/matomo/www'},
 'beelink-java-web-glassfish-1': {
  '/srv/postgres/data/mxinfo':'/daten/beelink-runtime/rootfs/srv/postgres/data/mxinfo'},
 'beelink-artifacts-artifacts-1': {'/app/files':'/daten/beelink-runtime/artifacts/files'}}
for name, mounts in expected.items():
    c=json.loads(out('docker','inspect',name))[0]
    assert not c['HostConfig'].get('PortBindings'), name
    assert c['HostConfig']['RestartPolicy']['Name'] == 'no', name
    assert set(c['NetworkSettings']['Networks']) == {'beelink-databases_isolated'}, name
    actual={m['Destination']:m['Source'] for m in c['Mounts']}
    assert all(actual.get(k)==v for k,v in mounts.items()), name
assert out('docker','exec','beelink-wordpress-apache-1','wp','--allow-root',
    '--path=/var/www/html/mxdocs/wordpress','--skip-plugins','--skip-themes',
    'core','version') == '6.9.11'
assert not out('docker','ps','-aq','--filter','name=^/beelink-file-reconcile$')
print('Destination T7, isolated mounts and upgraded WordPress core checked.')
PY"
work="$(mktemp -d /daten/beelink-cutover-files.XXXXXXXX)"
LOG="$work/sync.log"
exec > >(tee -a "$LOG") 2>&1
log() { printf '%s %s\n' "$(date -Is)" "$*"; }
active_worker=false
active_lock=false
cleanup() {
    status=$?
    if [[ "$active_worker" == true ]]; then
        remote "docker stop '$worker'" || printf 'ERROR: Stop destination worker %s manually.\n' "$worker" >&2
    fi
    if [[ "$active_lock" == true ]]; then
        remote "docker stop beelink-file-reconcile-lock" ||
            printf 'ERROR: Stop destination beelink-file-reconcile-lock manually.\n' >&2
    fi
    if [[ "$status" != 0 ]]; then
        printf 'ERROR: File sync incomplete. Affected destination apps remain stopped; inspect %s.\n' "$LOG" >&2
    fi
}
trap cleanup EXIT
remote "docker run -d --rm --name beelink-file-reconcile-lock --network none \
    --restart no --read-only \
    --mount type=bind,source=/run/lock/beelink-staging-control.lock,target=/lock \
    --entrypoint /bin/sh beelink-source-apache:20261116 \
    -c 'exec flock -n /lock sh -c \"echo locked; exec sleep infinity\"' >/dev/null"
active_lock=true
remote "sleep 1; test \"\$(docker logs beelink-file-reconcile-lock)\" = locked"
log "Stopping the remaining source artifact writer and affected isolated destination apps."
sudo systemctl stop githubArtifactsPy
remote "python3 - <<'PY'
import json, os, subprocess
from pathlib import Path
os.umask(0o077)
root=Path('/daten/latitude-migration/cutover-20261117')
assert not (root/'files-sync-complete.json').exists(), 'Files already synchronized; inspect before repeating.'
protected={}
for name in ('homeassistant','camera-postgres-db-1','beelink-apps-gitlab-1'):
    c=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
    assert c['State']['Running'], name
    protected[name]={'id':c['Id'],'started':c['State']['StartedAt'],'running':True}
with (root/'files-sync-pending.json').open('x') as stream:
    json.dump({'protected':protected},stream)
PY"
remote "docker stop beelink-frontend-frontend-1 ${apps[*]}"

sync_tree() {
    local source="$1" destination="$2"
    shift 2
    log "Reconciling $source"
    remote "set -eu
test \"\$(findmnt -rn -M /daten -t ext4 -o UUID)\" = c0c42461-ae4c-4edb-a993-bba94a05ed6c
docker run -d --rm --name '$worker' --network none --restart no --read-only \
  --mount 'type=bind,source=$destination,target=/target' \
  --entrypoint /bin/sleep beelink-source-apache:20261116 infinity >/dev/null"
    active_worker=true
    # A disposable receiver has only this data tree mounted, no network/ports.
    # Default rsync temp-file replacement is required; never use --inplace.
    rsync -aHAX --numeric-ids --delete-delay --stats "$@" -e "$rsync_shell" \
        --rsync-path="docker exec -i --user 0 $worker rsync" \
        "$source/" hannes@beelink:/target/
    rsync -aHAX --numeric-ids --delete-delay --dry-run --itemize-changes "$@" \
        -e "$rsync_shell" --rsync-path="docker exec -i --user 0 $worker rsync" \
        "$source/" hannes@beelink:/target/ > "$work/remaining-changes"
    [[ ! -s "$work/remaining-changes" ]] ||
        fail "Source changed or differences remain; inspect $work/remaining-changes"
    remote "docker stop '$worker' >/dev/null"
    active_worker=false
}
runtime=/daten/beelink-runtime
sync_tree /daten/owncloud/data "$runtime/rootfs/daten/owncloud/data"
sync_tree /daten/owncloud/apps "$runtime/apps/owncloud/volume/apps"
sync_tree /srv/postgres/data/mxinfo "$runtime/rootfs/srv/postgres/data/mxinfo"
sync_tree /srv/www/vhosts/dev.mxtracks "$runtime/wordpress/www/dev.mxtracks" \
    --exclude=.git --exclude=/piwik/ --exclude=/wordpress/wp-config.php
sync_tree /srv/www/vhosts/mxdocs "$runtime/wordpress/www/mxdocs" \
    --exclude=.git --exclude=/piwik/ --exclude=/wordpress/
sync_tree /srv/www/vhosts/mxdocs/wordpress/wp-content \
    "$runtime/wordpress/www/mxdocs/wordpress/wp-content" --exclude=.git
sync_tree /srv/www/vhosts/mxdocs/piwik "$runtime/matomo/www/piwik" \
    --exclude=.git --exclude=/config/config.ini.php --exclude=/tmp/
sync_tree /daten/githubArtifactsPy "$runtime/artifacts/files" \
    --chown=10001:10001 --chmod=D750,F640
remote "mv /daten/latitude-migration/cutover-20261117/files-sync-pending.json \
    /daten/latitude-migration/cutover-20261117/files-sync-complete.json"
log "All selected file trees reconciled; second rsync passes show no size/mtime/metadata differences."
log "Destination apps remain stopped for root-owned permission repair and application/TLS checks."
log "Source stays frozen. GitLab, databases, camera services, router and certificate renewal unchanged."
