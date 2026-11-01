#!/usr/bin/env bash
set -uo pipefail
export LC_ALL=C
export LANG=C

RECIPIENT="hannes.software@gmx.at"
MAC_HOST="mac2016"
MAC_USER="hannes"
SSH_KEY="/home/hannes/.ssh/id_rsa"
KNOWN_HOSTS="/home/hannes/.ssh/known_hosts"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/systems-health-monitor"
STATE_FILE="$STATE_DIR/failed-checks"
LOCK_FILE="$STATE_DIR/check.lock"
MAIL_BIN="$(command -v mail || true)"

mkdir -p "$STATE_DIR"
chmod 0700 "$STATE_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "Another systems health check is already running; skipping."
    exit 0
fi

if [[ -z "$MAIL_BIN" ]]; then
    echo "ERROR: mail command is unavailable." >&2
    exit 1
fi
if [[ ! -r "$SSH_KEY" || ! -r "$KNOWN_HOSTS" ]]; then
    echo "ERROR: Latitude-to-mac2016 SSH key or known_hosts is unavailable." >&2
    exit 1
fi

checks_file="$(mktemp "$STATE_DIR/checks.XXXXXX")"
previous_file="$(mktemp "$STATE_DIR/previous.XXXXXX")"
current_failures="$(mktemp "$STATE_DIR/failures.XXXXXX")"
previous_failures="$(mktemp "$STATE_DIR/old-failures.XXXXXX")"
new_failures="$(mktemp "$STATE_DIR/new-failures.XXXXXX")"
resolved_failures="$(mktemp "$STATE_DIR/resolved.XXXXXX")"
trap 'rm -f "$checks_file" "$previous_file" "$current_failures" "$previous_failures" "$new_failures" "$resolved_failures"' EXIT

record() {
    printf '%s|%s|%s\n' "$1" "$2" "$3" | tee -a "$checks_file"
}

check_unit() {
    local unit="$1"
    if systemctl is-active --quiet "$unit"; then
        record OK "Latitude systemd $unit" "active"
    else
        local state
        state="$(systemctl is-active "$unit" 2>/dev/null || true)"
        record FAIL "Latitude systemd $unit" "${state:-inactive}"
    fi
}

check_container() {
    local name="$1"
    local status
    if ! status="$(docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null)"; then
        record FAIL "Latitude container $name" "missing or Docker unavailable"
        return
    fi

    local state health
    IFS='|' read -r state health <<<"$status"
    if [[ "$state" == running && ( "$health" == healthy || "$health" == none ) ]]; then
        if [[ "$health" == none ]]; then
            record OK "Latitude container $name" "running"
        else
            record OK "Latitude container $name" "running (healthy)"
        fi
    else
        record FAIL "Latitude container $name" "state=$state health=$health"
    fi
}

check_http() {
    local name="$1"
    local url="$2"
    local code
    code="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
        --connect-timeout 3 --max-time 8 "$url" 2>/dev/null)" || code="000"

    case "$code" in
        2[0-9][0-9]|3[0-9][0-9]|401|403)
            record OK "$name" "HTTP $code"
            ;;
        *)
            record FAIL "$name" "HTTP $code"
            ;;
    esac
}

check_unit apache2
check_unit glassfish
check_unit postgresql@16-main
check_unit docker

latitude_containers=(
    apache-apache-1
    apache-db-1
    gitlab
    gitlab-runner
    homeassistant
    artifactory
    nextcloud-coredns-dot-1
    nextcloud-dnsmasq-1
)
for container in "${latitude_containers[@]}"; do
    check_container "$container"
done

check_http "Latitude native Apache" "http://127.0.0.1/"
check_http "Latitude GlassFish" "http://127.0.0.1:8081/"
check_http "Latitude Apache container" "http://127.0.0.1:8085/"
check_http "Latitude GitLab" "http://127.0.0.1:8083/users/sign_in"
check_http "Latitude Artifactory" "http://127.0.0.1:8091/"
check_http "Latitude Home Assistant" "http://127.0.0.1:8123/"

if dns_answer="$(dig +short +time=2 +tries=1 @192.168.178.129 nextcloud.mxtracks.info A 2>/dev/null)" &&
    [[ "$dns_answer" == "192.168.178.129" ]]; then
    record OK "Latitude dnsmasq" "nextcloud.mxtracks.info resolves to $dns_answer"
else
    record FAIL "Latitude dnsmasq" "expected 192.168.178.129; got ${dns_answer:-no answer}"
fi

remote_output="$(
    ssh -i "$SSH_KEY" \
        -o IdentitiesOnly=yes \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        -o ServerAliveInterval=10 \
        -o ServerAliveCountMax=2 \
        -o StrictHostKeyChecking=yes \
        -o UserKnownHostsFile="$KNOWN_HOSTS" \
        "$MAC_USER@$MAC_HOST" /bin/bash -s <<'REMOTE'
export PATH="/Applications/OrbStack.app/Contents/MacOS/xbin:/usr/local/bin:/usr/bin:/bin"
printf 'HOST|%s\n' "$(hostname)"
if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
    printf 'DOCKER|unavailable\n'
    exit 0
