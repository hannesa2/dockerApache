#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

fail() { echo "ERROR: $*" >&2; exit 1; }
[[ $# -eq 1 && "$1" == --execute ]] || fail "Usage: sudo bash mxserver/cutover-latitude-postgres.sh --execute"
[[ "$(id -u)" -eq 0 ]] || fail "Run as root."
[[ "$(hostname -s | tr '[:upper:]' '[:lower:]')" == latitude ]] || fail "Latitude only."
while IFS='=' read -r name _; do
    case "$name" in
        DOCKER_*|COMPOSE_*|PG*) fail "Unset inherited $name before running the cutover." ;;
    esac
done < <(env)

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$root/mxserver/cutover-locks.sh"
stack="$root/mxserver-postgres"
envfile="$stack/.env"
hba=/etc/postgresql/16/main/pg_hba.conf
startconf=/etc/postgresql/16/main/start.conf
marker=/etc/mxserver-postgres-dockerized
data_root=/home/hannes/server/mxserver-postgres
mirror=/usr/local/sbin/mxserver-mirror-sync
boot_wait=/usr/local/sbin/mxserver-postgres-wait
boot_dropin=/etc/systemd/system/glassfish.service.d/mxserver-postgres.conf
backup_lock=/daten/sicherung.postgresql/dump/.postgresql-backup.lock
mirror_lock=/run/lock/mxserver-mirror-sync.lock
stage_port=55432
native=(runuser -u postgres -- env -u PGHOST -u PGPORT -u PGUSER -u PGDATABASE
    psql -X -w -v ON_ERROR_STOP=1 -h /var/run/postgresql -p 5432 -U postgres -At -d postgres)
setting() { "${native[@]}" -c "SELECT current_setting('$1')"; }
compose=(docker compose --env-file "$envfile" -f "$stack/docker-compose.yml")

for cmd in docker runuser psql pg_dump pg_dumpall pg_restore systemctl pg_ctlcluster \
    python3 openssl sha256sum mountpoint ss flock curl install git; do
    command -v "$cmd" >/dev/null || fail "Missing command: $cmd"
done
mountpoint -q /daten || fail "/daten is not mounted."
glassfish_group="$(systemctl show glassfish -p ControlGroup --value)"
[[ "$glassfish_group" == /system.slice/glassfish.service ]] ||
    fail "Unexpected GlassFish control group."
glassfish_processes="/sys/fs/cgroup${glassfish_group}/cgroup.procs"
[[ -r "$glassfish_processes" ]] ||
    fail "GlassFish process group is unavailable."
[[ -n "$(cat "$glassfish_processes")" ]] || fail "GlassFish has no running processes."
curl -fsS --max-time 10 http://127.0.0.1:8081/ >/dev/null ||
    fail "GlassFish must respond before migration, including recovery from a failed service state."
systemctl is-active --quiet postgresql@16-main || fail "Native PostgreSQL is not running."
[[ -f "$hba" && ! -L "$hba" && -f "$startconf" && ! -L "$startconf" ]] ||
    fail "Native PostgreSQL configuration is missing or symlinked."
[[ ! -e "$data_root" && ! -L "$data_root" && ! -e "$envfile" && ! -L "$envfile" &&
   ! -e "$marker" && ! -L "$marker" ]] ||
    fail "A target directory, .env or cutover marker already exists; inspect before retrying."
[[ ! -e "$boot_wait" && ! -L "$boot_wait" && ! -e "$boot_dropin" && ! -L "$boot_dropin" ]] ||
    fail "A GlassFish PostgreSQL startup gate already exists; inspect before replacing it."
! docker inspect mxserver-postgres-db-1 >/dev/null 2>&1 ||
    fail "Target container already exists."
[[ -z "$(ss -H -ltn "sport = :$stage_port")" ]] || fail "Staging port $stage_port is in use."
[[ "$(setting server_version_num)" -ge 160000 && "$(setting server_version_num)" -lt 170000 ]] ||
    fail "Expected PostgreSQL 16."
[[ "$(setting port)" == 5432 && "$(setting ssl)" == on && "$(setting hba_file)" == "$hba" ]] ||
    fail "Unexpected native port, HBA path or TLS setting."
[[ "$("${native[@]}" -c "SELECT count(*) FROM pg_database WHERE NOT datistemplate AND datname NOT IN ('postgres','MXServer')")" == 0 ]] ||
    fail "Additional native databases exist; do not retire this cluster."
[[ "$("${native[@]}" -c "SELECT count(*) FROM pg_database WHERE datname='MXServer' AND datallowconn")" == 1 ]] ||
    fail "MXServer is missing or cannot connect."
[[ "$("${native[@]}" -c 'SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL')" == 0 ]] ||
    fail "Native HBA contains errors."
cert="$(setting ssl_cert_file)"
key="$(setting ssl_key_file)"
[[ "$cert" == /* && "$key" == /* && -r "$cert" && -r "$key" ]] ||
    fail "Native TLS certificate or key is unavailable."
cert_pub="$(openssl x509 -in "$cert" -pubkey -noout | openssl pkey -pubin -outform DER | sha256sum)"
key_pub="$(openssl pkey -in "$key" -pubout -outform DER | sha256sum)"
[[ "${cert_pub%% *}" == "${key_pub%% *}" ]] || fail "TLS certificate and key do not match."
openssl x509 -in "$cert" -checkend 0 -noout >/dev/null || fail "TLS certificate has expired."
[[ -f /home/hannes/.pgpass && ! -L /home/hannes/.pgpass &&
    "$(stat -c '%a:%U' /home/hannes/.pgpass)" == 600:hannes ]] ||
    fail "Expected private hannes .pgpass."
runuser -u hannes -- env -u PGPASSWORD -u PGPASSFILE PGSSLMODE=require \
    psql -w -h localhost -p 5432 -U hannes -d MXServer -X -q -c 'SELECT 1' >/dev/null ||
    fail "Current MXServer credentials or TLS failed."
[[ -f "$mirror" && ! -L "$mirror" ]] || fail "Installed mirror sync is missing or symlinked."
cmp -s "$mirror" <(git -C "$root" show HEAD:mxserver/sync-latitude.sh) ||
    fail "Installed mirror sync differs from repository baseline; inspect before replacing."
docker image inspect camera-postgres-db >/dev/null || fail "Tested PostgreSQL image is missing."
[[ "$(awk '!/^[[:space:]]*(#|$)/ { print; exit }' "$startconf")" == auto ]] ||
    fail "Unexpected native cluster start.conf."

archive="$(mktemp -d /daten/sicherung.postgresql/mxserver-cutover.XXXXXXXX)"
chmod 0700 "$archive"
glassfish_stopped=0
native_stopped=0
native_config_changed=0
freeze_installed=0
mirror_changed=0
marker_created=0
target_created=0
boot_gate_changed=0
exposed=0
complete=0
freeze_temp=
cleanup() {
    status=$?
    hold_mirror_lock=0
    trap - EXIT INT TERM
    if [[ "$complete" -eq 0 && "$exposed" -eq 0 ]]; then
        rollback_ok=1
        echo "Cutover failed before Docker exposure; restoring native service." >&2
        if [[ -n "$freeze_temp" ]]; then
            rm -f -- "$freeze_temp" || { echo "ERROR: Remove temporary HBA file $freeze_temp manually." >&2; rollback_ok=0; }
        fi
        if [[ "$marker_created" -eq 1 ]]; then
            rm -f -- "$marker" || { echo "ERROR: Remove cutover marker manually." >&2; rollback_ok=0; }
        fi
        if [[ "$target_created" -eq 1 ]]; then
            "${compose[@]}" stop db >/dev/null 2>&1 ||
                { echo "ERROR: Stop staging container manually before starting native PostgreSQL." >&2; rollback_ok=0; }
        fi
        if [[ "$mirror_changed" -eq 1 ]]; then
            install -o root -g root -m 0750 "$archive/mirror-original" "$mirror" ||
                { echo "ERROR: Restore $mirror manually." >&2; rollback_ok=0; }
        fi
        if [[ "$boot_gate_changed" -eq 1 ]]; then
            rm -f -- "$boot_dropin" "$boot_wait" && systemctl daemon-reload ||
                { echo "ERROR: Remove the GlassFish startup gate and reload systemd manually." >&2; rollback_ok=0; }
        fi
        if [[ "$native_config_changed" -eq 1 ]]; then
            cp -p -- "$archive/start.conf" "$startconf" ||
                { echo "ERROR: Restore native start.conf from $archive." >&2; rollback_ok=0; }
        fi
        if [[ "$freeze_installed" -eq 1 ]]; then
            cp -p -- "$archive/pg_hba.conf" "$hba" ||
                { echo "ERROR: Restore native pg_hba.conf from $archive." >&2; rollback_ok=0; }
        fi
        if [[ "$rollback_ok" -eq 1 && "$native_stopped" -eq 1 ]] &&
            ! systemctl is-active --quiet postgresql@16-main; then
            pg_ctlcluster 16 main start ||
                { echo "ERROR: Native PostgreSQL failed to restart; GlassFish stays stopped." >&2; rollback_ok=0; }
        elif [[ "$rollback_ok" -eq 1 && "$freeze_installed" -eq 1 ]]; then
            pg_ctlcluster 16 main reload ||
                { echo "ERROR: Native HBA reload failed; GlassFish stays stopped." >&2; rollback_ok=0; }
        fi
        if [[ "$glassfish_stopped" -eq 1 ]]; then
            if [[ "$rollback_ok" -eq 1 ]] && systemctl is-active --quiet postgresql@16-main &&
                runuser -u hannes -- env -u PGPASSWORD -u PGPASSFILE PGSSLMODE=require \
                    psql -w -h localhost -p 5432 -U hannes -d MXServer -X -q -c 'SELECT 1' >/dev/null; then
                if [[ -r "$glassfish_processes" && -n "$(cat "$glassfish_processes")" ]]; then
                    echo "ERROR: GlassFish processes still run; refusing a duplicate start. Inspect the service." >&2
                    rollback_ok=0
                else
                    systemctl start glassfish || { echo "ERROR: GlassFish restart failed." >&2; rollback_ok=0; }
                fi
            else
                echo "ERROR: Rollback or native database verification failed; GlassFish remains stopped." >&2
                rollback_ok=0
            fi
        fi
        if [[ "$rollback_ok" -eq 0 ]]; then
            hold_mirror_lock=1
            status=1
        fi
    elif [[ "$complete" -eq 0 ]]; then
        echo "ERROR: Docker may have accepted writes. Do NOT restart the stale native database." >&2
        echo "Keep Docker data intact and investigate; recovery files: $archive" >&2
        hold_mirror_lock=1
        status=1
    fi
    if [[ "$hold_mirror_lock" -eq 1 && -n "${CUTOVER_MIRROR_LOCK:-}" ]]; then
        echo "ERROR: Mirror lock retained for investigation: $CUTOVER_MIRROR_LOCK" >&2
        unset CUTOVER_MIRROR_LOCK
    fi
    release_cutover_locks || status=1
    echo "Recovery and migration files: $archive" >&2
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
acquire_cutover_locks "$backup_lock" "$mirror_lock"

cp -p -- "$hba" "$archive/pg_hba.conf"
cp -p -- "$startconf" "$archive/start.conf"
cp -p -- "$mirror" "$archive/mirror-original"
python3 "$root/mxserver/build-freeze-hba.py" "$archive/pg_hba.conf" "$archive/pg_hba.freeze"
echo "Stopping GlassFish and blocking new MXServer connections..."
boot_gate_changed=1
install -d -o root -g root -m 0755 "$(dirname "$boot_dropin")"
printf '[Service]\nKillMode=control-group\n' > "$archive/glassfish-stop.conf"
install -o root -g root -m 0644 "$archive/glassfish-stop.conf" "$boot_dropin"
systemctl daemon-reload
glassfish_stopped=1
systemctl stop glassfish
! systemctl is-active --quiet glassfish || fail "GlassFish did not stop."
if [[ -r "$glassfish_processes" && -n "$(cat "$glassfish_processes")" ]]; then
    echo "Stopping remaining processes in the GlassFish service group..."
    systemctl kill --kill-who=all --signal=SIGTERM glassfish ||
        fail "Could not signal the remaining GlassFish service processes."
    for attempt in $(seq 1 60); do
        [[ ! -e "$glassfish_processes" || -z "$(cat "$glassfish_processes")" ]] && break
        sleep 2
    done
fi
[[ ! -e "$glassfish_processes" || -z "$(cat "$glassfish_processes")" ]] ||
    fail "GlassFish processes remain after stop; refusing to freeze the database."
freeze_installed=1
freeze_temp="$(mktemp "${hba}.cutover.XXXXXXXX")"
install -o postgres -g postgres -m 0640 "$archive/pg_hba.freeze" "$freeze_temp"
mv -f -- "$freeze_temp" "$hba"
freeze_temp=
pg_ctlcluster 16 main reload
[[ "$("${native[@]}" -c 'SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL')" == 0 ]] ||
    fail "Freeze HBA has errors."
for attempt in $(seq 1 30); do
    if runuser -u hannes -- env -u PGPASSWORD -u PGPASSFILE PGSSLMODE=require \
        psql -w -h localhost -p 5432 -U hannes -d MXServer -X -q -c 'SELECT 1' \
        >"$archive/freeze-check.log" 2>&1; then
        sleep 1
    elif grep -Eq 'pg_hba\.conf (rejects connection|lehnt Verbindung ab)' "$archive/freeze-check.log"; then
        break
    else
        fail "Could not verify the freeze; see $archive/freeze-check.log."
    fi
done
grep -Eq 'pg_hba\.conf (rejects connection|lehnt Verbindung ab)' "$archive/freeze-check.log" ||
    fail "Freeze did not block TCP connections."
[[ "$("${native[@]}" -c "SELECT count(*) FROM pg_stat_activity WHERE datname='MXServer'")" == 0 ]] ||
    fail "MXServer still has sessions. No sessions were terminated; inspect clients and retry."

echo "Creating final database and role backups..."
runuser -u postgres -- pg_dump -w -h /var/run/postgresql -p 5432 -U postgres \
    -d MXServer -Fc > "$archive/MXServer.dump"
pg_restore -l "$archive/MXServer.dump" > "$archive/dump-list"
pg_restore -f /dev/null "$archive/MXServer.dump"
runuser -u postgres -- pg_dumpall -w -h /var/run/postgresql -p 5432 -U postgres \
    --roles-only | sed '/^CREATE ROLE postgres;$/d' > "$archive/roles.sql"
sha256sum "$archive/MXServer.dump" > "$archive/MXServer.sha256"

mkdir -- "$data_root"
install -d -m 0700 "$data_root/data"
install -d -m 0755 "$data_root/tls"
uid="$(docker run --rm --entrypoint id camera-postgres-db -u postgres)"
gid="$(docker run --rm --entrypoint id camera-postgres-db -g postgres)"
chown "$uid:$gid" "$data_root/data"
install -o "$uid" -g "$gid" -m 0644 "$cert" "$data_root/tls/server.crt"
install -o "$uid" -g "$gid" -m 0600 "$key" "$data_root/tls/server.key"
python3 - "$envfile" "$data_root" "$stage_port" <<'PY'
import os
import pwd
import secrets
import sys

path, root, port = sys.argv[1:]
owner = pwd.getpwnam("hannes")
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as out:
    out.write(f"MXSERVER_POSTGRES_DATA_DIR={root}/data\n")
    out.write(f"MXSERVER_POSTGRES_TLS_DIR={root}/tls\n")
    out.write(f"MXSERVER_POSTGRES_PORT={port}\n")
    out.write(f"MXSERVER_POSTGRES_BOOTSTRAP_PASSWORD={secrets.token_urlsafe(36)}\n")
os.chown(path, owner.pw_uid, owner.pw_gid)
PY
[[ "$(stat -c %U "$envfile")" == hannes ]] || fail "Unexpected hannes UID; .env ownership needs correction."
cat > "$archive/stage.yml" <<'YAML'
services:
  db:
    command: !override
      - postgres
      - -c
      - port=55432
      - -c
      - listen_addresses=127.0.0.1
      - -c
      - hba_file=/etc/postgresql/mxserver-pg_hba.conf
      - -c
      - ssl=on
      - -c
      - ssl_cert_file=/run/mxserver-tls/server.crt
      - -c
      - ssl_key_file=/run/mxserver-tls/server.key
      - -c
      - timezone=Europe/Berlin
      - -c
      - datestyle=ISO, DMY
      - -c
      - default_text_search_config=pg_catalog.german
YAML
stage=("${compose[@]}" -f "$archive/stage.yml")
target_created=1
"${stage[@]}" up -d --no-build
for attempt in $(seq 1 90); do
    [[ "$(docker inspect -f '{{.State.Health.Status}}' mxserver-postgres-db-1 2>/dev/null || true)" == healthy ]] && break
    sleep 2
done
[[ "$(docker inspect -f '{{.State.Health.Status}}' mxserver-postgres-db-1)" == healthy ]] ||
    fail "Staging PostgreSQL is unhealthy."
if ! docker exec -i --user postgres mxserver-postgres-db-1 psql -X -q -v ON_ERROR_STOP=1 \
    -U postgres -d postgres < "$archive/roles.sql" > "$archive/roles-restore.log" 2>&1; then
    fail "Role restoration failed; diagnostics are private in $archive."
fi
docker exec -i --user postgres mxserver-postgres-db-1 pg_restore --exit-on-error --create \
    -U postgres -d postgres < "$archive/MXServer.dump"
docker exec --user postgres mxserver-postgres-db-1 pg_dump --no-owner --no-privileges -d MXServer >/dev/null
echo "Comparing source and restored table contents..."
runuser -u postgres -- psql -X -q -At -v ON_ERROR_STOP=1 \
    -h /var/run/postgresql -p 5432 -U postgres -d MXServer \
    < "$root/mxserver/verify-postgres-data.sql" > "$archive/source.check"
docker exec -i --user postgres mxserver-postgres-db-1 psql -X -q -At -v ON_ERROR_STOP=1 \
    -U postgres -d MXServer < "$root/mxserver/verify-postgres-data.sql" > "$archive/stage.check"
[[ -s "$archive/source.check" ]] && cmp -s "$archive/source.check" "$archive/stage.check" ||
    fail "Source and restored table checksums differ; see private files in $archive."
[[ "$("${native[@]}" -c "SELECT count(*) FROM pg_stat_activity WHERE datname='MXServer'")" == 0 ]] ||
    fail "MXServer sessions appeared after the dump; inspect clients and retry."
if runuser -u hannes -- env -u PGPASSWORD -u PGPASSFILE PGSSLMODE=require \
    psql -w -h localhost -p 5432 -U hannes -d MXServer -X -q -c 'SELECT 1' \
    >"$archive/freeze-check.log" 2>&1 || ! grep -Eq 'pg_hba\.conf (rejects connection|lehnt Verbindung ab)' "$archive/freeze-check.log"; then
    fail "The native connection freeze is no longer verified."
fi
echo "Staging restore and mirror-compatible dump completed."

python3 - /home/hannes/.pgpass "$archive/pgpass-stage" "$stage_port" <<'PY'
import os
from pathlib import Path
import sys

source, target, port = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
lines = []
for line in source.read_text().splitlines():
    if not line or line.startswith("#"):
        continue
    fields, start, escaped = [], 0, False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":" and len(fields) < 4:
            fields.append(line[start:index])
            start = index + 1
    fields.append(line[start:])
    if len(fields) == 5 and fields[1] in ("5432", "*"):
        fields[1] = port
        lines.append(":".join(fields))
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as out:
    out.write("\n".join(lines) + "\n")
PY
result="$(env -u PGPASSWORD PGPASSFILE="$archive/pgpass-stage" PGSSLMODE=require \
    psql -w -h localhost -p "$stage_port" -U hannes -d MXServer -X -At -v ON_ERROR_STOP=1 \
    -c 'SELECT current_user,current_database(),ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()')"
[[ "$result" == 'hannes|MXServer|t' ]] || fail "Staging credentials or TLS failed."
rm -f -- "$archive/pgpass-stage"

echo "Stopping staging and native PostgreSQL; preparing production port..."
"${stage[@]}" stop db
native_stopped=1
pg_ctlcluster 16 main stop
native_config_changed=1
sed '/^[[:space:]]*auto[[:space:]]*$/s/auto/manual/' "$archive/start.conf" > "$archive/start.conf.new"
install -o postgres -g postgres -m 0644 "$archive/start.conf.new" "$startconf"
cp -p -- "$archive/pg_hba.conf" "$hba"
freeze_installed=0
sed 's/^MXSERVER_POSTGRES_PORT=55432$/MXSERVER_POSTGRES_PORT=5432/' "$envfile" > "$archive/env-production"
install -o hannes -g "$(id -gn hannes)" -m 0600 "$archive/env-production" "$envfile"
rm -f -- "$archive/env-production"
mirror_changed=1
install -o root -g root -m 0750 "$root/mxserver/sync-latitude.sh" "$mirror"
boot_gate_changed=1
install -o root -g root -m 0755 "$root/mxserver/wait-latitude-postgres.sh" "$boot_wait"
install -d -o root -g root -m 0755 "$(dirname "$boot_dropin")"
install -o root -g root -m 0644 "$root/mxserver/glassfish-mxserver-postgres.conf" "$boot_dropin"
systemctl daemon-reload
marker_created=1
install -o root -g root -m 0644 /dev/null "$marker"

# From this point TCP clients may write to Docker, even if a later check fails.
exposed=1
"${compose[@]}" up -d --no-build --force-recreate
for attempt in $(seq 1 90); do
    [[ "$(docker inspect -f '{{.State.Health.Status}}' mxserver-postgres-db-1 2>/dev/null || true)" == healthy ]] && break
    sleep 2
done
[[ "$(docker inspect -f '{{.State.Health.Status}}' mxserver-postgres-db-1)" == healthy ]] ||
    fail "Production Docker PostgreSQL is unhealthy."
runuser -u hannes -- env -u PGPASSWORD -u PGPASSFILE PGSSLMODE=require \
    psql -w -h localhost -p 5432 -U hannes -d MXServer -X -q -c 'SELECT 1' >/dev/null ||
    fail "Production database login failed."
docker exec --user postgres mxserver-postgres-db-1 pg_dump --no-owner --no-privileges -d MXServer >/dev/null
systemctl start glassfish
for attempt in $(seq 1 90); do
    count="$(docker exec --user postgres mxserver-postgres-db-1 psql -X -At -U postgres -d postgres \
        -c "SELECT count(*) FROM pg_stat_activity WHERE datname='MXServer' AND usename='hannes' AND application_name='PostgreSQL JDBC Driver'")"
    [[ "$count" -gt 0 ]] && break
    sleep 2
done
[[ "$count" -gt 0 ]] ||
    fail "No GlassFish JDBC session observed. Pools may initialize lazily: perform an authenticated application database read and verify the JDBC session. Do not rerun the cutover."
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8081/)" || fail "GlassFish HTTP is unreachable."
case "$code" in 2??|3??|401|403) ;; *) fail "GlassFish HTTP returned $code." ;; esac
complete=1
echo "MXServer Docker PostgreSQL is running on port 5432; GlassFish reconnected."
echo "Native data and the final encrypted-filesystem backup remain untouched at $archive."
echo "Encrypt mxserver-postgres/.env with ./private/private.sh --crypt mxserver-postgres."
echo "The native PostgreSQL data directory is retained; do not restart it without a data reconciliation plan."
