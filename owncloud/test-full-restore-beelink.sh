#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C HOME=/root
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 077

fail() { echo "ERROR: $*" >&2; exit 1; }
[[ $# == 1 && "$1" == --test ]] || fail "Usage: sudo bash $0 --test"
[[ "$(id -u)" == 0 && "$(hostname -s)" == beelink ]] ||
    fail "Run as root on beelink."
for cmd in docker findmnt flock python3 rsync ionice nice curl; do
    command -v "$cmd" >/dev/null || fail "Missing command: $cmd"
done

target=/datenRestoreTest
sid=20261126-063812-580
snapshot="/datenBackup/owncloud-root/backintime/beelink/root/1/$sid"
source="$snapshot/backup"
data="$source/daten/beelink-runtime/rootfs/daten/owncloud/data"
volume="$source/daten/beelink-runtime/apps/owncloud/volume"
stage="$source/daten/beelink-backups/backintime-owncloud"
work="$target/full-recovery-$sid"
app=owncloud-full-restore-app
db=owncloud-full-restore-db
app_image=sha256:60f13cb41959d9a4abc44535409d113107efb64b83764efbcc3530c12c4de98d
db_image=sha256:81e77a4833487b8132fe4b00bedd39dc3f74b8641e387f19312a48f7de218c2e
db_started=0
app_started=0

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if (( app_started )); then
        docker stop --time 30 "$app" >/dev/null || status=1
    fi
    if (( db_started )); then
        docker stop --time 30 "$db" >/dev/null || status=1
    fi
    if (( status != 0 )); then
        echo "ERROR: Full recovery incomplete. Inspect $work and test container logs." >&2
    fi
    echo "Artifacts retained at $work; no production container was changed."
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
exec 9>/run/lock/owncloud-restore-test.lock
flock -n 9 || fail "Another restore test is running."
[[ ! -L "$target" &&
   "$(findmnt -rn -M "$target" -t ext4 -o UUID)" == 4f29138d-ca50-4a46-bf9d-d4e37e3b64c9 &&
   "$(findmnt -rn -M "$target" -o SOURCE)" == /dev/mapper/owncloud-restore-test ]] ||
    fail "Expected encrypted test SSD is not mounted."
[[ "$(findmnt -rn -M /datenBackup -t ext4 -o UUID)" == fe91ecb5-c1a4-4e6c-9edf-f3c9c75cffe1 ]] ||
    fail "Expected backup disk is not mounted."
[[ -d "$snapshot" && ! -e "$snapshot/failed" && -f "$data/.ocdata" &&
   -f "$volume/config/config.php" && -d "$volume/apps" && -s "$stage/owncloud.dump" &&
   -s "$snapshot/fileinfo.bz2" ]] ||
    fail "Consistent snapshot is incomplete."
for name in "$app" "$db"; do
    [[ -z "$(docker ps -aq --filter name="^/${name}$")" ]] ||
        fail "$name already exists. Inspect it before retrying; no containers removed."
done
docker image inspect "$app_image" "$db_image" >/dev/null
python3 - "$stage/images.json" "$app_image" "$db_image" <<'PY'
import json
import sys
with open(sys.argv[1]) as stream:
    ids = {image["Id"] for image in json.load(stream)}
if not set(sys.argv[2:]).issubset(ids):
    raise SystemExit("ERROR: Pinned images do not match snapshot image metadata.")
PY

healthy() {
    curl --noproxy '*' -fsS --max-time 10 \
        --resolve www.mxtracks.info:443:127.0.0.1 \
        https://www.mxtracks.info/owncloud/status.php |
        python3 -c 'import json,sys; s=json.load(sys.stdin); assert s.get("installed") and not s.get("maintenance") and not s.get("needsDbUpgrade")'
}
healthy || fail "Production is not healthy before the test."
[[ ! -L "$work" ]] || fail "Test directory is a symlink."
mkdir -p "$work"
chmod 0700 "$work"
[[ ! -e "$work/RESULT.txt" && ! -e "$work/pgdata" && ! -e "$work/config" ]] ||
    fail "Application restoration already attempted; inspect artifacts before retrying."
mkdir -p "$work/volume/files" "$work/volume/apps" "$work/config-original" "$work/control"
chown www-data:root "$work/volume"
chmod 0750 "$work/volume"
chown root:www-data "$work/control"
chmod 0750 "$work/control"
exec > >(tee -a "$work/run.log") 2>&1
echo "$(date -Is) Starting full isolated recovery of $sid."
echo "Copy capped at 16 MiB/s, idle I/O priority; production stays online."
echo "A stopped copy can be resumed by rerunning before application restoration starts."
python3 - "$data" "$volume/apps" "$volume/config" "$work" <<'PY'
import os
from pathlib import Path
import shutil
import sys
sources = [Path(p) for p in sys.argv[1:4]]
destination = Path(sys.argv[4])
remaining = 0
for source, target in zip(sources, [destination / "volume/files",
                                   destination / "volume/apps",
                                   destination / "config-original"]):
    for root, dirs, names in os.walk(source, followlinks=False):
        for name in names:
            path = Path(root) / name
            if path.is_symlink() or not path.is_file():
                continue
            copied = target / path.relative_to(source)
            info = path.stat()
            if not copied.is_file() or copied.stat().st_size != info.st_size:
                remaining += info.st_size
if remaining + 10 * 1024**3 > shutil.disk_usage(destination).free:
    raise SystemExit("ERROR: Insufficient space for full copy plus database/headroom.")
print(f"Uncopied file bytes: {remaining}; reserve: 10 GiB.")
PY
for part in files apps config; do
    case "$part" in
        files) from="$data/"; to="$work/volume/files/" ;;
        apps) from="$volume/apps/"; to="$work/volume/apps/" ;;
        config) from="$volume/config/"; to="$work/config-original/" ;;
    esac
    echo "$(date -Is) Copying $part..."
    ionice -c 3 nice -n 15 rsync -aHAXS --numeric-ids --bwlimit=16384 \
        --info=stats2 "$from" "$to"
