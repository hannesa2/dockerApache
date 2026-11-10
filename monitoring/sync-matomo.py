#!/usr/bin/env python3
"""Mirror native Latitude Matomo without changing the production installation."""
import configparser
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import datetime as dt

SOURCE = Path(os.environ.get("MATOMO_SOURCE_DIR", "/srv/www/vhosts/mxdocs/piwik"))
TARGET = os.environ.get("MATOMO_MIRROR_DIR", "/Users/hannes/server/apache/www/mxdocs/piwik")
STATE = Path.home() / ".local/state/matomo-mirror"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
       "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "mac2016"]


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def remote(script, **kwargs):
    return run(SSH + ["/bin/bash -euo pipefail -c " + shlex.quote(
        "export PATH=/usr/local/bin:/usr/bin:/bin; " + script)], **kwargs)


def main():
    if sys.argv[1:] not in ([], ["--apply"]):
        raise SystemExit("Usage: sync-matomo.py [--apply]")
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "sync.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        synchronize("--apply" in sys.argv)


def synchronize(apply):
    native = (SOURCE / "config/config.ini.php").read_text()
    parsed = configparser.ConfigParser(interpolation=None, strict=False)
    parsed.read_string(native)
    database = {k: v.strip('"') for k, v in parsed["database"].items()}
    if database["dbname"] != "piwik_db":
        raise RuntimeError("Unexpected native Matomo database")
    version = re.search(r"const VERSION\s*=\s*'([^']+)'",
                        (SOURCE / "core/Version.php").read_text()).group(1)
    mirror_config = remote(
        "docker exec apache-apache-1 cat /var/www/html/mxdocs/piwik/config/config.ini.php",
        capture_output=True, text=True,
    ).stdout
    mirror_parsed = configparser.ConfigParser(interpolation=None, strict=False)
    mirror_parsed.read_string(mirror_config)
    if mirror_parsed["database"]["dbname"].strip('"') != "matomo_mirror":
        raise RuntimeError("Unexpected mirror database")
    section = re.compile(r"(?ms)^\[database\]\s*\n.*?(?=^\[|\Z)")
    mirror_db = section.search(mirror_config).group()
    updated = section.sub(lambda _: mirror_db, native, count=1)
    updated = re.sub(r"(?m)^\s*trusted_hosts\[\]\s*=.*\n?", "", updated)
    updated = updated.replace("[General]", '[General]\ntrusted_hosts[] = "www16.mxtracks.info"\n'
                              'trusted_hosts[] = "www16.mxtracks.info:8443"\n'
                              'trusted_hosts[] = "www16.mxtracks.info:9443"', 1)
    print(f"Native Matomo {version}; mirror target matomo_mirror on mac2016", flush=True)
    if not apply:
        print("Preflight passed; no database or files changed")
        return
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"/Users/hannes/.local/state/matomo-mirror/pre-sync-{timestamp}"
    remote(f"umask 077; mkdir -p {shlex.quote(backup)} && chmod 700 {shlex.quote(backup)} && "
           f"cp {TARGET}/config/config.ini.php {backup}/config.ini.php && "
           f"chmod 600 {backup}/config.ini.php && "
           "docker exec apache-db-1 sh -c "
           + shlex.quote('MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump -uroot '
                        '--single-transaction --no-tablespaces matomo_mirror')
           + f" > {backup}/database.sql && chmod 600 {backup}/database.sql")
    # Keep the mirror inaccessible throughout replacement, including on failure.
    remote(f"test ! -e {TARGET}/.htaccess && "
           f"printf 'Require all denied\\n' > {TARGET}/.htaccess")
    blocked = remote(
        "curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' --max-time 30 "
        "-H 'Host: www16.mxtracks.info' http://127.0.0.1:9080/piwik/",
        capture_output=True, text=True,
    ).stdout
    if blocked != "403":
        raise RuntimeError("Mirror access block did not take effect; database not changed")
    print("Mirror /piwik access blocked during replacement", flush=True)
    with tempfile.TemporaryDirectory(prefix="matomo-sync-", dir=STATE) as temporary:
        temporary = Path(temporary)
        defaults = temporary / "mysql.cnf"
        def escaped(value):
            return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        defaults.write_text("[client]\n" + "".join(
            f'{key}="{escaped(database[field])}"\n'
            for key, field in (("host", "host"), ("user", "username"), ("password", "password"))
        ))
        defaults.chmod(0o600)
        dump = temporary / "source.sql"
        with dump.open("wb") as stream:
            run(["mysqldump", f"--defaults-extra-file={defaults}",
                 "--single-transaction", "--no-tablespaces", "--hex-blob",
                 database["dbname"]], stdout=stream)
        run(["rsync", "-rltp", "--delete", "--rsync-path=/usr/local/bin/rsync",
             "--exclude=/config/", "--exclude=/tmp/", "--exclude=/.htaccess",
             "-e", "ssh -o BatchMode=yes -o ConnectTimeout=15",
             str(SOURCE) + "/", f"mac2016:{TARGET}/"])
        run(["rsync", "-rltp", "--delete", "--rsync-path=/usr/local/bin/rsync",
             "--exclude=/config.ini.php*",
             "-e", "ssh -o BatchMode=yes -o ConnectTimeout=15",
             str(SOURCE / "config") + "/", f"mac2016:{TARGET}/config/"])
        with dump.open("rb") as stream:
            remote("docker exec -i apache-db-1 sh -c " + shlex.quote(
                'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -e '
                '"DROP DATABASE matomo_mirror; CREATE DATABASE matomo_mirror '
                'CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;" && '
                'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot matomo_mirror'
            ), stdin=stream)
        remote("docker exec -i apache-apache-1 sh -c " + shlex.quote(
            "cat > /var/www/html/mxdocs/piwik/config/config.ini.php && "
            "chown www-data:www-data /var/www/html/mxdocs/piwik/config/config.ini.php && "
            "chmod 600 /var/www/html/mxdocs/piwik/config/config.ini.php && "
            "chown -R www-data:www-data /var/www/html/mxdocs/piwik/tmp"
        ),
               input=updated, text=True)
    remote("docker exec --user www-data apache-apache-1 "
           "php /var/www/html/mxdocs/piwik/console core:update --yes")
    remote("docker exec --user www-data apache-apache-1 "
           "php /var/www/html/mxdocs/piwik/console core:clear-caches")
    remote("docker exec apache-apache-1 php -r " + shlex.quote(
        'require "/var/www/html/mxdocs/piwik/core/Version.php"; '
        f'if (\\Piwik\\Version::VERSION !== "{version}") exit(1);'
    ))
    defaults_hash = hashlib.sha256((SOURCE / "config/global.ini.php").read_bytes()).hexdigest()
    remote("docker exec apache-apache-1 php -r " + shlex.quote(
        'if (hash_file("sha256", "/var/www/html/mxdocs/piwik/config/global.ini.php") '
        f'!== "{defaults_hash}") {{ fwrite(STDERR, "Matomo configuration defaults differ\\n"); exit(1); }}'
    ))
    remote(f"rm {TARGET}/.htaccess")
    remote("curl --noproxy '*' -fsS --max-time 30 "
           "--resolve www16.mxtracks.info:9443:127.0.0.1 "
           "https://www16.mxtracks.info:9443/piwik/ "
           "| python3 -c " + shlex.quote(
               'import sys;s=sys.stdin.read();'
               'assert "<?php" not in s and ("Matomo" in s or "matomo" in s)'
           ))
    print(f"Matomo {version} mirrored successfully; backup: {backup}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}. If replacement started, mirror /piwik remains blocked; "
              "production is unchanged.", file=sys.stderr)
        sys.exit(1)
