#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APACHE_DIR="$REPO_ROOT/apache"
ENV_FILE="$APACHE_DIR/.env"
DEFAULT_MIRROR_DIR="/Volumes/SamsungT7_2T/MXServerMirror"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE is missing; copy apache/.env.example to apache/.env first." >&2
    exit 1
fi
if [[ ! -f "$REPO_ROOT/../MXServer/pom.xml" ]]; then
    echo "ERROR: MXServer checkout must be at $REPO_ROOT/../MXServer." >&2
    exit 1
fi
if ! /sbin/mount | /usr/bin/awk '$3 == "/Volumes/SamsungT7_2T" { mounted = 1 } END { exit !mounted }'; then
    echo "ERROR: SamsungT7_2T is not mounted; refusing to prepare the mirror." >&2
    exit 1
fi

python3 - "$ENV_FILE" "$DEFAULT_MIRROR_DIR" "$(id -u)" <<'PY'
from pathlib import Path
import os
import secrets
import sys

path = Path(sys.argv[1])
default_dir = sys.argv[2]
uid = sys.argv[3]
lines = path.read_text().splitlines()
defaults = {
    "MXSERVER_MIRROR_DIR": default_dir,
    "MXSERVER_PAYARA_UID": uid,
    "MXSERVER_DB_NAME": "MXServer",
    "MXSERVER_DB_USER": "mxserver",
}
secrets_to_generate = ("MXSERVER_DB_PASSWORD", "MXSERVER_POSTGRES_PASSWORD")
for key in secrets_to_generate:
    defaults[key] = secrets.token_hex(32)

for key, default in defaults.items():
    matches = [i for i, line in enumerate(lines) if line.startswith(key + "=")]
    if len(matches) > 1:
        raise SystemExit(f"Duplicate {key} entries in {path}; left it unchanged.")
    if matches:
        current = lines[matches[0]].split("=", 1)[1].strip().strip("\"'")
        if key in secrets_to_generate and (
            not current
            or current.startswith("generate-a-private-password")
            or current.startswith("change-this-mxserver")
        ):
            lines[matches[0]] = f"{key}={default}"
    else:
        lines.append(f"{key}={default}")

path.write_text("\n".join(lines) + "\n")
os.chmod(path, 0o600)
PY

MIRROR_DIR="$(awk -F= '$1 == "MXSERVER_MIRROR_DIR" {sub(/^[^=]*=/, ""); gsub(/^["'\"']|["'\"']$/, ""); value=$0} END {print value}' "$ENV_FILE")"
MIRROR_DIR="${MIRROR_DIR:-$DEFAULT_MIRROR_DIR}"
mkdir -p "$MIRROR_DIR/postgres" "$MIRROR_DIR/pictures" "$MIRROR_DIR/config"
touch "$MIRROR_DIR/.mxserver-mirror-managed" \
    "$MIRROR_DIR/pictures/.mxserver-mirror-managed" \
    "$MIRROR_DIR/config/keyfile"
chmod 0600 "$MIRROR_DIR/config/keyfile"

docker compose -f "$APACHE_DIR/docker-compose.yml" --profile mxserver config --quiet
docker compose -f "$APACHE_DIR/docker-compose.yml" --profile mxserver build mxserver
docker compose -f "$APACHE_DIR/docker-compose.yml" --profile mxserver up -d mxserver-db mxserver-mail

echo "MXServer mirror storage and runtime are prepared."
echo "Run sudo ./mxserver/install-latitude-sync.sh on latitude to copy the database and pictures."
