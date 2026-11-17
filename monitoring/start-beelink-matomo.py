#!/usr/bin/env python3
"""Prepare and check production Matomo in an isolated Docker test instance."""

import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess


SPEC = importlib.util.spec_from_file_location(
    "beelink_apps", Path(__file__).with_name("start-beelink-apps.py"))
apps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apps)
WORK = apps.RUNTIME / "matomo"
COMPOSE = ["docker", "compose", "-p", "beelink-matomo", "-f", str(WORK / "compose.json")]


def patch_config(text, username=None, password=None):
    database = re.search(r"(?ms)^\[database\]\s*\n.*?(?=^\[|\Z)", text)
    if not database or not re.search(r'(?m)^dbname\s*=\s*"piwik_db"\s*$', database.group()):
        raise RuntimeError("Expected production Matomo piwik_db configuration")
    updated, count = re.subn(
        r'(?m)^host\s*=.*$', 'host = "beelink-databases-native-mysql-1"', database.group())
    if count != 1:
        raise RuntimeError("Expected one Matomo database host")
    if username is not None:
        for key, value in (("username", username), ("password", password)):
            if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_]+", value):
                raise RuntimeError("Unexpected generated Matomo test credential")
            updated, count = re.subn(rf"(?m)^{key}\s*=.*$", f'{key} = "{value}"', updated)
            if count != 1:
                raise RuntimeError("Expected one Matomo database " + key)
    text = text[:database.start()] + updated + text[database.end():]
    text = re.sub(r"(?m)^\s*trusted_hosts\[\]\s*=.*\n?", "", text)
    if len(re.findall(r"(?m)^\[General\]\s*$", text)) != 1:
        raise RuntimeError("Expected one Matomo General section")
    return re.sub(r"(?m)^\[General\]\s*$",
                  '[General]\ntrusted_hosts[] = "localhost"\ntrusted_hosts[] = "127.0.0.1"',
                  text, count=1)


def compose_config(image):
    return {
        "name": "beelink-matomo",
        "services": {"matomo": {
            "image": image, "restart": "no", "networks": ["isolated"],
            "volumes": [
                apps.mount(WORK / "www", "/var/www/html"),
                apps.mount(WORK / "vhosts", "/etc/apache2/sites-enabled", True),
                apps.mount(WORK / "logs", "/var/log/apache2"),
            ],
        }},
        "networks": {"isolated": {"external": True, "name": "beelink-databases_isolated"}},
    }


def prepare(image):
    config = compose_config(image)
    path = WORK / "compose.json"
    if path.exists():
        if json.loads(path.read_text()) != config or not (WORK / "prepared").is_file():
            raise RuntimeError("Existing Matomo test configuration differs or is incomplete")
        return
    for name in ("www", "vhosts", "logs"):
        if (WORK / name).exists():
            raise RuntimeError("Unmanaged Matomo test directory exists: " + name)
        (WORK / name).mkdir()
    source = apps.ROOTFS / "srv/www/vhosts/mxdocs/piwik"
    if not (source / "config/config.ini.php").is_file():
        raise RuntimeError("Production Matomo seed is missing")
    apps.run("rsync", "-aHAX", "--numeric-ids", "--exclude=/tmp/",
             str(source) + "/", str(WORK / "www/piwik") + "/")
    target = WORK / "www/piwik"
    configuration = target / "config/config.ini.php"
    username = "matomo_test_" + secrets.token_hex(4)
    password = secrets.token_hex(32)
    configuration.write_text(patch_config(configuration.read_text(), username, password))
    configuration.chmod(0o600)
    os.chown(configuration, 33, 33)
    (target / "tmp").mkdir(exist_ok=True)
    os.chown(target / "tmp", 33, 33)
    os.chmod(target / "tmp", 0o750)
    (WORK / "vhosts/matomo.conf").write_text("""ServerName localhost
<VirtualHost *:80>
    ServerName localhost
    DocumentRoot /var/www/html
    <Directory /var/www/html>
        Options -Indexes +FollowSymLinks
        AllowOverride All
        Require all granted
    </Directory>
    ErrorLog /var/log/apache2/matomo-error.log
    CustomLog /var/log/apache2/matomo-access.log combined
</VirtualHost>
""")
    # Native piwik@localhost cannot authenticate from another container.
    # Add a new schema-scoped test account without altering production users.
    apps.run("docker", "exec", "-i", "beelink-databases-native-mysql-1", "mysql",
             "--defaults-file=/run/restore-client.cnf", "--protocol=socket",
             input=f"CREATE USER '{username}'@'%' IDENTIFIED BY '{password}';\n"
             f"GRANT ALL PRIVILEGES ON `piwik_db`.* TO '{username}'@'%';\n", text=True)
    apps.write_json(path, config)
    (WORK / "prepared").touch()