fi
printf 'DOCKER|available\n'

containers=(
    apache-apache-1
    apache-db-1
    apache-github-artifacts-1
    apache-owncloud-1
    apache-owncloud-db-1
    apache-mxserver-db-1
    apache-mxserver-mail-1
    nextcloud-nextcloud-1
    nextcloud-db-1
    nextcloud-redis-1
    gitlab16
)
for name in "${containers[@]}"; do
    status="$(docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null)" || status="missing|missing"
    printf 'CONTAINER|%s|%s\n' "$name" "$status"
done

probe() {
    local name="$1"
    local url="$2"
    local resolve="${3:-}"
    local code
    local options=(--silent --insecure --output /dev/null --write-out '%{http_code}' --connect-timeout 3 --max-time 8)
    if [[ -n "$resolve" ]]; then
        options+=(--resolve "$resolve")
    fi
    code="$(curl "${options[@]}" "$url" 2>/dev/null)" || code="000"
    printf 'HTTP|%s|%s\n' "$name" "$code"
}

probe "mac2016 Apache" "http://127.0.0.1:9080/"
probe "mac2016 Nextcloud" "http://127.0.0.1:8082/status.php"
probe "mac2016 ownCloud" "https://www16.mxtracks.info:8443/owncloud/status.php" "www16.mxtracks.info:8443:127.0.0.1"
probe "mac2016 GitLab mirror" "https://git16.mxtracks.info:9443/users/sign_in" "git16.mxtracks.info:9443:127.0.0.1"
probe "mac2016 MXServer" "https://www16.mxtracks.info:9433/backend/MXServer/rest/version" "www16.mxtracks.info:9433:127.0.0.1"
REMOTE
)" || {
    record FAIL "mac2016 SSH" "host is unreachable over the configured SSH connection"
    remote_output=""
}

if [[ -n "$remote_output" ]]; then
    record OK "mac2016 SSH" "connected to $MAC_HOST"
    while IFS='|' read -r kind name value extra; do
        case "$kind" in
            HOST)
                if [[ "$name" == mac2016* ]]; then
                    record OK "mac2016 host identity" "$name"
                else
                    record FAIL "mac2016 host identity" "expected mac2016; got $name"
                fi
                ;;
            DOCKER)
                if [[ "$name" == available ]]; then
                    record OK "mac2016 OrbStack/Docker" "available"
                else
                    record FAIL "mac2016 OrbStack/Docker" "$name"
                fi
                ;;
            CONTAINER)
                container_name="${name%%|*}"
                state="$value"
                health="$extra"
                if [[ "$state" == running && ( "$health" == healthy || "$health" == none ) ]]; then
                    if [[ "$health" == none ]]; then
                        record OK "mac2016 container $container_name" "running"
                    else
                        record OK "mac2016 container $container_name" "running (healthy)"
                    fi
                else
                    record FAIL "mac2016 container $container_name" "state=$state health=$health"
                fi
                ;;
            HTTP)
                endpoint="$name"
                code="$value"
                if [[ "$code" =~ ^2[0-9][0-9]$ || "$code" =~ ^3[0-9][0-9]$ || "$code" == 401 || "$code" == 403 ]]; then
                    record OK "$endpoint" "HTTP $code"
                else
                    record FAIL "$endpoint" "HTTP $code"
                fi
                ;;
        esac
    done <<<"$remote_output"
fi

grep '^FAIL|' "$checks_file" | sort >"$current_failures"
if [[ -f "$STATE_FILE" ]]; then
    cp "$STATE_FILE" "$previous_file"
    sort -o "$previous_file" "$previous_file"
else
    : >"$previous_file"
fi

comm -13 "$previous_file" "$current_failures" >"$new_failures"
comm -23 "$previous_file" "$current_failures" >"$resolved_failures"

if [[ ! -f "$STATE_FILE" || -s "$new_failures" || -s "$resolved_failures" ]]; then
    if [[ -s "$current_failures" ]]; then
        subject="[Availability] Issues detected: $(wc -l <"$current_failures" | tr -d ' ') check(s)"
    else
        subject="[Availability] All monitored systems are reachable"
    fi

    {
        printf 'Availability check on %s\nTime: %s\n\n' \
            "$(hostname -f 2>/dev/null || hostname)" "$(date '+%Y-%m-%d %H:%M:%S %z')"
        if [[ ! -f "$STATE_FILE" ]]; then
            printf 'Initial status report.\n\n'
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
        printf '\nFull check results:\n'
        cat "$checks_file"
    } | "$MAIL_BIN" -s "$subject" "$RECIPIENT" || {
        echo "ERROR: Could not send availability email to $RECIPIENT." >&2
        exit 1
    }

    state_tmp="$(mktemp "$STATE_DIR/failed-checks.XXXXXX")"
    cp "$current_failures" "$state_tmp"
    chmod 0600 "$state_tmp"
    mv "$state_tmp" "$STATE_FILE"
    echo "Sent availability notification: $subject"
else
    echo "No availability-state changes; email suppressed."
fi
