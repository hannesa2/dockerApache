#!/usr/bin/env bash
# deploy-certs-to-mirror.sh
# ─────────────────────────────────────────────────────────────────────────────
# Copies Let's Encrypt certs issued on latitude → mac2016 and reloads native
# Apache there.
#
# Run ONCE manually after initial cert issuance:
#   cd ~/git/dockerApache/apache
#   ./deploy-certs-to-mirror.sh
#
# Then install as a certbot deploy hook so it fires on every renewal:
#   sudo ln -sf "$(realpath deploy-certs-to-mirror.sh)" \
#       /etc/letsencrypt/renewal-hooks/deploy/deploy-certs-to-mirror.sh
#
# Certbot sets $RENEWED_DOMAINS and $RENEWED_LINEAGE when called as a hook;
# when run manually we deploy all configured domains.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
export LC_ALL=C; export LANG=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && source "$SCRIPT_DIR/.env"

MIRROR_HOST="${MIRROR_HOST:-mac2016}"
MIRROR_USER="${MIRROR_USER:-hannes}"
MIRROR_SSH_PORT="${MIRROR_SSH_PORT:-22}"
MIRROR_EXTRA_PATH="${MIRROR_EXTRA_PATH:-/usr/local/bin}"

# Where to store certs on mac2016 (readable by Apache)
MIRROR_SSL_DIR="${MIRROR_SSL_DIR:-/Users/${MIRROR_USER}/ssl}"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG="$SCRIPT_DIR/deploy-certs-${TIMESTAMP}.log"
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

# Domains to deploy (space-separated; override from .env if needed)
CERT_DOMAINS="${CERT_DOMAINS:-dev16.mxtracks.info www16.mxtracks.info}"

log "════════════════════════════════════════════════════════════"
log "  deploy-certs-to-mirror.sh  $TIMESTAMP"
log "  Domains : $CERT_DOMAINS"
log "  Mirror  : ${MIRROR_USER}@${MIRROR_HOST}:${MIRROR_SSL_DIR}"
log "════════════════════════════════════════════════════════════"

# Create target directories on mirror
ssh -p "$MIRROR_SSH_PORT" "${MIRROR_USER}@${MIRROR_HOST}" \
    "mkdir -p ${MIRROR_SSL_DIR}"

for domain in $CERT_DOMAINS; do
    CERT_DIR="/etc/letsencrypt/live/${domain}"

    # If running as a deploy hook, only redeploy if this domain was just renewed
    if [ -n "${RENEWED_DOMAINS:-}" ]; then
        echo "$RENEWED_DOMAINS" | grep -qw "$domain" || {
            log "  Skipping $domain (not in this renewal batch)"
            continue
        }
    fi

    log ""
    log "==> Deploying certs for $domain ..."

    # rsync -L follows symlinks (live/ contains symlinks → archive/)
    sudo rsync -L --rsh="ssh -p ${MIRROR_SSH_PORT}" \
        "${CERT_DIR}/fullchain.pem" \
        "${CERT_DIR}/privkey.pem" \
        "${CERT_DIR}/chain.pem" \
        "${MIRROR_USER}@${MIRROR_HOST}:${MIRROR_SSL_DIR}/" 2>&1 | tee -a "$LOG"

    # Rename to domain-specific filenames on mirror (avoids collision)
    ssh -p "$MIRROR_SSH_PORT" "${MIRROR_USER}@${MIRROR_HOST}" "
        mv -f '${MIRROR_SSL_DIR}/fullchain.pem' '${MIRROR_SSL_DIR}/${domain}-fullchain.pem'
        mv -f '${MIRROR_SSL_DIR}/privkey.pem'   '${MIRROR_SSL_DIR}/${domain}-privkey.pem'
        mv -f '${MIRROR_SSL_DIR}/chain.pem'     '${MIRROR_SSL_DIR}/${domain}-chain.pem'
        chmod 600 '${MIRROR_SSL_DIR}/${domain}-privkey.pem'
        echo 'Certs written to ${MIRROR_SSL_DIR}/${domain}-*.pem'
    " | tee -a "$LOG"

    log "    $domain certs deployed."
done

# Reload native Apache on mac2016 to pick up new certs
log ""
log "==> Reloading native Apache on ${MIRROR_HOST} ..."
ssh -p "$MIRROR_SSH_PORT" "${MIRROR_USER}@${MIRROR_HOST}" \
    "export PATH='${MIRROR_EXTRA_PATH}:/usr/local/bin:/usr/bin:/bin'; \
     sudo -n apachectl graceful 2>&1 || sudo -n apachectl restart 2>&1 || \
     { echo 'WARNING: could not reload Apache – run: sudo apachectl graceful  (on ${MIRROR_HOST})'; true; }" \
    | tee -a "$LOG"

log ""
log "════════════════════════════════════════════════════════════"
log "  Done.  Log: $LOG"
log "════════════════════════════════════════════════════════════"