done
python3 - "$snapshot/fileinfo.bz2" "$work" <<'PY'
import bz2
import grp
import os
from pathlib import Path
import pwd
import stat
import sys
work = Path(sys.argv[2])
mappings = [
    ("/daten/beelink-runtime/rootfs/daten/owncloud/data", work / "volume/files"),
    ("/daten/beelink-runtime/apps/owncloud/volume/apps", work / "volume/apps"),
    ("/daten/beelink-runtime/apps/owncloud/volume/config", work / "config-original"),
]
directories = []
restored = 0
with bz2.open(sys.argv[1], "rb") as stream:
    for line in stream:
        fields = line.rstrip(b"\n").split(b" ", 3)
        if len(fields) != 4:
            raise SystemExit("ERROR: Malformed Back In Time permissions metadata.")
        mode, user, group, original = fields
        original = os.fsdecode(original)
        for prefix, destination in mappings:
            if original != prefix and not original.startswith(prefix + "/"):
                continue
            path = destination / original[len(prefix):].lstrip("/")
            if path.is_symlink():
                break
            if not path.resolve().is_relative_to(destination.resolve()):
                raise SystemExit("ERROR: Permissions path escapes restoration target.")
            if not path.exists():
                raise SystemExit("ERROR: File from permissions metadata is missing.")
            uid = int(user) if user.isdigit() else pwd.getpwnam(os.fsdecode(user)).pw_uid
            gid = int(group) if group.isdigit() else grp.getgrnam(os.fsdecode(group)).gr_gid
            os.chown(path, uid, gid)
            permissions = stat.S_IMODE(int(mode))
            if path.is_dir():
                directories.append((path, permissions))
            else:
                os.chmod(path, permissions)
            restored += 1
            break
for path, mode in sorted(directories, key=lambda item: len(item[0].parts), reverse=True):
    os.chmod(path, mode)
if restored == 0:
    raise SystemExit("ERROR: No original ownership/permissions restored.")
