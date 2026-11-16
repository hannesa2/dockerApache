#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C

for attempt in $(seq 1 150); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
        mxserver-postgres-db-1 2>/dev/null || true)"
    if [[ "$status" == healthy ]] &&
        docker exec --user postgres mxserver-postgres-db-1 \
            psql -X -w -v ON_ERROR_STOP=1 -U postgres -d MXServer -q -c 'SELECT 1' >/dev/null 2>&1; then
        exit 0
    fi
    sleep 2
done
echo "ERROR: MXServer Docker PostgreSQL did not become ready; GlassFish will not start." >&2
exit 1
