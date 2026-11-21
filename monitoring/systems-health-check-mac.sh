#!/usr/bin/env bash
set -uo pipefail
export LC_ALL=C
export LANG=C

RECIPIENT="hannes.software@gmx.at"
PRIMARY_HOST="${PRIMARY_HOST:-beelink}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/systems-health-monitor"
STATE_FILE="$STATE_DIR/mac2016-failed-checks"
LOCK_DIR="$STATE_DIR/mac2016-check.lock"
MAIL_BIN="$(command -v mail || true)"

mkdir -p "$STATE_DIR"
chmod 0700 "$STATE_DIR"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    lock_pid=""
    if [[ -f "$LOCK_DIR/pid" ]]; then
        read -r lock_pid < "$LOCK_DIR/pid" || true
    fi
    if [[ "$lock_pid" =~ ^[0-9]+$ ]] && kill -0 "$lock_pid" 2>/dev/null; then
        echo "Another mac2016 systems health check is already running; skipping."
        exit 0
    fi
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || {
        echo "Another mac2016 systems health check is already starting; skipping."
        exit 0
    }
    mkdir "$LOCK_DIR" 2>/dev/null || {
        echo "Another mac2016 systems health check is already running; skipping."
        exit 0
    }
fi
printf '%s\n' "$$" > "$LOCK_DIR/pid"

checks_file="$(mktemp "$STATE_DIR/mac2016-checks.XXXXXX")"
previous_file="$(mktemp "$STATE_DIR/mac2016-previous.XXXXXX")"
current_failures="$(mktemp "$STATE_DIR/mac2016-failures.XXXXXX")"
new_failures="$(mktemp "$STATE_DIR/mac2016-new-failures.XXXXXX")"
resolved_failures="$(mktemp "$STATE_DIR/mac2016-resolved.XXXXXX")"

cleanup() {
    rm -f "$checks_file" "$previous_file" "$current_failures" \
        "$new_failures" "$resolved_failures"
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

record() {
    printf '%s|%s|%s\n' "$1" "$2" "$3" | tee -a "$checks_file"
}

check_container() {
    local host="$1"
    local name="$2"
    local status state health
    if ! status="$(docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null)"; then
        record FAIL "$host container $name" "missing or Docker unavailable"
        return
    fi

    IFS='|' read -r state health <<< "$status"
    if [[ "$state" == running && ( "$health" == healthy || "$health" == none ) ]]; then
        if [[ "$health" == none ]]; then
            record OK "$host container $name" "running"
        else
            record OK "$host container $name" "running (healthy)"
        fi
    else
        record FAIL "$host container $name" "state=$state health=$health"
    fi
}

check_http() {
    local name="$1"
    local url="$2"
    local resolve="${3:-}"
    local max_time="${4:-8}"
    local code
    local options=(--silent --insecure --output /dev/null --write-out '%{http_code}' \
        --connect-timeout 3 --max-time "$max_time")
    if [[ -n "$resolve" ]]; then
        options+=(--resolve "$resolve")
    fi
    code="$(curl "${options[@]}" "$url" 2>/dev/null)" || code="000"

    case "$code" in
        2[0-9][0-9]|3[0-9][0-9]|401|403)
            record OK "$name" "HTTP $code"
            ;;
        *)
            record FAIL "$name" "HTTP $code"
            ;;
    esac
}

if [[ -z "$MAIL_BIN" ]]; then
    echo "ERROR: mail command is unavailable." >&2
    exit 1
fi

host_name="$(hostname -s 2>/dev/null || hostname)"
if [[ "${host_name,,}" == mac2016* ]]; then
    record OK "mac2016 host identity" "$host_name"
else
    record FAIL "mac2016 host identity" "expected mac2016; got $host_name"
fi

if docker info >/dev/null 2>&1; then
    record OK "mac2016 OrbStack/Docker" "available"
else
    record FAIL "mac2016 OrbStack/Docker" "unavailable"
