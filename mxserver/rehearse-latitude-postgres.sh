#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C
umask 077

[[ $# -eq 0 ]] || { echo "Usage: $0" >&2; exit 2; }
[[ "$(id -un)" == hannes ]] || { echo "Run as hannes on Latitude, without sudo." >&2; exit 1; }
[[ "$(hostname -s | tr '[:upper:]' '[:lower:]')" == latitude ]] || {
    echo "ERROR: This rehearsal is restricted to Latitude." >&2
    exit 1
}
command -v docker >/dev/null
command -v pg_dump >/dev/null
command -v pg_dumpall >/dev/null
command -v pg_restore >/dev/null
command -v psql >/dev/null
mountpoint -q /daten || { echo "ERROR: /daten is not mounted." >&2; exit 1; }
docker image inspect camera-postgres-db >/dev/null || {
    echo "ERROR: PostgreSQL 16 image camera-postgres-db is unavailable." >&2
    exit 1
}
docker image inspect postgres:14-alpine >/dev/null || {
    echo "ERROR: PostgreSQL 14 mirror test image is unavailable: postgres:14-alpine" >&2
    echo "Pull it with: docker pull postgres:14-alpine" >&2
    exit 1
}

work="$(mktemp -d /daten/sicherung.postgresql/.mxserver-rehearsal.XXXXXXXX)"
container="mxserver-rehearsal-$(basename "$work" | cut -d. -f3)"
mirror_container="$container-mirror"
created=0
mirror_created=0
cleanup() {
    status=$?
    trap - EXIT
    if [[ "$created" -eq 1 ]]; then
        if ! docker rm -f -v "$container" >/dev/null; then
            echo "ERROR: Remove rehearsal container manually: $container" >&2
            status=1
        fi
        if [[ "$mirror_created" -eq 1 ]]; then
            if ! docker rm -f -v "$mirror_container" >/dev/null; then
                echo "ERROR: Remove rehearsal mirror container manually: $mirror_container" >&2
                status=1
            fi
        fi
    fi
    if ! rm -f -- "$work/env" "$work/MXServer.dump" "$work/roles.sql" \
        "$work/roles.log" "$work/pgpass" "$work/check.sql" "$work/source.check" \
        "$work/mirror.check"; then
        echo "ERROR: Could not remove rehearsal files in $work" >&2
        status=1
    fi
    if ! rmdir -- "$work"; then
        echo "ERROR: Could not remove rehearsal directory: $work" >&2
        status=1
    fi
    exit "$status"
}
trap cleanup EXIT

python3 - "$work/env" <<'PY'
import os
import secrets
import sys

fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    stream.write("POSTGRES_PASSWORD=" + secrets.token_urlsafe(36) + "\n")
PY

echo "Creating a consistent read-only snapshot of native MXServer..."
pg_dump -w -h localhost -U hannes -d MXServer -Fc -f "$work/MXServer.dump"
pg_restore -l "$work/MXServer.dump" >/dev/null
pg_dumpall -w -h localhost -U hannes --roles-only |
    sed '/^CREATE ROLE postgres;$/d' > "$work/roles.sql"

echo "Starting temporary PostgreSQL 16, bound only to loopback..."
docker create --name "$container" -p 127.0.0.1::5432 --env-file "$work/env" \
    -e POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 \
    camera-postgres-db >/dev/null
created=1
docker start "$container" >/dev/null
for attempt in $(seq 1 90); do
    if docker logs "$container" 2>&1 | grep -Fq 'PostgreSQL init process complete; ready for start up.' &&
        docker exec "$container" pg_isready -U postgres -d postgres >/dev/null 2>&1; then
        break
    fi
    if [[ "$(docker inspect -f '{{.State.Running}}' "$container")" != true ]]; then
        echo "ERROR: Rehearsal PostgreSQL exited." >&2
        docker logs --tail 30 "$container" >&2
        exit 1
    fi
    sleep 2
done
docker logs "$container" 2>&1 | grep -Fq 'PostgreSQL init process complete; ready for start up.' &&
    docker exec "$container" pg_isready -U postgres -d postgres >/dev/null || {
    echo "ERROR: Rehearsal PostgreSQL did not become ready." >&2
    exit 1
}

echo "Restoring role attributes and password verifiers..."
if ! docker exec -i "$container" psql -X -q -v ON_ERROR_STOP=1 -U postgres -d postgres \
    < "$work/roles.sql" > "$work/roles.log" 2>&1; then
    echo "ERROR: Role restoration failed; sensitive diagnostics will be removed." >&2
    exit 1
fi

docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres <<'SQL'
CREATE DATABASE "MXServer" OWNER hannes TEMPLATE template0
  ENCODING 'UTF8' LC_COLLATE 'de_DE.UTF-8' LC_CTYPE 'de_DE.UTF-8';
SQL
echo "Restoring snapshot into the isolated temporary database..."
docker exec -i "$container" pg_restore --exit-on-error -U postgres -d MXServer \
    < "$work/MXServer.dump"
docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d MXServer \
    -c "SELECT count(*) AS user_tables FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='r' AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%';"

echo "Enabling TLS with a temporary rehearsal certificate..."
docker exec -u root "$container" sh -ec '
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost \
        -keyout "$PGDATA/server.key" -out "$PGDATA/server.crt" >/dev/null 2>&1
    chown postgres:postgres "$PGDATA/server.key" "$PGDATA/server.crt"
    chmod 600 "$PGDATA/server.key"
    printf "%s\n" \
        "local all postgres peer" \
        "local all all scram-sha-256" \
        "host all all all scram-sha-256" > "$PGDATA/pg_hba.conf"
    chown postgres:postgres "$PGDATA/pg_hba.conf"
'
docker exec --user postgres "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres \
    -c 'ALTER SYSTEM SET ssl = on'
docker restart "$container" >/dev/null
for attempt in $(seq 1 90); do
    if docker exec "$container" pg_isready -U postgres -d postgres >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
docker exec "$container" pg_isready -U postgres -d postgres >/dev/null || {
    echo "ERROR: TLS rehearsal PostgreSQL did not become ready." >&2
    exit 1
}

port="$(docker port "$container" 5432/tcp)"
port="${port##*:}"
[[ "$port" =~ ^[0-9]+$ ]] || { echo "ERROR: Could not determine rehearsal port." >&2; exit 1; }
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
    fields = []
    start = 0
    escaped = False
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
echo "Verifying TLS and the existing hannes password against the restored verifier..."
result="$(PGPASSFILE="$work/pgpass" PGSSLMODE=require psql -w -h localhost -p "$port" \
    -U hannes -d MXServer -X -v ON_ERROR_STOP=1 -At -c \
    "SELECT current_user, current_database(), ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")"
[[ "$result" == 'hannes|MXServer|t' ]] || { echo "ERROR: TLS client verification failed: $result" >&2; exit 1; }
echo "$result"

echo "Testing the Docker mirror source against an isolated PostgreSQL 14 database..."
docker create --name "$mirror_container" --network none --env-file "$work/env" \
    postgres:14-alpine >/dev/null
mirror_created=1
docker start "$mirror_container" >/dev/null
for attempt in $(seq 1 90); do
    if docker logs "$mirror_container" 2>&1 | grep -Fq 'PostgreSQL init process complete; ready for start up.' &&
        docker exec "$mirror_container" pg_isready -U postgres -d postgres >/dev/null 2>&1; then
        break
    fi
    if [[ "$(docker inspect -f '{{.State.Running}}' "$mirror_container")" != true ]]; then
        echo "ERROR: Rehearsal mirror PostgreSQL exited." >&2
        docker logs --tail 30 "$mirror_container" >&2
        exit 1
    fi
    sleep 2
done
docker logs "$mirror_container" 2>&1 | grep -Fq 'PostgreSQL init process complete; ready for start up.' &&
    docker exec "$mirror_container" pg_isready -U postgres -d postgres >/dev/null || {
    echo "ERROR: Rehearsal mirror PostgreSQL did not become ready." >&2
    exit 1
}
docker exec -i "$mirror_container" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres <<'SQL'
CREATE ROLE mxserver LOGIN;
CREATE DATABASE "MXServer_staging" OWNER mxserver;
SQL
docker exec --user postgres "$container" pg_dump --format=plain --no-owner \
    --no-privileges --dbname=MXServer |
    docker exec -i "$mirror_container" psql -X -q -v ON_ERROR_STOP=1 -U mxserver -d MXServer_staging
source_count="$(docker exec --user postgres "$container" psql -X -At -U postgres -d MXServer \
    -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='r' AND n.nspname='public'")"
mirror_count="$(docker exec "$mirror_container" psql -X -At -U postgres -d MXServer_staging \
    -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='r' AND n.nspname='public'")"
