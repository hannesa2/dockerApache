#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C
umask 077

(( EUID == 0 )) || { echo "Run with sudo on Latitude." >&2; exit 1; }
systemctl is-active --quiet php8.4-fpm
test -S /run/php/php8.4-fpm.sock
test -f /etc/apache2/conf-enabled/latitude-php84-mxdocs.conf
apache2ctl -t

# This legacy apache2.conf loads conf.d/, not conf-enabled/*.conf.
install -d -m 0755 /etc/apache2/conf.d
include=/etc/apache2/conf.d/latitude-php84-mxdocs.conf
if [[ -e "$include" ]]; then
    grep -Fxq 'Include /etc/apache2/conf-enabled/latitude-php84-mxdocs.conf' "$include" ||
        { echo "ERROR: Unexpected existing include file; inspect it." >&2; exit 1; }
else
    printf '%s\n' 'Include /etc/apache2/conf-enabled/latitude-php84-mxdocs.conf' > "$include"
    chmod 0644 "$include"
fi
apache2ctl -t
systemctl reload apache2

probe="$(mktemp /srv/www/vhosts/mxdocs/php-handler-check-XXXXXXXX.php)"
trap 'rm -f -- "$probe"' EXIT
token="$(basename "$probe" .php)-executed"
printf '<?php header("Content-Type: text/plain"); echo "%s";\n' "$token" > "$probe"
chmod 0644 "$probe"
result="$(curl --noproxy '*' --fail --silent --show-error --max-time 15 \
    --resolve www.mxtracks.info:443:127.0.0.1 \
    "https://www.mxtracks.info/$(basename "$probe")")"
[[ "$result" == "$token" ]] || {
    echo "ERROR: PHP execution verification failed; do not treat HTTP 200 as success." >&2
    exit 1
}
python3 - <<'PY'
import subprocess
for path in ("/piwik/", "/piwik/index.php"):
    response = subprocess.run(
        ["curl", "--noproxy", "*", "--fail", "--silent", "--show-error",
         "--max-time", "20", "--resolve", "www.mxtracks.info:443:127.0.0.1",
         "https://www.mxtracks.info" + path],
        capture_output=True, check=True,
    ).stdout
    if b"<?php" in response or not response.strip():
        raise SystemExit("ERROR: Matomo still exposes PHP source or returns an empty response")
    print(f"{path}: nonempty response, no PHP source")
PY
echo "PHP 8.4 FPM execution verified; Matomo source exposure corrected."