print(f"Restored original ownership and modes for {restored} paths.")
PY
healthy || fail "Production health check failed after copying."
python3 - "$stage" "$work" <<'PY'
import hashlib
from pathlib import Path
import shutil
import sys
source, target = map(Path, sys.argv[1:])
expected = (source / "owncloud.dump.sha256").read_text().split()[0]
def checksum(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()
if checksum(source / "owncloud.dump") != expected:
    raise SystemExit("ERROR: Snapshot dump checksum mismatch.")
shutil.copy2(source / "owncloud.dump", target / "owncloud.dump")
shutil.copy2(source / "containers.json", target / "control/containers.json")
if checksum(target / "owncloud.dump") != expected:
    raise SystemExit("ERROR: Restored dump checksum mismatch.")
PY
mkdir "$work/config" "$work/pgdata"
mkdir -p "$work/volume/sessions"
chown www-data:root "$work/config" "$work/volume/sessions"
chmod 0750 "$work/config" "$work/volume/sessions"

cat > "$work/control/configure.php" <<'PHP'
<?php
class OC {
    public static $SERVERROOT = '/var/www/owncloud';
}
$containers = json_decode(file_get_contents('/recovery/control/containers.json'), true, 512, JSON_THROW_ON_ERROR);
$found = false;
foreach ($containers as $container) {
    if (($container['Name'] ?? '') !== '/beelink-apps-owncloud-app-1') {
        continue;
    }
    foreach ($container['Config']['Env'] as $entry) {
        if (str_starts_with($entry, 'OWNCLOUD_')) {
            putenv($entry);
        }
    }
    $found = true;
}
if (!$found) {
    throw new RuntimeException('Saved application environment missing');
}
$base = '/recovery/config-original/';
$files = glob($base . '*.config.php');
natsort($files);
array_unshift($files, $base . 'config.php');
$merged = [];
foreach ($files as $file) {
    unset($CONFIG);
    require $file;
    if (!isset($CONFIG) || !is_array($CONFIG)) {
        throw new RuntimeException('Invalid saved configuration');
    }
    $merged = array_merge($merged, $CONFIG);
}
if (!empty($merged['objectstore']) || !empty($merged['objectstore_multibucket'])) {
    throw new RuntimeException('Primary object storage requires a separate recovery plan');
}
foreach (['redis', 'memcache.distributed', 'memcache.locking', 'trusted_proxies',
          'overwritecondaddr', 'mail_smtphost', 'mail_smtpname', 'mail_smtppassword',
          'user_backends', 'session.save_path'] as $key) {
    unset($merged[$key]);
}
$merged = array_merge($merged, [
    'dbtype' => 'pgsql', 'dbhost' => '127.0.0.1', 'dbport' => '5432',
    'dbname' => 'owncloud_recovery', 'dbuser' => 'postgres', 'dbpassword' => '',
    'datadirectory' => '/mnt/data/files',
    'apps_paths' => [
        ['path' => '/var/www/owncloud/apps', 'url' => '/apps', 'writable' => false],
        ['path' => '/var/www/owncloud/custom', 'url' => '/custom', 'writable' => true],
    ],
    'trusted_domains' => ['localhost', '127.0.0.1'],
    'overwritehost' => 'localhost:8080', 'overwriteprotocol' => 'http',
    'overwritewebroot' => '', 'overwrite.cli.url' => 'http://localhost:8080',
    'htaccess.RewriteBase' => '/', 'maintenance' => false,
    'config_is_read_only' => false, 'memcache.local' => '\OC\Memcache\APCu',
    'mail_smtpmode' => 'smtp', 'mail_smtphost' => '127.0.0.1', 'mail_smtpport' => 9,
    'logfile' => '/mnt/data/owncloud-recovery.log',
    'updatechecker' => false, 'has_internet_connection' => false,
]);
if (file_put_contents('/recovery/config/config.php',
    "<?php\n\$CONFIG = " . var_export($merged, true) . ";\n") === false) {
    throw new RuntimeException('Cannot write isolated configuration');
}
PHP
docker run --rm --network none --memory 256m --cpus 0.5 \
    --entrypoint php --mount "type=bind,src=$work,dst=/recovery" \
    "$app_image" /recovery/control/configure.php
chown -R www-data:root "$work/config"
chmod 0640 "$work/config/config.php"

echo "$(date -Is) Restoring isolated PostgreSQL..."
docker run -d --name "$db" --network none --restart no --memory 768m --cpus 0.5 \
    --pids-limit 256 --label purpose=owncloud-full-restore \
    --mount "type=bind,src=$work/pgdata,dst=/var/lib/postgresql/data" \
    -e POSTGRES_HOST_AUTH_METHOD=trust "$db_image" >/dev/null
db_started=1
ready=0
for attempt in {1..90}; do
    if [[ "$(docker exec "$db" cat /proc/1/comm)" == postgres ]] &&
        docker exec "$db" pg_isready -U postgres >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
(( ready )) || fail "Test database did not become ready."
docker exec "$db" createdb -U postgres owncloud_recovery
docker exec -i "$db" pg_restore -U postgres -d owncloud_recovery \
    --exit-on-error --single-transaction --no-owner --no-privileges < "$work/owncloud.dump"

echo "$(date -Is) Starting isolated ownCloud (no entrypoint hooks or cron)..."
docker run -d --name "$app" --network "container:$db" --restart no \
    --memory 1g --cpus 0.5 --pids-limit 256 --label purpose=owncloud-full-restore \
    --mount "type=bind,src=$work/volume,dst=/mnt/data" \
    --mount "type=bind,src=$work/config,dst=/var/www/owncloud/config" \
    --mount "type=bind,src=$work/volume/apps,dst=/var/www/owncloud/custom" \
    --mount "type=bind,src=$work/control,dst=/recovery-control,readonly" \
    -e APACHE_DOCUMENT_ROOT=/var/www/owncloud \
    --entrypoint /bin/bash "$app_image" -c \
    'set -eo pipefail; source /etc/entrypoint.d/99-apache.sh; mkdir -p "$APACHE_RUN_DIR" "$APACHE_LOCK_DIR"; exec /usr/sbin/apache2 -D FOREGROUND' >/dev/null
app_started=1
ready=0
for attempt in {1..60}; do
    if docker exec "$app" curl -fsS --max-time 10 http://localhost:8080/status.php \
        > "$work/status.json"; then
        ready=1
        break
    fi
    sleep 2
done
(( ready )) || fail "Restored ownCloud HTTP endpoint did not become ready."
python3 - "$work/status.json" <<'PY'
import json
import sys
with open(sys.argv[1]) as stream:
    status = json.load(stream)
if not (status.get("installed") and not status.get("maintenance")
        and not status.get("needsDbUpgrade") and status.get("versionstring") == "11.1.0"):
    raise SystemExit("ERROR: Restored ownCloud status is not healthy/matching.")
PY
docker exec --user www-data "$app" php /var/www/owncloud/occ status
docker exec --user www-data "$app" php /var/www/owncloud/occ background:webcron
docker exec --user www-data "$app" php /var/www/owncloud/occ app:list \
    > "$work/restored-apps.txt"
docker exec "$app" curl -fsS --max-time 30 http://localhost:8080/index.php/login \
    > "$work/login.html"
grep -q 'name="password"' "$work/login.html" || fail "Login form missing."

cat > "$work/control/verify-files.php" <<'PHP'
<?php
define('OC_CONSOLE', 1);
require '/var/www/owncloud/lib/base.php';
$verified = 0;
foreach (new DirectoryIterator('/mnt/data/files') as $user) {
    if ($user->isDot() || !$user->isDir() || $user->isLink()) {
        continue;
    }
    $uid = $user->getFilename();
    $files = $user->getPathname() . '/files';
    if (!is_dir($files) || is_link($files) ||
        !\OC::$server->getUserManager()->userExists($uid)) {
        continue;
    }
    $sample = null;
    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($files, FilesystemIterator::SKIP_DOTS));
    foreach ($iterator as $file) {
        if ($file->isFile() && !$file->isLink() && $file->getSize() <= 16 * 1024 * 1024) {
            $sample = $file->getPathname();
            break;
        }
    }
    if ($sample === null) {
        continue;
    }
    \OC_Util::tearDownFS();
    \OC_Util::setupFS($uid);
    $view = new \OC\Files\View('/' . $uid . '/files');
    $relative = substr($sample, strlen($files));
    $stream = $view->fopen($relative, 'rb');
    if (!is_resource($stream)) {
        throw new RuntimeException('Cannot read restored file through ownCloud');
    }
    $hash = hash_init('sha256');
    hash_update_stream($hash, $stream);
    fclose($stream);
    if (hash_final($hash) !== hash_file('sha256', $sample)) {
        throw new RuntimeException('ownCloud file read differs from restored file');
    }
    if (++$verified === 4) {
        break;
    }
}
if ($verified === 0) {
    throw new RuntimeException('No existing user files verified through ownCloud');
}
echo "Existing file reads through ownCloud verified for $verified users.\n";
PHP
chown root:www-data "$work/control/verify-files.php"
chmod 0640 "$work/control/verify-files.php"
docker exec --user www-data "$app" php /recovery-control/verify-files.php