fi

if mount | grep -qE ' on /Volumes/SamsungT5_1T( |/)'; then
    record OK "mac2016 Samsung T5" "mounted"
else
    record FAIL "mac2016 Samsung T5" "not mounted at /Volumes/SamsungT5_1T"
fi

server_root="$HOME/server"
if [[ -d "$server_root/apache/mysql" && -d "$server_root/nextcloud/postgres" &&
      -d "$server_root/gitlab/data" && -d "$server_root/githubArtifactsPy/files" ]]; then
    free_kb="$(df -Pk "$server_root" | awk 'NR == 2 {print $4}')"
    if [[ "$free_kb" =~ ^[0-9]+$ ]] && (( free_kb >= 50 * 1024 * 1024 )); then
        record OK "mac2016 server storage" "$server_root available; at least 50 GiB free"
    else
        record FAIL "mac2016 server storage" "$server_root has less than 50 GiB free or capacity is unknown"
    fi
else
    record FAIL "mac2016 server storage" "expected service data is missing under $server_root"
fi

if nc -z -w 3 127.0.0.1 22 >/dev/null 2>&1; then
    record OK "mac2016 SSH service" "listening on port 22"
else
    record FAIL "mac2016 SSH service" "not listening on port 22"
fi

mac_containers=(
    apache-apache-1
    apache-db-1
    apache-github-artifacts-1
    apache-owncloud-1
    apache-owncloud-db-1
    apache-mxserver-1
    apache-mxserver-db-1
    apache-mxserver-mail-1
    nextcloud-nextcloud-1
    nextcloud-db-1
    nextcloud-redis-1
    gitlab16
)
for container in "${mac_containers[@]}"; do
    check_container "mac2016" "$container"
done

check_http "mac2016 Apache" "http://127.0.0.1:9080/"
check_http "mac2016 Nextcloud" "http://127.0.0.1:8082/status.php"
check_http "mac2016 ownCloud" "https://www16.mxtracks.info:9443/owncloud/status.php" \
    "www16.mxtracks.info:9443:127.0.0.1"
check_http "mac2016 GitLab mirror" "https://git16.mxtracks.info/users/sign_in" \
    "git16.mxtracks.info:443:127.0.0.1" 30
check_http "mac2016 MXServer" "https://www16.mxtracks.info:9433/backend/MXServer/rest/version" \
    "www16.mxtracks.info:9433:127.0.0.1"

if [[ -r "$HOME/bin/check-beelink-homeassistant.sh" ]]; then
    source "$HOME/bin/check-beelink-homeassistant.sh"
    check_beelink_homeassistant
else
    record FAIL "beelink Home Assistant monitoring" "check helper is missing"
fi

owncloud_status="$(curl --silent --insecure --fail --connect-timeout 3 --max-time 8 \
    --resolve "www16.mxtracks.info:9443:127.0.0.1" \
    "https://www16.mxtracks.info:9443/owncloud/status.php" 2>/dev/null)" || owncloud_status=""
if [[ -n "$owncloud_status" ]] && printf '%s' "$owncloud_status" | \
    python3 -c 'import json,sys; s=json.load(sys.stdin); sys.exit(0 if s.get("installed") and s.get("version","").startswith("11.1.0") and not s.get("maintenance") else 1)' 2>/dev/null; then
    record OK "mac2016 ownCloud application" "11.1.0 installed and out of maintenance"
else
    record FAIL "mac2016 ownCloud application" "not installed, not 11.1.0, or in maintenance mode"
fi

remote_output="$(
    ssh -o BatchMode=yes \
        -o ConnectTimeout=10 \
        -o ServerAliveInterval=10 \
        -o ServerAliveCountMax=2 \
        -o StrictHostKeyChecking=yes \
        "$PRIMARY_HOST" /bin/bash -s <<'REMOTE'
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
printf 'HOST|%s\n' "$(hostname)"
if [[ "$(hostname -s)" != beelink ]]; then
    printf 'CHECK|FAIL|Primary host identity|expected beelink; got %s\n' "$(hostname -s)"
    exit 0
