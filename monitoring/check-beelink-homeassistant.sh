#!/usr/bin/env bash

check_beelink_homeassistant() {
    local host="${BEELINK_HOST:-beelink}"
    local code output identity state health

    code="$(curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' \
        --connect-timeout 3 --max-time 8 "http://$host:8123/" 2>/dev/null)" || code="000"
    case "$code" in
        2[0-9][0-9]|3[0-9][0-9]|401|403) record OK "beelink Home Assistant" "HTTP $code" ;;
        *) record FAIL "beelink Home Assistant" "HTTP $code" ;;
    esac

    if ! output="$(ssh -o BatchMode=yes -o ConnectTimeout=10 \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=2 \
        -o StrictHostKeyChecking=yes "$host" /bin/bash -s <<'REMOTE'
printf '%s\n' "$(hostname -s)"
docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' homeassistant
REMOTE
    )"; then
        record FAIL "beelink SSH / Home Assistant container" "SSH or container inspection failed"
        return
    fi
    identity="${output%%$'\n'*}"
    if [[ "${identity,,}" != beelink* || "$output" != *$'\n'* ]]; then
        record FAIL "beelink SSH / Home Assistant container" "unexpected hostname or incomplete response"
        return
    fi
    IFS='|' read -r state health <<< "${output#*$'\n'}"
    if [[ "$state" == running && ( "$health" == healthy || "$health" == none ) ]]; then
        record OK "beelink container homeassistant" "state=$state health=$health"
    else
        record FAIL "beelink container homeassistant" "state=$state health=$health"
    fi
}
