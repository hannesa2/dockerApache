#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APACHE_DIR="$REPO_ROOT/apache"
ENV_FILE="$APACHE_DIR/.env"

APACHE_DATA_DIR="$(awk -F= '$1 == "APACHE_DATA_DIR" {sub(/^[^=]*=/, ""); gsub(/^["'\"']|["'\"']$/, ""); value=$0} END {print value}' "$ENV_FILE")"
APACHE_DATA_DIR="${APACHE_DATA_DIR:-$APACHE_DIR/data}"
RUNTIME_VHOST="$APACHE_DATA_DIR/vhosts/www16.mxtracks.info.conf"

if [[ ! -f "$RUNTIME_VHOST" ]]; then
    echo "ERROR: Active www16 Apache vhost not found at $RUNTIME_VHOST." >&2
    exit 1
fi

cp "$APACHE_DIR/vhosts/www16-mxserver.inc" "$APACHE_DATA_DIR/vhosts/www16-mxserver.inc"
python3 - "$RUNTIME_VHOST" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = path.read_text().splitlines()
include = "    Include /etc/apache2/sites-enabled/www16-mxserver.inc"
inside_tls = False
tls_blocks = 0
updated = []
inserted = False

for line in lines:
    stripped = line.strip().lower()
    if stripped.startswith("<virtualhost") and ":443>" in stripped:
        inside_tls = True
        tls_blocks += 1
    if inside_tls and line.strip() == include:
        inserted = True
    updated.append(line)
    if inside_tls and not inserted and line.strip().lower() == "servername www16.mxtracks.info":
        updated.append(include)
        inserted = True
    if inside_tls and stripped == "</virtualhost>":
        inside_tls = False

if tls_blocks != 1:
    raise SystemExit(f"Expected one TLS vhost in {path}; left it unchanged.")
if not inserted:
    raise SystemExit(f"Could not add MXServer proxy include to {path}; left it unchanged.")

path.write_text("\n".join(updated) + "\n")
PY

docker compose -f "$APACHE_DIR/docker-compose.yml" up -d apache
docker compose -f "$APACHE_DIR/docker-compose.yml" exec -T apache apache2ctl -t
docker compose -f "$APACHE_DIR/docker-compose.yml" exec -T apache apache2ctl graceful

response_file="$(mktemp)"
trap 'rm -f "$response_file"' EXIT
http_status="$(curl -k --silent --show-error --max-time 30 \
    --output "$response_file" --write-out '%{http_code}' \
    https://www16.mxtracks.info:9433/backend/MXServer/rest/version)" || {
    echo "ERROR: Could not reach MXServer through HTTPS port 9433." >&2
    exit 1
}
if [[ "$http_status" != "200" && "$http_status" != "401" ]]; then
    echo "ERROR: MXServer endpoint returned HTTP $http_status." >&2
    cat "$response_file" >&2
    exit 1
fi

echo "MXServer is responding through https://www16.mxtracks.info:9433/backend/ (HTTP $http_status)."