echo "$(date -Is) Testing authenticated WebDAV with a new test-only user..."
python3 - "$work/control/webdav.json" <<'PY'
import json
import secrets
import sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
    "user": "restore-test-" + secrets.token_hex(6),
    "password": secrets.token_urlsafe(32),
    "content": secrets.token_hex(128),
}))
PY
chown www-data:root "$work/control/webdav.json"
chmod 0640 "$work/control/webdav.json"
cat > "$work/control/add-user.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
OC_PASS="$(php -r '$v=json_decode(file_get_contents("/recovery-control/webdav.json"),true,512,JSON_THROW_ON_ERROR); echo $v["password"];')"
export OC_PASS
user="$(php -r '$v=json_decode(file_get_contents("/recovery-control/webdav.json"),true,512,JSON_THROW_ON_ERROR); echo $v["user"];')"
exec php /var/www/owncloud/occ user:add --password-from-env "$user"
SH
chown root:www-data "$work/control/add-user.sh"
chmod 0640 "$work/control/add-user.sh"
docker exec --user www-data "$app" bash /recovery-control/add-user.sh
cat > "$work/control/webdav.php" <<'PHP'
<?php
$test = json_decode(file_get_contents('/recovery-control/webdav.json'), true, 512, JSON_THROW_ON_ERROR);
$url = 'http://localhost:8080/remote.php/dav/files/' .
    rawurlencode($test['user']) . '/restore-verification.txt';
