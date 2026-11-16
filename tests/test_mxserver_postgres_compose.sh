#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "$(uname -s)" == Linux ]] || { echo "This test requires Linux host networking." >&2; exit 1; }
docker image inspect camera-postgres-db >/dev/null
work="$(mktemp -d "${TMPDIR:-/tmp}/mxserver-compose-test.XXXXXXXX")"
id="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
project="mxservertest$id"
data="mxservertest-data-$id"
tls="mxservertest-tls-$id"
created_data=0
created_tls=0
compose_started=0

cleanup() {
    status=$?
    trap - EXIT
    if [[ "$compose_started" -eq 1 ]]; then
        docker compose -p "$project" --env-file "$work/env" -f "$root/mxserver-postgres/docker-compose.yml" \
            -f "$work/override.yml" down >/dev/null || status=1
    fi
    if [[ "$created_data" -eq 1 ]]; then docker volume rm "$data" >/dev/null || status=1; fi
    if [[ "$created_tls" -eq 1 ]]; then docker volume rm "$tls" >/dev/null || status=1; fi
    rm -f -- "$work/env" "$work/override.yml" "$work/failed-login" \
        "$work/roles.log" "$work/pgpass" "$work/freeze-hba" "$work/test-hba" \
        "$work/source.check" "$work/copy.check" || status=1
    rmdir -- "$work" || status=1
    exit "$status"
}
trap cleanup EXIT

port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
cat > "$work/env" <<EOF
MXSERVER_POSTGRES_DATA_DIR=/unused-for-this-test
MXSERVER_POSTGRES_TLS_DIR=/unused-for-this-test
MXSERVER_POSTGRES_PORT=$port
MXSERVER_POSTGRES_BOOTSTRAP_PASSWORD=test-bootstrap-password
EOF
cp "$root/mxserver-postgres/pg_hba.conf" "$work/test-hba"
chmod 0644 "$work/test-hba"
cat > "$work/override.yml" <<EOF
services:
  db:
    image: camera-postgres-db
    volumes: !override
      - test-data:/var/lib/postgresql/data
      - test-tls:/run/mxserver-tls:ro
      - $work/test-hba:/etc/postgresql/mxserver-pg_hba.conf:ro
volumes:
  test-data:
    external: true
    name: $data
  test-tls:
    external: true
    name: $tls
EOF

docker volume inspect "$data" >/dev/null 2>&1 && { echo "Test volume already exists." >&2; exit 1; }
docker volume inspect "$tls" >/dev/null 2>&1 && { echo "Test volume already exists." >&2; exit 1; }
docker volume create "$data" >/dev/null
created_data=1
docker volume create "$tls" >/dev/null
created_tls=1
docker run --rm --user root -v "$tls:/tls" --entrypoint sh camera-postgres-db -ec '
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost \
        -keyout /tls/server.key -out /tls/server.crt >/dev/null 2>&1
    chown postgres:postgres /tls/server.key /tls/server.crt
    chmod 600 /tls/server.key
'
compose=(docker compose -p "$project" --env-file "$work/env"
    -f "$root/mxserver-postgres/docker-compose.yml" -f "$work/override.yml")
"${compose[@]}" config --quiet
compose_started=1
"${compose[@]}" up -d --no-build
for attempt in $(seq 1 90); do
    if [[ "$("${compose[@]}" ps -q db)" != "" ]] &&
        docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$("${compose[@]}" ps -q db)" |
            grep -qx healthy; then
        break
    fi
    sleep 2
done
container="$("${compose[@]}" ps -q db)"
[[ -n "$container" && "$(docker inspect -f '{{.State.Health.Status}}' "$container")" == healthy ]] ||
    { "${compose[@]}" logs --tail 40 db >&2; exit 1; }

if ! pg_dumpall -w -h localhost -U hannes --roles-only |
    sed '/^CREATE ROLE postgres;$/d' |
    docker exec -i --user postgres "$container" psql -X -q -v ON_ERROR_STOP=1 \
        -U postgres -d postgres >"$work/roles.log" 2>&1; then
    echo "ERROR: Native role restoration failed; sensitive diagnostics will be removed." >&2
    exit 1
fi
docker exec --user postgres "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres \
    -c 'CREATE DATABASE "MXServer" OWNER hannes' >/dev/null
python3 - "$HOME/.pgpass" "$work/pgpass" "$port" <<'PY'
import os
from pathlib import Path
import sys

source, target, port = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
if not source.is_file() or source.stat().st_mode & 0o077:
    raise SystemExit("ERROR: ~/.pgpass must exist and have private permissions.")
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
result="$(PGPASSFILE="$work/pgpass" PGSSLMODE=require psql -w -h localhost -p "$port" \
    -U hannes -d MXServer -X -At -v ON_ERROR_STOP=1 \
    -c 'SELECT current_user, ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()')"
[[ "$result" == 'hannes|t' ]] || { echo "Existing credentials or TLS failed." >&2; exit 1; }

docker exec -i --user postgres "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres <<'SQL'
CREATE ROLE mxserver_test LOGIN PASSWORD 'test-client-password';
CREATE DATABASE mxserver_test OWNER mxserver_test;
SQL
result="$(PGPASSWORD=test-client-password PGSSLMODE=require psql -w -h 127.0.0.1 -p "$port" \
    -U mxserver_test -d mxserver_test -X -At -v ON_ERROR_STOP=1 \
    -c 'SELECT current_user, ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()')"