fi
set -o pipefail
python3 /usr/local/bin/beelink-job-status --health-json | python3 -c '
import json,sys
report=json.load(sys.stdin)
failures=report["failures"]
for failure in failures:
    print("CHECK|FAIL|beelink health|" + failure.replace("\n", "; "))
if not failures:
    print("CHECK|OK|beelink health|containers, TLS, mount and job freshness checked")
'
REMOTE
)" || {
    record FAIL "Primary SSH" "host is unreachable over SSH ($PRIMARY_HOST)"
    remote_output=""
}

if [[ -n "$remote_output" ]]; then
    record OK "Primary SSH" "connected to $PRIMARY_HOST"
    while IFS='|' read -r kind result name detail; do
        case "$kind" in
            HOST)
                if [[ "${result,,}" == "$PRIMARY_HOST"* ]]; then
                    record OK "Primary host identity" "$result"
                else
                    record FAIL "Primary host identity" "expected $PRIMARY_HOST; got $result"
                fi
                ;;
            CHECK)
                record "$result" "$name" "$detail"
                ;;
        esac
    done <<< "$remote_output"
fi

sync_report="$(ssh -o BatchMode=yes -o ConnectTimeout=10 \
    -o ServerAliveInterval=10 -o ServerAliveCountMax=2 \
    -o StrictHostKeyChecking=yes "$PRIMARY_HOST" \
    'python3 /usr/local/bin/beelink-job-status --report' 2>&1)" ||
    sync_report="ERROR: Primary sync status unavailable: $sync_report"
grep '^FAIL|' "$checks_file" | sort > "$current_failures"
if [[ -f "$STATE_FILE" ]]; then
    cp "$STATE_FILE" "$previous_file"
    sort -o "$previous_file" "$previous_file"
else
    : > "$previous_file"
fi

comm -13 "$previous_file" "$current_failures" > "$new_failures"
comm -23 "$previous_file" "$current_failures" > "$resolved_failures"

if [[ ! -f "$STATE_FILE" || -s "$new_failures" || -s "$resolved_failures" ]]; then
    if [[ -s "$current_failures" ]]; then
        subject="[Availability mac2016] Issues detected: $(wc -l < "$current_failures" | tr -d ' ') check(s)"
    else
        subject="[Availability mac2016] All monitored systems are reachable"
    fi

    {
        printf 'Availability check on %s\nTime: %s\n\n' \
            "$(hostname -f 2>/dev/null || hostname)" "$(date '+%Y-%m-%d %H:%M:%S %z')"
        if [[ ! -f "$STATE_FILE" ]]; then
            printf 'Initial mac2016-hosted status report.\n\n'
        fi
        if [[ -s "$new_failures" ]]; then
            printf 'New or changed failures:\n'
            cat "$new_failures"
            printf '\n'
        fi
        if [[ -s "$resolved_failures" ]]; then
            printf 'Recovered checks:\n'
            cat "$resolved_failures"
            printf '\n'
        fi
        if [[ -s "$current_failures" ]]; then
            printf 'Current failures:\n'
            cat "$current_failures"
        else
            printf 'All monitored checks are passing.\n'
        fi
        printf '\n%s\n' "$sync_report"
        printf '\nFull check results:\n'
        cat "$checks_file"
    } | "$MAIL_BIN" -s "$subject" "$RECIPIENT" || {
        echo "ERROR: Could not submit availability notification to $RECIPIENT." >&2
        exit 1
    }

    state_tmp="$(mktemp "$STATE_DIR/mac2016-failed-checks.XXXXXX")"
    cp "$current_failures" "$state_tmp"
    chmod 0600 "$state_tmp"
    mv "$state_tmp" "$STATE_FILE"
    echo "Submitted availability notification: $subject"
else
    echo "No availability-state changes; email suppressed."
fi
