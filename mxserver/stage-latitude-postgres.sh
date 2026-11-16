#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

fail() { echo "ERROR: $*" >&2; exit 1; }
[[ $# -eq 0 ]] || fail "Usage: sudo bash mxserver/stage-latitude-postgres.sh"
[[ "$(id -u)" -eq 0 ]] || fail "Run as root on Latitude."
[[ "$(hostname -s | tr '[:upper:]' '[:lower:]')" == latitude ]] ||
    fail "This script is restricted to Latitude."

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
hba="$root/mxserver-postgres/pg_hba.conf"
port=55432
for command in docker pg_dump pg_dumpall pg_restore psql runuser python3 openssl \
    mountpoint ss cmp sha256sum; do
    command -v "$command" >/dev/null || fail "Missing command: $command"
done
mountpoint -q /daten || fail "/daten is not mounted."
[[ -r "$hba" ]] || fail "Cannot read $hba."
docker image inspect camera-postgres-db >/dev/null ||
    fail "Missing tested PostgreSQL image camera-postgres-db."
if ss -H -ltn "sport = :$port" | grep -q .; then
    fail "Staging port $port is already in use."
fi

native=(runuser -u postgres -- env -u PGHOST -u PGPORT -u PGUSER -u PGDATABASE
    psql -X -w -v ON_ERROR_STOP=1 -h /var/run/postgresql -p 5432 -U postgres -At -d postgres)
setting() { "${native[@]}" -c "SELECT current_setting('$1')"; }
version="$(setting server_version_num)"
[[ "$version" -ge 160000 && "$version" -lt 170000 ]] || fail "Native PostgreSQL is not version 16."
[[ "$(setting ssl)" == on ]] || fail "Native PostgreSQL TLS is not enabled."
cert="$(setting ssl_cert_file)"
key="$(setting ssl_key_file)"
[[ "$cert" == /* && "$key" == /* && -r "$cert" && -r "$key" ]] ||
    fail "Native TLS certificate or key is unavailable."
[[ -f /home/hannes/.pgpass && ! -L /home/hannes/.pgpass &&
    "$(stat -c '%a:%U' /home/hannes/.pgpass)" == 600:hannes ]] ||
    fail "Expected a private /home/hannes/.pgpass owned by hannes."

work="$(mktemp -d /daten/sicherung.postgresql/.mxserver-stage.XXXXXXXX)"
suffix="${work##*.}"
container="mxserver-stage-$suffix"
data="mxserver-stage-data-$suffix"
tls="mxserver-stage-tls-$suffix"
created_container=0
created_data=0
created_tls=0
cleanup() {
    status=$?
    trap - EXIT
    if [[ "$created_container" -eq 1 ]]; then
        docker rm -f "$container" >/dev/null || { echo "ERROR: Remove container $container manually." >&2; status=1; }
    fi
    if [[ "$created_data" -eq 1 ]]; then
        docker volume rm "$data" >/dev/null || { echo "ERROR: Remove volume $data manually." >&2; status=1; }
    fi
    if [[ "$created_tls" -eq 1 ]]; then
        docker volume rm "$tls" >/dev/null || { echo "ERROR: Remove TLS volume $tls manually." >&2; status=1; }
    fi
    rm -f -- "$work/env" "$work/MXServer.dump" "$work/roles.sql" "$work/roles.log" \
        "$work/pgpass" "$work/cert.pem" || status=1
    rmdir -- "$work" || { echo "ERROR: Inspect sensitive staging directory $work." >&2; status=1; }
    exit "$status"
}
trap cleanup EXIT

python3 - "$work/env" <<'PY'
import os
import secrets
import sys

fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as out:
    out.write("POSTGRES_PASSWORD=" + secrets.token_urlsafe(36) + "\n")
PY
echo "Creating a temporary consistent snapshot and role export..."
runuser -u postgres -- pg_dump -w -h /var/run/postgresql -p 5432 -U postgres \
    -d MXServer -Fc > "$work/MXServer.dump"
pg_restore -l "$work/MXServer.dump" >/dev/null
runuser -u postgres -- pg_dumpall -w -h /var/run/postgresql -p 5432 -U postgres \
    --roles-only | sed '/^CREATE ROLE postgres;$/d' > "$work/roles.sql"

docker volume inspect "$data" >/dev/null 2>&1 && fail "Staging volume already exists: $data"
docker volume inspect "$tls" >/dev/null 2>&1 && fail "Staging volume already exists: $tls"
docker volume create "$data" >/dev/null
created_data=1
docker volume create "$tls" >/dev/null
created_tls=1
docker run --rm --network none --user root -v "$tls:/tls" \
    -v "$cert:/source/server.crt:ro" -v "$key:/source/server.key:ro" \
    --entrypoint sh camera-postgres-db -ec \
    'install -o postgres -g postgres -m 0644 /source/server.crt /tls/server.crt
     install -o postgres -g postgres -m 0600 /source/server.key /tls/server.key'

docker create --name "$container" --network host --restart no --env-file "$work/env" \
    -e POSTGRES_INITDB_ARGS="--auth-host=scram-sha-256 --auth-local=peer" \
    -e PGPORT="$port" -v "$data:/var/lib/postgresql/data" \
    -v "$tls:/run/mxserver-tls:ro" -v "$hba:/etc/postgresql/mxserver-pg_hba.conf:ro" \
    camera-postgres-db postgres -c "port=$port" -c listen_addresses=127.0.0.1 \
    -c hba_file=/etc/postgresql/mxserver-pg_hba.conf -c ssl=on \
    -c ssl_cert_file=/run/mxserver-tls/server.crt -c ssl_key_file=/run/mxserver-tls/server.key \
    -c timezone=Europe/Berlin -c 'datestyle=ISO, DMY' \
    -c default_text_search_config=pg_catalog.german >/dev/null
created_container=1
docker start "$container" >/dev/null
for attempt in $(seq 1 90); do
    if docker logs "$container" 2>&1 | grep -Fq 'PostgreSQL init process complete; ready for start up.' &&
        docker exec --user postgres "$container" pg_isready -p "$port" >/dev/null 2>&1; then
        break
    fi
    [[ "$(docker inspect -f '{{.State.Running}}' "$container")" == true ]] ||
        fail "Staging PostgreSQL exited; inspect docker logs $container."
    sleep 2
done
docker logs "$container" 2>&1 | grep -Fq 'PostgreSQL init process complete; ready for start up.' &&
    docker exec --user postgres "$container" pg_isready -p "$port" >/dev/null ||
    fail "Staging PostgreSQL did not become ready."

if ! docker exec -i --user postgres "$container" psql -X -q -v ON_ERROR_STOP=1 \
    -U postgres -d postgres < "$work/roles.sql" > "$work/roles.log" 2>&1; then
    fail "Role restoration failed; sensitive temporary diagnostics will be removed."
fi
docker exec -i --user postgres "$container" pg_restore --exit-on-error --create \
    -U postgres -d postgres < "$work/MXServer.dump"

python3 - /home/hannes/.pgpass "$work/pgpass" "$port" <<'PY'
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
with os.fdopen(fd, "w") as stream:
    stream.write("\n".join(lines) + "\n")
PY
result="$(env -u PGPASSWORD PGPASSFILE="$work/pgpass" PGSSLMODE=require \
    psql -w -h localhost -p "$port" -U hannes -d MXServer -X -At -v ON_ERROR_STOP=1 \
    -c "SELECT current_user,current_database(),ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()")"
[[ "$result" == 'hannes|MXServer|t' ]] || fail "Existing credentials or TLS failed: $result"
openssl s_client -starttls postgres -connect "127.0.0.1:$port" -showcerts \
    </dev/null 2>/dev/null | openssl x509 -outform DER > "$work/cert.pem"
expected="$(openssl x509 -in "$cert" -outform DER | sha256sum)"
actual="$(sha256sum "$work/cert.pem")"
[[ "${expected%% *}" == "${actual%% *}" ]] || fail "Staging server certificate differs from native."
docker exec --user postgres "$container" pg_dump --no-owner --no-privileges -d MXServer >/dev/null
echo "Staging passed: native certificate, TLS, existing credentials and Docker mirror dump."
echo "Production services and the existing database were not changed."