[[ "$source_count" -gt 0 && "$source_count" == "$mirror_count" ]] || {
    echo "ERROR: PostgreSQL 14 mirror table count differs." >&2
    exit 1
}
echo "PostgreSQL 14 mirror restore succeeded: $mirror_count tables."

cat > "$work/check.sql" <<'SQL'
CREATE FUNCTION pg_temp.table_checks()
RETURNS TABLE(table_name text, row_count bigint, checksum numeric)
LANGUAGE plpgsql AS $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT n.nspname, c.relname
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind='r' AND n.nspname='public'
        ORDER BY n.nspname, c.relname
    LOOP
        RETURN QUERY EXECUTE format(
            'SELECT %L::text, count(*), coalesce(sum((''x'' || substr(md5(row_to_json(t)::text), 1, 16))::bit(64)::bigint::numeric), 0) FROM %I.%I t',
            r.nspname || '.' || r.relname, r.nspname, r.relname);
    END LOOP;
END
$$;
SELECT * FROM pg_temp.table_checks() ORDER BY table_name;
SQL
echo "Comparing row counts and checksums for every public table..."
docker exec -i --user postgres "$container" psql -X -q -At -v ON_ERROR_STOP=1 \
    -U postgres -d MXServer < "$work/check.sql" > "$work/source.check"
docker exec -i "$mirror_container" psql -X -q -At -v ON_ERROR_STOP=1 \
    -U postgres -d MXServer_staging < "$work/check.sql" > "$work/mirror.check"
[[ "$(wc -l < "$work/source.check")" -eq "$source_count" ]] &&
    cmp -s "$work/source.check" "$work/mirror.check" || {
    echo "ERROR: Mirror row counts or checksums differ." >&2
    exit 1
}
echo "All $source_count public tables match."

echo "Rehearsal completed. Production, native PostgreSQL, clients, and cron were not changed."
echo "This is not a cutover script and does not migrate the live service."
