#!/bin/bash
set -euo pipefail
export LC_ALL=C LANG=C

: "${RENEWED_LINEAGE:?This hook must be run by Certbot or with RENEWED_LINEAGE set}"

site="${RENEWED_LINEAGE##*/}"
case "$site" in
    dev16.mxtracks.info|www16.mxtracks.info)
        ;;
    *)
        exit 0
        ;;
esac

tar -h -C "$RENEWED_LINEAGE" -cf - fullchain.pem privkey.pem |
ssh \
    -i /home/hannes/.ssh/mac2016-cert-deploy \
    -o IdentitiesOnly=yes \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=/home/hannes/.ssh/known_hosts \
    "hannes@mac2016" "$site"