def repair_permissions(root):
    if root.is_symlink() or not (root / "piwik/config/config.ini.php").is_file():
        raise RuntimeError("Expected the private copied Matomo document root")
    for directory, directories, files in os.walk(root, followlinks=False):
        os.chown(directory, 33, 33)
        os.chmod(directory, 0o750)
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                os.chown(path, 33, 33)
                os.chmod(path, 0o640)
    os.chmod(root / "piwik/config/config.ini.php", 0o600)


def verify():
    apps.run(*COMPOSE, "up", "-d")
    apps.COMPOSE = COMPOSE
    apps.run(*COMPOSE, "exec", "-T", "matomo", "apache2ctl", "configtest")
    php = r"""
$config = parse_ini_file('/var/www/html/piwik/config/config.ini.php', true);
$d = $config['database'];
if ($d['host'] !== 'beelink-databases-native-mysql-1' || $d['dbname'] !== 'piwik_db') {
    throw new Exception('Expected isolated Matomo database');
}
if (!preg_match('/^[a-zA-Z0-9_]+$/D', $d['tables_prefix'])) {
    throw new Exception('Unexpected Matomo table prefix');
}
$db = new PDO('mysql:host='.$d['host'].';dbname='.$d['dbname'], $d['username'], $d['password'],
    [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
$count = $db->query('SELECT count(*) FROM `'.$d['tables_prefix'].'site`')->fetchColumn();
if ($count < 1) throw new Exception('Expected restored Matomo sites');
echo "Matomo application credentials and restored sites: OK\n";
"""
    apps.run(*COMPOSE, "exec", "-T", "--user", "www-data", "matomo", "php", "-r", php)
    apps.run(*COMPOSE, "exec", "-T", "--user", "www-data", "matomo", "php",
             "/var/www/html/piwik/console", "--version")
    apps.wait_http("matomo", ["curl", "--fail", "--silent", "--max-time", "10",
                             "http://127.0.0.1/piwik/index.php"])
    apps.write_json(WORK / "matomo-report.json", {
        "database": "Application credentials and nonempty restored sites checked",
        "console": "Version command completed",
        "http": "index.php returned a successful HTTP response",
        "production_ready": False,
    })
    print("Isolated Matomo checks complete. Production, mirror and schedules unchanged.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-isolated", action="store_true", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    os.listdir("/daten")
    if apps.capture("findmnt", "-rn", "-M", "/daten", "-t", "ext4", "-o", "UUID") != \
            "c0c42461-ae4c-4edb-a993-bba94a05ed6c":
        raise RuntimeError("Expected encrypted T7 is not mounted")
    if (apps.RUNTIME.is_symlink() or apps.RUNTIME.stat().st_uid != 0
            or apps.RUNTIME.stat().st_mode & 0o077
            or not (apps.RUNTIME / "java-web/java-web-report.json").is_file()):
        raise RuntimeError("Complete the private Java/web staging first")
    network = json.loads(apps.capture(
        "docker", "network", "inspect", "beelink-databases_isolated"))[0]
    if not network["Internal"]:
        raise RuntimeError("Matomo must remain on the internal test network")
    if shutil.disk_usage(apps.RUNTIME).free < 20 * 1024**3:
        raise RuntimeError("At least 20 GiB free space is required")
    if WORK.is_symlink():
        raise RuntimeError("Matomo test root must not be a symlink")
    WORK.mkdir(mode=0o700, exist_ok=True)
    with (WORK / "start.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        image = apps.capture("docker", "image", "inspect", "beelink-source-apache:20261116",
                             "--format", "{{.Id}}")
        prepare(image)
        try:
            repair_permissions(WORK / "www")
            verify()
        except BaseException:
            print("ERROR: Matomo staging failed; stopping only the Matomo test container.", flush=True)
            apps.run(*COMPOSE, "stop")
            raise


if __name__ == "__main__":
    main()
