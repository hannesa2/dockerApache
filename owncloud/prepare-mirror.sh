#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APACHE_DIR="$REPO_ROOT/apache"
ENV_FILE="$APACHE_DIR/.env"
DEFAULT_ROOT="/Volumes/SamsungT7_2T/ownCloudBackup/owncloud"

env_value() {
    local key="$1"
    if [[ -f "$ENV_FILE" ]]; then
        awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); gsub(/^["'\"']|["'\"']$/, ""); value=$0} END {print value}' "$ENV_FILE"
    fi
}

OWNCLOUD_MIRROR_DIR="$(env_value OWNCLOUD_MIRROR_DIR)"
OWNCLOUD_MIRROR_DIR="${OWNCLOUD_MIRROR_DIR:-$DEFAULT_ROOT}"
OWNCLOUD_UID="$(env_value OWNCLOUD_UID)"
OWNCLOUD_UID="${OWNCLOUD_UID:-$(id -u)}"

if [[ "$(id -u)" != "$OWNCLOUD_UID" ]]; then
    echo "ERROR: OWNCLOUD_UID=$OWNCLOUD_UID does not match this macOS account ($(id -u))." >&2
    exit 1
fi

if ! /sbin/mount | /usr/bin/awk '$3 == "/Volumes/SamsungT7_2T" { mounted = 1 } END { exit !mounted }'; then
    echo "ERROR: SamsungT7_2T is not mounted; refusing to prepare the mirror." >&2
    exit 1
fi

CODE_DIR="$OWNCLOUD_MIRROR_DIR/owncloud"
DATA_DIR="$OWNCLOUD_MIRROR_DIR/data"
SOURCE_CONFIG_DIR="$OWNCLOUD_MIRROR_DIR/source-config"
RUNTIME_CONFIG_DIR="$OWNCLOUD_MIRROR_DIR/runtime-config"

if [[ ! -f "$CODE_DIR/version.php" ]] || ! grep -q "\$OC_VersionString = '10.13.4';" "$CODE_DIR/version.php"; then
    echo "ERROR: Expected the existing ownCloud 10.13.4 code at $CODE_DIR." >&2
    exit 1
fi
if [[ ! -f "$DATA_DIR/.ocdata" || ! -f "$CODE_DIR/config/config.php" ]]; then
    echo "ERROR: Existing ownCloud data or config is missing; refusing to initialize." >&2
    exit 1
fi

mkdir -p "$SOURCE_CONFIG_DIR" "$RUNTIME_CONFIG_DIR" "$OWNCLOUD_MIRROR_DIR/postgres"
touch "$OWNCLOUD_MIRROR_DIR/.owncloud-mirror-managed" \
    "$CODE_DIR/.owncloud-mirror-managed" \
    "$DATA_DIR/.owncloud-mirror-managed" \
    "$SOURCE_CONFIG_DIR/.owncloud-mirror-managed"
if [[ ! -f "$SOURCE_CONFIG_DIR/config.php" ]]; then
    install -m 0600 "$CODE_DIR/config/config.php" "$SOURCE_CONFIG_DIR/config.php"
fi
chmod 0700 "$SOURCE_CONFIG_DIR"
chmod 0600 "$SOURCE_CONFIG_DIR/config.php"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE is missing; copy apache/.env.example to apache/.env first." >&2
    exit 1
fi
python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import os
import secrets
import sys

path = Path(sys.argv[1])
lines = path.read_text().splitlines()
keys = ("OWNCLOUD_DB_PASSWORD", "OWNCLOUD_POSTGRES_SUPERUSER_PASSWORD")
for key in keys:
    matches = [i for i, line in enumerate(lines) if line.startswith(key + "=")]
    if len(matches) > 1:
        raise SystemExit(f"Duplicate {key} entries in {path}; left it unchanged.")
    value = lines[matches[0]].split("=", 1)[1].strip().strip("\"'") if matches else ""
    if not value or value.startswith(("generate-a-private-password", "change-this-owncloud")):
        new_line = f"{key}={secrets.token_hex(32)}"
        if matches:
            lines[matches[0]] = new_line
        else:
            lines.append(new_line)

path.write_text("\n".join(lines) + "\n")
os.chmod(path, 0o600)
PY
if ! grep -q '^OWNCLOUD_UID=' "$ENV_FILE"; then
    printf 'OWNCLOUD_UID=%s\n' "$(id -u)" >> "$ENV_FILE"
fi
chmod 0600 "$ENV_FILE"

docker compose -f "$APACHE_DIR/docker-compose.yml" --profile owncloud config --quiet
docker compose -f "$APACHE_DIR/docker-compose.yml" --profile owncloud build owncloud
docker compose -f "$APACHE_DIR/docker-compose.yml" up -d apache
docker compose -f "$APACHE_DIR/docker-compose.yml" exec -T apache apache2ctl -t
docker compose -f "$APACHE_DIR/docker-compose.yml" exec -T apache apache2ctl graceful

echo "Prepared ownCloud 10.13.4 mirror storage and PHP 7.4 image."
echo "Run sudo ./owncloud/install-latitude-sync.sh on latitude for the initial sync and daily schedule."