foreach (['PUT', 'GET', 'DELETE'] as $method) {
    $curl = curl_init($url);
    curl_setopt_array($curl, [
        CURLOPT_USERPWD => $test['user'] . ':' . $test['password'],
        CURLOPT_HTTPAUTH => CURLAUTH_BASIC, CURLOPT_CUSTOMREQUEST => $method,
        CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 60,
        CURLOPT_FOLLOWLOCATION => false,
    ]);
    if ($method === 'PUT') {
        curl_setopt($curl, CURLOPT_POSTFIELDS, $test['content']);
    }
    $body = curl_exec($curl);
    $status = curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
    if ($body === false || !in_array($status, $method === 'GET' ? [200] :
        ($method === 'PUT' ? [201, 204] : [204]), true)) {
        throw new RuntimeException("WebDAV $method failed with HTTP $status");
    }
    if ($method === 'GET' && !hash_equals($test['content'], $body)) {
        throw new RuntimeException('WebDAV download content mismatch');
    }
    curl_close($curl);
}
echo "Authenticated WebDAV upload, download and deletion passed.\n";
PHP
chown root:www-data "$work/control/webdav.php"
chmod 0640 "$work/control/webdav.php"
docker exec --user www-data "$app" php /recovery-control/webdav.php
echo "$(date -Is) Recovery verified; stopping both test containers."
docker stop --time 30 "$app" >/dev/null
app_started=0
docker stop --time 30 "$db" >/dev/null
db_started=0
healthy || fail "Production health check failed after recovery."
printf 'PASS: full data copied; PostgreSQL and ownCloud 11.1.0 recovered in isolation; login form, existing file reads and authenticated WebDAV verified.\n' |
    tee "$work/RESULT.txt"
