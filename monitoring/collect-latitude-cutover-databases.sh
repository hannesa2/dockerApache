#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ $# == 1 && "$1" == --collect-frozen ]] ||
    fail "Usage: sudo bash monitoring/collect-latitude-cutover-databases.sh --collect-frozen"
[[ "$(id -u)" == 0 && "$(hostname -s | tr '[:upper:]' '[:lower:]')" == latitude ]] ||
    fail "Run as root directly on Latitude."
mountpoint -q /daten || fail "Source /daten must be mounted."
for command in docker mysql mysqldump pg_restore gzip flock rsync sha256sum; do
    command -v "$command" >/dev/null || fail "Missing command: $command"
done
exec 9>/run/lock/latitude-beelink-cutover-databases.lock
flock -n 9 || fail "Another database collection is running."

check_frozen() {
    local service container status result
    for service in apache2 php8.3-fpm php8.4-fpm glassfish wildfly; do
        [[ "$(systemctl show "$service" -p ActiveState --value)" == inactive ]] ||
            fail "Source service must be inactive: $service"
    done
    for container in apache-apache-1 owncloud-staging-owncloud-1 gitlab-runner; do
        [[ "$(docker inspect -f '{{.State.Running}}' "$container")" == false ]] ||
            fail "Source app must be stopped: $container"
    done
    for service in puma sidekiq sshd; do
        result=0
        status="$(docker exec gitlab gitlab-ctl status "$service")" || result=$?
        [[ "$result" == 3 && "$status" == "down: $service:"* ]] ||
            fail "Source GitLab writer is not stopped: $service"
    done
}
check_frozen

mysql_options=(--user=root)
native_password=
if [[ -f /etc/mysql/debian.cnf && ! -L /etc/mysql/debian.cnf ]] &&
    mysql --defaults-file=/etc/mysql/debian.cnf -NBe 'SELECT 1' >/dev/null 2>&1; then
    mysql_options=(--defaults-file=/etc/mysql/debian.cnf)
elif ! mysql --user=root -NBe 'SELECT 1' >/dev/null 2>&1; then
    [[ -t 0 ]] || fail "Native MySQL needs credentials; run interactively."
    IFS= read -r -s -p "Native MySQL root password: " native_password
    printf '\n'
    MYSQL_PWD="$native_password" mysql --user=root -NBe 'SELECT 1' >/dev/null ||
        fail "Native MySQL authentication failed."
fi
# Enabled scheduled database events would bypass stopped web applications.
event_query="SELECT COUNT(*) FROM information_schema.EVENTS WHERE STATUS='ENABLED' AND @@GLOBAL.event_scheduler='ON'"
[[ "$(MYSQL_PWD="$native_password" mysql "${mysql_options[@]}" -NBe "$event_query")" == 0 ]] ||
    fail "Native MySQL has active scheduled events; inspect before collecting."
[[ "$(docker exec apache-db-1 sh -c \
    'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysql -uroot -NB -e "$1"' sh "$event_query")" == 0 ]] ||
    fail "Docker MySQL has active scheduled events; inspect before collecting."

ssh_options=(-i /home/hannes/.ssh/id_rsa -o IdentitiesOnly=yes -o BatchMode=yes
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/home/hannes/.ssh/known_hosts
    -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=6)
target=/daten/latitude-migration/cutover-20261117
ssh -n "${ssh_options[@]}" hannes@beelink "set -eu
test \"\$(hostname -s)\" = beelink
test \"\$(findmnt -rn -M /daten -t ext4 -o UUID)\" = c0c42461-ae4c-4edb-a993-bba94a05ed6c
test \"\$(cat /daten/latitude-migration/.latitude-seed)\" = latitude-to-beelink-v1
test ! -L '$target'
test \"\$(stat -c '%u:%a' '$target')\" = \"\$(id -u):700\"
test -s '$target/SHA256SUMS'
"
work="$(mktemp -d /daten/beelink-cutover-databases.XXXXXXXX)"
LOG="$work/collection.log"
exec > >(tee -a "$LOG") 2>&1
log() { printf '%s %s\n' "$(date -Is)" "$*"; }
trap 'status=$?; if [[ $status != 0 ]]; then printf "ERROR: Collection incomplete; retained at %s. Source stays frozen; nothing restored.\n" "$work" >&2; fi' EXIT
log "Collecting application databases with source web writers stopped. No GitLab backup."
mkdir "$work/databases"
docker exec --user postgres mxserver-postgres-db-1 pg_dump -Fc -U postgres -d MXServer \
    > "$work/databases/mxserver.dump"
docker exec owncloud-staging-db-1 pg_dump -Fc -U owncloud_stage -d owncloud_stage \
    > "$work/databases/owncloud.dump"
for name in mxserver owncloud; do
    pg_restore -f /dev/null "$work/databases/$name.dump"
done
docker exec apache-db-1 sh -c \
    'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysqldump -uroot --all-databases --single-transaction --routines --events --triggers --hex-blob --no-tablespaces' |
    gzip > "$work/databases/apache-mysql.sql.gz"
MYSQL_PWD="$native_password" mysqldump "${mysql_options[@]}" --all-databases \
    --single-transaction --routines --events --triggers --hex-blob --no-tablespaces |
    gzip > "$work/databases/native-mysql.sql.gz"
unset native_password
gzip -t "$work/databases/apache-mysql.sql.gz" "$work/databases/native-mysql.sql.gz"
check_frozen
(
    cd "$work/databases"
    sha256sum mxserver.dump owncloud.dump apache-mysql.sql.gz native-mysql.sql.gz > SHA256SUMS
)
log "Transferring verified snapshots privately; no runtime files or destination databases are changed."
remote="$target/$(basename "$work")"
rsync -a --partial -e "ssh ${ssh_options[*]}" "$work/" "hannes@beelink:$remote/"
ssh -n "${ssh_options[@]}" hannes@beelink "set -eu
test \"\$(findmnt -rn -M /daten -t ext4 -o UUID)\" = c0c42461-ae4c-4edb-a993-bba94a05ed6c
cd '$remote/databases'
sha256sum -c SHA256SUMS
umask 077
printf '%s\n' 'Application database snapshots only; files and live camera databases are not reconciled.' > '$remote/collection-complete'
"
log "Snapshots transferred and checksum-verified: beelink:$remote"
log "No restore, file sync, GitLab backup, router, schedule or certificate-renewal changes."
