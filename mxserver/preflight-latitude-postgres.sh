#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

fail() { echo "ERROR: $*" >&2; exit 1; }

[[ $# -eq 0 ]] || fail "Usage: $0"
[[ "$(id -u)" -eq 0 ]] || fail "Run as root: sudo bash mxserver/preflight-latitude-postgres.sh"
[[ "$(hostname -s | tr '[:upper:]' '[:lower:]')" == latitude ]] ||
    fail "This preflight is restricted to Latitude."

for cmd in docker runuser psql pg_dump pg_dumpall pg_restore systemctl mountpoint \
    openssl sha256sum df flock curl; do
    command -v "$cmd" >/dev/null || fail "Required command is unavailable: $cmd"
done
docker info >/dev/null || fail "Docker daemon is unavailable."
docker image inspect camera-postgres-db >/dev/null ||
    fail "The PostgreSQL 16 rehearsal image camera-postgres-db is unavailable."
mountpoint -q /daten || fail "/daten is not mounted."
systemctl is-active --quiet postgresql@16-main ||
    fail "Native PostgreSQL 16/main is not running."
systemctl is-active --quiet glassfish || fail "GlassFish is not running."
[[ ! -e /run/lock/mxserver-mirror-sync.lock ]] ||
    fail "The MXServer mirror sync lock exists. Wait for the sync or inspect the lock."

backup_lock=/daten/sicherung.postgresql/dump/.postgresql-backup.lock
[[ -f "$backup_lock" && ! -L "$backup_lock" ]] ||
    fail "Expected existing PostgreSQL backup lock file: $backup_lock"
exec 9>>"$backup_lock"
flock -n 9 || fail "The PostgreSQL backup job is running."

native_psql=(runuser -u postgres -- env -u PGHOST -u PGPORT -u PGUSER -u PGDATABASE \
    psql -X -w -v ON_ERROR_STOP=1 -h /var/run/postgresql -p 5432 -U postgres -At -d postgres)
setting() { "${native_psql[@]}" -c "SELECT current_setting('$1')"; }
[[ "$(setting server_version_num)" -ge 160000 && "$(setting server_version_num)" -lt 170000 ]] ||
    fail "Expected a native PostgreSQL 16 server."
[[ "$(setting port)" == 5432 ]] || fail "Native PostgreSQL is not on port 5432."
[[ "$(setting ssl)" == on ]] || fail "Native PostgreSQL TLS is not enabled."
[[ "$("${native_psql[@]}" -c "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL")" == 0 ]] ||
    fail "Native pg_hba.conf contains invalid rules."
[[ "$("${native_psql[@]}" -c "SELECT count(*) FROM pg_database WHERE datname='MXServer' AND datallowconn")" == 1 ]] ||
    fail "MXServer is missing or disallows connections."

cert="$(setting ssl_cert_file)"
key="$(setting ssl_key_file)"
[[ "$cert" == /* && "$key" == /* ]] ||
    fail "TLS paths are not absolute; review before migration."
[[ -r "$cert" && -r "$key" ]] || fail "TLS certificate or private key is not readable."
cert_pub="$(openssl x509 -in "$cert" -pubkey -noout | openssl pkey -pubin -outform DER | sha256sum)"
key_pub="$(openssl pkey -in "$key" -pubout -outform DER | sha256sum)"
[[ "${cert_pub%% *}" == "${key_pub%% *}" ]] || fail "TLS certificate and key do not match."
openssl x509 -in "$cert" -checkend 0 -noout >/dev/null ||
    fail "Native PostgreSQL TLS certificate is expired or invalid."

[[ -f /home/hannes/.pgpass && ! -L /home/hannes/.pgpass ]] ||
    fail "/home/hannes/.pgpass is missing or a symlink."
[[ "$(stat -c '%a:%U' /home/hannes/.pgpass)" == 600:hannes ]] ||
    fail "/home/hannes/.pgpass must be owned by hannes and mode 0600."
runuser -u hannes -- env -u PGPASSWORD -u PGPASSFILE PGSSLMODE=require psql -w -h localhost -p 5432 \
    -U hannes -d MXServer -X -q -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null ||
    fail "Existing hannes credentials or TLS login failed."

echo "Native PostgreSQL and GlassFish are running; TLS certificate and key match."
echo "Current non-template databases:"
"${native_psql[@]}" -F ' | ' -c \
    "SELECT datname,pg_get_userbyid(datdba),pg_size_pretty(pg_database_size(oid)) FROM pg_database WHERE NOT datistemplate ORDER BY datname"
echo "Current MXServer connections:"
"${native_psql[@]}" -F ' | ' -c \
    "SELECT usename,coalesce(client_addr::text,'local'),coalesce(application_name,''),state,count(*) FROM pg_stat_activity WHERE datname='MXServer' GROUP BY 1,2,3,4 ORDER BY 1,2,3,4"
echo "PostgreSQL certificate and configuration paths (no secret values):"
printf 'certificate: %s\nprivate key: %s\nhba: %s\ndata: %s\n' \
    "$cert" "$key" "$(setting hba_file)" "$(setting data_directory)"
echo "Filesystem availability:"
df -h /daten /home/hannes/server
echo "Preflight completed; no database, container, service or cron configuration was changed."
echo "This is not a migration or a production cutover script."