[[ "$result" == 'mxserver_test|t' ]] || { echo "TLS client login failed." >&2; exit 1; }
if PGPASSWORD=wrong PGSSLMODE=require psql -w -h 127.0.0.1 -p "$port" \
    -U mxserver_test -d mxserver_test -X -c 'SELECT 1' >"$work/failed-login" 2>&1; then
    echo "ERROR: Invalid password was accepted." >&2
    exit 1
fi
grep -q 'password authentication failed' "$work/failed-login" ||
    { echo "ERROR: Wrong-password test did not reach authentication." >&2; exit 1; }
docker exec --user postgres "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d mxserver_test \
    -c 'CREATE TABLE persistence_test (value integer); INSERT INTO persistence_test VALUES (42)' >/dev/null
docker exec -i --user postgres "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d mxserver_test <<'SQL' >/dev/null
CREATE SCHEMA other;
CREATE TABLE other.sample (value text);
INSERT INTO other.sample VALUES ('one'), ('two');
CREATE MATERIALIZED VIEW other.sample_view AS SELECT * FROM other.sample;
SQL
docker exec --user postgres "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres \
    -c 'CREATE DATABASE mxserver_copy OWNER mxserver_test' >/dev/null
docker exec --user postgres "$container" pg_dump -Fc -U postgres -d mxserver_test |
    docker exec -i --user postgres "$container" pg_restore --exit-on-error -U postgres -d mxserver_copy
for database in mxserver_test mxserver_copy; do
    target="$work/source.check"
    [[ "$database" == mxserver_copy ]] && target="$work/copy.check"
    docker exec -i --user postgres "$container" psql -X -q -At -v ON_ERROR_STOP=1 \
        -U postgres -d "$database" < "$root/mxserver/verify-postgres-data.sql" > "$target"
done
[[ "$(wc -l < "$work/source.check")" -eq 3 ]] &&
    grep -q '^other.sample|2|' "$work/source.check" &&
    grep -q '^other.sample_view|2|' "$work/source.check" &&
    cmp -s "$work/source.check" "$work/copy.check" ||
    { echo "ERROR: Restored table-content verification failed." >&2; exit 1; }
docker exec --user postgres "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d mxserver_copy \
    -c "UPDATE other.sample SET value='changed' WHERE value='one'" >/dev/null
docker exec -i --user postgres "$container" psql -X -q -At -v ON_ERROR_STOP=1 \
    -U postgres -d mxserver_copy < "$root/mxserver/verify-postgres-data.sql" > "$work/copy.check"
! cmp -s "$work/source.check" "$work/copy.check" ||
    { echo "ERROR: Table-content verification missed modified data." >&2; exit 1; }
"${compose[@]}" restart db >/dev/null
for attempt in $(seq 1 90); do
    [[ "$(docker inspect -f '{{.State.Health.Status}}' "$container")" == healthy ]] && break
    sleep 2
done
result="$(docker exec --user postgres "$container" psql -X -v ON_ERROR_STOP=1 -At \
    -U postgres -d mxserver_test -c 'SELECT value FROM persistence_test')"
[[ "$result" == 42 ]] || { echo "ERROR: Container data did not persist." >&2; exit 1; }

docker exec --user postgres "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres \
    -c "ALTER SYSTEM SET lc_messages = 'de_DE.UTF-8'" >/dev/null
python3 "$root/mxserver/build-freeze-hba.py" "$root/mxserver-postgres/pg_hba.conf" "$work/freeze-hba"
cat "$work/freeze-hba" > "$work/test-hba"
docker exec --user postgres "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres \
    -c 'SELECT pg_reload_conf()' >/dev/null
for attempt in $(seq 1 30); do
    rules="$(docker exec --user postgres "$container" psql -X -At -v ON_ERROR_STOP=1 \
        -U postgres -d postgres -c "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL")"
    [[ "$rules" == 0 ]] || { echo "ERROR: Freeze HBA is invalid." >&2; exit 1; }
    active="$(PGPASSFILE="$work/pgpass" PGSSLMODE=require psql -w -h localhost -p "$port" \
        -U hannes -d MXServer -X -c 'SELECT 1' >"$work/failed-login" 2>&1 && echo allowed || echo rejected)"
    [[ "$active" == rejected ]] && grep -q 'pg_hba.conf lehnt Verbindung ab' "$work/failed-login" && break
    sleep 1
done
[[ "$active" == rejected ]] && grep -q 'pg_hba.conf lehnt Verbindung ab' "$work/failed-login" ||
    { echo "ERROR: Freeze test did not reach the HBA rejection." >&2; exit 1; }
if docker exec --user postgres "$container" psql -w -X -U hannes -d MXServer \
    -c 'SELECT 1' >"$work/failed-login" 2>&1; then
    echo "ERROR: Freeze HBA allowed a non-postgres local role." >&2
    exit 1
fi
grep -q 'pg_hba.conf lehnt Verbindung ab' "$work/failed-login" ||
    { echo "ERROR: Local freeze test did not reach the HBA rejection." >&2; exit 1; }
docker exec --user postgres "$container" pg_dump -Fc -U postgres -d MXServer >/dev/null
result="$(PGPASSWORD=test-client-password PGSSLMODE=require psql -w -h 127.0.0.1 -p "$port" \
    -U mxserver_test -d mxserver_test -X -At -v ON_ERROR_STOP=1 -c 'SELECT current_user')"
[[ "$result" == mxserver_test ]] || { echo "ERROR: Freeze affected an unrelated database." >&2; exit 1; }
echo "Compose test passed: credentials, TLS, restart persistence and MXServer freeze."
