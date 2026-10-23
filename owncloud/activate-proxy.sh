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

cp "$APACHE_DIR/vhosts/www16-owncloud.inc" "$APACHE_DATA_DIR/vhosts/www16-owncloud.inc"
python3 - "$RUNTIME_VHOST" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
include = "    Include /etc/apache2/sites-enabled/www16-owncloud.inc"
if include not in text:
    count = text.count("    Include /etc/apache2/sites-enabled/www16-github.inc")
    if count != 2:
        raise SystemExit(
            f"Expected both www16 GitHub include directives in {path}; left it unchanged."
        )
    text = text.replace(
        "    Include /etc/apache2/sites-enabled/www16-github.inc",
        include + "\n    Include /etc/apache2/sites-enabled/www16-github.inc",
    )
    path.write_text(text)
PY

docker compose -f "$APACHE_DIR/docker-compose.yml" exec -T apache apache2ctl -t
docker compose -f "$APACHE_DIR/docker-compose.yml" exec -T apache apache2ctl graceful

STATUS_BODY="$(mktemp)"
trap 'rm -f "$STATUS_BODY"' EXIT
HTTP_STATUS="$(curl --silent --show-error --max-time 30 \
    --output "$STATUS_BODY" --write-out '%{http_code}' \
    https://www16.mxtracks.info:8443/owncloud/status.php)" || {
    echo "ERROR: Could not reach ownCloud status endpoint over HTTPS." >&2
    exit 1
}
if [[ "$HTTP_STATUS" != "200" ]]; then
    echo "ERROR: ownCloud status endpoint returned HTTP $HTTP_STATUS." >&2
    cat "$STATUS_BODY" >&2
    exit 1
fi

python3 - "$STATUS_BODY" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as response:
    status = json.load(response)
version = status.get("version", "")
if not status.get("installed") or not version.startswith("10.13.4"):
    raise SystemExit("ownCloud endpoint did not report an installed 10.13.4 version.")
print(f"ownCloud {version} is healthy over HTTPS.")
PY
