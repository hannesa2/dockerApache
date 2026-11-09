#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C
umask 077

MODE="${1:---preflight}"
[[ "$MODE" == "--preflight" || "$MODE" == "--apply" ]] ||
    { echo "Usage: $0 [--preflight|--apply]" >&2; exit 2; }
(( $# <= 1 )) || { echo "Usage: $0 [--preflight|--apply]" >&2; exit 2; }
(( EUID == 0 )) || { echo "Run with sudo on latitude." >&2; exit 1; }

HANDLER_NAME="latitude-php84-mxdocs"
HANDLER_AVAILABLE="/etc/apache2/conf-available/$HANDLER_NAME.conf"
HANDLER_ENABLED="/etc/apache2/conf-enabled/$HANDLER_NAME.conf"
STATE_ROOT="/root/.local/state/remove-host-php7"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
BACKUP="$STATE_ROOT/$TIMESTAMP"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }

for command_name in apache2ctl a2query a2enconf a2disconf a2enmod a2dismod \
    systemctl apt-get dpkg-query update-alternatives curl; do
    command -v "$command_name" >/dev/null || fail "Missing command: $command_name"
done

systemctl is-active --quiet php8.4-fpm || fail "PHP 8.4 FPM is not active."
[[ -S /run/php/php8.4-fpm.sock ]] || fail "PHP 8.4 FPM socket is missing."
a2query -m proxy_fcgi >/dev/null || fail "Apache proxy_fcgi module is disabled."
a2query -c php8.4-fpm >/dev/null || fail "Apache PHP 8.4 FPM config is disabled."
[[ ! -e "$HANDLER_AVAILABLE" && ! -e "$HANDLER_ENABLED" ]] ||
    fail "$HANDLER_NAME already exists; inspect before continuing."

if ! apache2ctl -t; then
    fail "Apache configuration is invalid. Repair it before changing PHP modules."
fi

declare -a url_labels=(
    "Matomo|www.mxtracks.info|/piwik/index.php|200"
    "mxdocs WordPress|www.mxtracks.info|/wordpress/wp-login.php|any"
    "development WordPress|dev.mxtracks.info|/wordpress/wp-login.php|200"
)
declare -a before_codes=()
for index in "${!url_labels[@]}"; do
    IFS='|' read -r label host path expected <<< "${url_labels[$index]}"
    code="$(curl --noproxy '*' --silent --insecure --output /dev/null --write-out '%{http_code}' \
        --connect-timeout 3 --max-time 20 --resolve "$host:443:127.0.0.1" \
        "https://$host$path")" || code="000"
    [[ "$code" != "000" ]] ||
        fail "$label baseline is unavailable (HTTP $code). Review before PHP migration."
    [[ "$expected" == "any" || "$code" == "$expected" ]] ||
        fail "$label baseline changed: expected HTTP $expected, got $code. Review before PHP migration."
    before_codes+=("$code")
    log "$label baseline HTTP $code."
    if [[ "$label" == "mxdocs WordPress" && "$code" == "500" ]]; then
        log "mxdocs WordPress already returns HTTP 500; recovery to HTTP 200 is allowed."
    fi
done

mapfile -t php7_packages < <(
    dpkg-query -W -f='${db:Status-Abbrev} ${binary:Package}\n' \
        'php7.*' 'libapache2-mod-php7.*' 2>/dev/null |
        awk '$1 == "ii" {print $2}'
)
((${#php7_packages[@]} > 0)) || fail "No installed host PHP 7.x packages found."

shopt -s nullglob
php7_module_files=(/etc/apache2/mods-enabled/php7.*.load)
shopt -u nullglob
php7_modules=()
for module_file in "${php7_module_files[@]}"; do
    php7_modules+=("$(basename "$module_file" .load)")
done

if [[ "$MODE" == "--preflight" ]]; then
    log "Would route /srv/www/vhosts/mxdocs PHP through PHP 8.4 FPM, disable mod_php7.4, verify unchanged site responses, then purge ${#php7_packages[@]} PHP 7.4 packages."
    log "No files, modules, services, or packages changed."
    exit 0
fi

mkdir -p -m 0700 "$BACKUP"
dpkg-query -W -f='${db:Status-Abbrev} ${binary:Package} ${Version}\n' \
    'php7.*' 'libapache2-mod-php7.*' > "$BACKUP/php7-packages.txt" 2>/dev/null || true
update-alternatives --query php > "$BACKUP/php-alternative.txt"
for module in "${php7_modules[@]}"; do
    cp -a "/etc/apache2/mods-enabled/$module.conf" "$BACKUP/" 2>/dev/null || true
    cp -a "/etc/apache2/mods-enabled/$module.load" "$BACKUP/" 2>/dev/null || true
done

rollback_apache() {
    a2disconf "$HANDLER_NAME" >/dev/null 2>&1 || true
    if [[ -f /etc/apache2/conf.d/latitude-php84-mxdocs.conf ]] &&
        grep -Fxq 'Include /etc/apache2/conf-enabled/latitude-php84-mxdocs.conf' \
            /etc/apache2/conf.d/latitude-php84-mxdocs.conf; then
        rm -f -- /etc/apache2/conf.d/latitude-php84-mxdocs.conf
    fi
    rm -f -- "$HANDLER_AVAILABLE"
    for module in "${php7_modules[@]}"; do
        a2enmod "$module" >/dev/null 2>&1 || true
    done
    if apache2ctl -t && systemctl reload apache2; then
        log "Apache PHP handler change rolled back."
    else
        log "ERROR: Apache rollback could not reload; restore using $BACKUP." >&2
    fi
}

cat > "$HANDLER_AVAILABLE" <<'APACHE'
<Directory /srv/www/vhosts/mxdocs>
    <FilesMatch "\.ph(?:ar|p|tml)$">
        SetHandler "proxy:unix:/run/php/php8.4-fpm.sock|fcgi://localhost"
    </FilesMatch>
</Directory>
APACHE
chmod 0644 "$HANDLER_AVAILABLE"
a2enconf "$HANDLER_NAME" >/dev/null
for module in "${php7_modules[@]}"; do
    a2dismod "$module" >/dev/null
done
if ! apache2ctl -t; then
    rollback_apache
    fail "Apache rejected the PHP 8.4 handler change."
fi
if ! systemctl reload apache2; then
    rollback_apache
    fail "Apache reload failed."
fi

if ! bash "$(dirname "${BASH_SOURCE[0]}")/fix-latitude-php-handler.sh"; then
    fail "PHP execution was not verified. Packages were not purged; inspect the handler immediately."
fi

for index in "${!url_labels[@]}"; do
    IFS='|' read -r label host path expected <<< "${url_labels[$index]}"
    code="$(curl --noproxy '*' --silent --insecure --output /dev/null --write-out '%{http_code}' \
        --connect-timeout 3 --max-time 20 --resolve "$host:443:127.0.0.1" \
        "https://$host$path")" || code="000"
    if [[ "$code" != "${before_codes[$index]}" &&
        ! ( "$label" == "mxdocs WordPress" &&
            "${before_codes[$index]}" == "500" && "$code" == "200" ) ]]; then
        rollback_apache
        fail "$label changed from HTTP ${before_codes[$index]} to $code; PHP packages were not purged."
    fi
    log "$label after handler switch: HTTP $code."
done

update-alternatives --set php /usr/bin/php8.4
if [[ -x /usr/bin/phar8.4 ]]; then
    update-alternatives --set phar /usr/bin/phar8.4
fi
if [[ -x /usr/bin/phar.phar8.4 ]]; then
    update-alternatives --set phar.phar /usr/bin/phar.phar8.4
fi

log "Purging installed PHP 7.4 packages only; no autoremove."
apt-get purge -y "${php7_packages[@]}"
if dpkg-query -W -f='${db:Status-Abbrev} ${binary:Package}\n' \
    'php7.*' 'libapache2-mod-php7.*' 2>/dev/null |
    awk '$1 == "ii" {found=1} END {exit found ? 0 : 1}'; then
    fail "Some PHP 7.4 packages remain installed."
fi
[[ "$(php -r 'echo PHP_MAJOR_VERSION, ".", PHP_MINOR_VERSION;')" == "8.4" ]] ||
    fail "Host PHP CLI did not switch to PHP 8.4."
! apache2ctl -M 2>/dev/null | grep -q 'php7_module' ||
    fail "Apache still has mod_php7 loaded."
apache2ctl -t
log "Host PHP 7.x packages removed. PHP CLI and web FPM use PHP 8.4."
log "PHP 8.4 migration metadata: $BACKUP"
log "The old native ownCloud rollback tree remains on disk but no longer runs; production is Dockerized."
