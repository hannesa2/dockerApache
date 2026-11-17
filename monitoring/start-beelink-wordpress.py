#!/usr/bin/env python3
"""Prepare the original WordPress sites in isolated Docker, without a cutover."""

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


SPEC = importlib.util.spec_from_file_location(
    "beelink_apps", Path(__file__).with_name("start-beelink-apps.py"))
apps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apps)
WORK = apps.RUNTIME / "wordpress"
DATABASE = "beelink-databases-native-mysql-1"
COMPOSE = ["docker", "compose", "-p", "beelink-wordpress",
           "-f", str(WORK / "compose.json")]
SITES = (
    ("dev.mxtracks.info", "dev.mxtracks", "wordpress"),
    ("www.mxtracks.info", "mxdocs", "wordpress_mxtracks"),
)
WWW_VERSION = "6.9.11"


def patch_config(text, schema, username, password):
    text = text.replace("\r\n", "\n")
    if text.startswith("<?php\nd/**"):
        text = text.replace("<?php\nd/**", "<?php\n/**", 1)
    if schema not in {site[2] for site in SITES} or not all(
            re.fullmatch(r"[a-zA-Z0-9_]+", value) for value in (username, password)):
        raise RuntimeError("Unexpected WordPress test credentials or schema")
    literal = r"""(?:'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")"""
    name = re.findall(
        rf"""define\s*\(\s*['"]DB_NAME['"]\s*,\s*({literal})\s*\)\s*;""", text)
    if len(name) != 1 or name[0][1:-1] != schema:
        raise RuntimeError("Expected the original WordPress database: " + schema)
    for key, value in (("DB_HOST", DATABASE), ("DB_USER", username), ("DB_PASSWORD", password)):
        text, count = re.subn(
            rf"""define\s*\(\s*['"]{key}['"]\s*,\s*{literal}\s*\)\s*;""",
            lambda match: f"define('{key}', '{value}');", text)
        if count != 1:
            raise RuntimeError("Expected exactly one literal WordPress " + key)
    settings = {
        "DISABLE_WP_CRON": "true", "AUTOMATIC_UPDATER_DISABLED": "true",
        "WP_HTTP_BLOCK_EXTERNAL": "true", "WP_ENVIRONMENT_TYPE": "'staging'",
    }
    domain = next(site[0] for site in SITES if site[2] == schema)
    for key in ("WP_HOME", "WP_SITEURL"):
        values = re.findall(
            rf"""define\s*\(\s*['"]{key}['"]\s*,\s*({literal})\s*\)\s*;""", text)
        if any(value[1:-1] not in {
                f"http://{domain}/wordpress", f"https://{domain}/wordpress"} for value in values):
            raise RuntimeError("Unexpected original WordPress URL: " + key)
        settings[key] = f"'https://{domain}/wordpress'"
    for key in settings:
        text, count = re.subn(
            rf"""define\s*\(\s*['"]{key}['"]\s*,\s*(?:{literal}|true|false)\s*\)\s*;""",
            "", text, flags=re.IGNORECASE)
        if count > 1 or re.search(rf"""define\s*\(\s*['"]{key}['"]""", text):
            raise RuntimeError("Unexpected WordPress staging constant: " + key)
    if text.count("<?php") != 1:
        raise RuntimeError("Expected one PHP opening tag")
    additions = "\n".join(f"define('{key}', {value});" for key, value in settings.items())
    # Only the internal test vhost supplies this header; no public port is bound.
    return text.replace("<?php", "<?php\n" + additions + """
if (($_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '') === 'https') {
    $_SERVER['HTTPS'] = 'on';
}
""", 1)


def compose_config(image):
    return {
        "name": "beelink-wordpress",
        "services": {"apache": {
            "image": image, "restart": "no", "networks": ["isolated"],
            "volumes": [
                apps.mount(WORK / "www", "/var/www/html"),
                apps.mount(WORK / "vhosts", "/etc/apache2/sites-enabled", True),
                apps.mount(WORK / "logs", "/var/log/apache2"),
            ],
        }},
        "networks": {"isolated": {"external": True, "name": "beelink-databases_isolated"}},
    }


def vhost(domain, subdir):
    if (domain, subdir) not in {(site[0], site[1]) for site in SITES}:
        raise RuntimeError("Unexpected original WordPress vhost")
    return f"""<VirtualHost *:80>
    ServerName {domain}
    DocumentRoot /var/www/html/{subdir}
    DirectoryIndex index.php index.html
    <Directory /var/www/html/{subdir}>
        Options -Indexes +FollowSymLinks
        AllowOverride All
        Require all granted
    </Directory>
    <DirectoryMatch "/[.]git(?:/|$)">
        Require all denied
    </DirectoryMatch>
    RequestHeader set X-Forwarded-Proto "https"
    ErrorLog /var/log/apache2/{domain}-error.log
    CustomLog /var/log/apache2/{domain}-access.log combined
</VirtualHost>
"""


def relocate_links(root, original):
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(directory) / name
            if not path.is_symlink():
                continue
            target = Path(os.readlink(path))
            if target.is_absolute():
                if not target.is_relative_to(original):
                    raise RuntimeError("External WordPress symlink: " + str(path))
                target = root / target.relative_to(original)
            else:
                target = path.parent / target
            if not target.resolve().is_relative_to(root.resolve()) or not target.is_file():
                raise RuntimeError("Missing or escaping WordPress symlink: " + str(path))
            path.unlink()
            path.symlink_to(os.path.relpath(target, path.parent))


def repair_permissions(root):
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("Expected copied WordPress document root")
    for directory, directories, files in os.walk(root, followlinks=False):
        os.chown(directory, 33, 33)
        os.chmod(directory, 0o750)
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                os.chown(path, 33, 33)
                path.chmod(0o600 if name == "wp-config.php" else 0o640)


def prepare(image):
    config = compose_config(image)
    path = WORK / "compose.json"
    if path.exists():
        if json.loads(path.read_text()) != config or not (WORK / "prepared").is_file():
            raise RuntimeError("Existing original WordPress configuration differs or is incomplete")
        return
    for name in ("www", "vhosts", "logs"):
        if (WORK / name).exists():
            raise RuntimeError("Unmanaged WordPress test directory exists: " + name)
        (WORK / name).mkdir()
    (WORK / "vhosts/000-default.conf").write_text("""ServerName localhost
<VirtualHost *:80>
    ServerName localhost
    <Location />
        Require all denied
    </Location>
</VirtualHost>
""")
    for domain, subdir, schema in SITES:
        original = Path("/srv/www/vhosts") / subdir
        source = apps.ROOTFS / original.relative_to("/")
        if not (source / "wordpress/wp-config.php").is_file():
            raise RuntimeError("Missing original WordPress seed: " + domain)
        target = WORK / "www" / subdir
        apps.run("rsync", "-aHAX", "--numeric-ids", "--exclude=.git",
                 "--exclude=/piwik/", str(source) + "/", str(target) + "/")
        relocate_links(target, original)
        configuration = target / "wordpress/wp-config.php"
        if configuration.read_text().startswith("<?php\nd/**"):
            print("Removing stray PHP token from copied wp-config.php: " + domain, flush=True)
        username = "wp_test_" + secrets.token_hex(4)
        password = secrets.token_hex(32)
        configuration.write_text(patch_config(configuration.read_text(), schema, username, password))
        configuration.chmod(0o600)
        apps.run("docker", "exec", "-i", DATABASE, "mysql",
                 "--defaults-file=/run/restore-client.cnf", "--protocol=socket",
                 input=f"CREATE USER '{username}'@'%' IDENTIFIED BY '{password}';\n"
                 f"GRANT ALL PRIVILEGES ON `{schema}`.* TO '{username}'@'%';\n", text=True)
        (WORK / "vhosts" / (domain + ".conf")).write_text(vhost(domain, subdir))
    repair_permissions(WORK / "www")
    apps.write_json(path, config)
    (WORK / "prepared").touch()


def core_version(path):
    versions = re.findall(r"(?m)^\$wp_version\s*=\s*'([^']+)';",
                          (path / "wp-includes/version.php").read_text())
    if len(versions) != 1:
        raise RuntimeError("Expected one WordPress core version")
    return versions[0]


def upgrade_www_core(image):
    target = WORK / "www/mxdocs/wordpress"
    version = core_version(target)
    if version == WWW_VERSION:
        return
    if version != "5.0.25":
        raise RuntimeError("Unexpected original www core version; inspect before upgrading")
    staging = WORK / ("core-" + WWW_VERSION)
    if staging.is_symlink():
        raise RuntimeError("Core download directory must not be a symlink")
    staging.mkdir(mode=0o700, exist_ok=True)
    # This disposable downloader has internet access, but no application files,
    # credentials or database network. The application stays internal-only.
    downloader = [
        "docker", "run", "--rm", "--network", "bridge", "--entrypoint", "wp",
        "--mount", f"type=bind,source={staging},target=/download",
        image, "--allow-root", "--path=/download",
    ]
    apps.run(*downloader, "core", "download", "--version=" + WWW_VERSION,
             "--locale=en_US", "--skip-content", "--force")
    apps.run(*downloader, "core", "verify-checksums", "--version=" + WWW_VERSION,
             "--locale=en_US")
    if core_version(staging) != WWW_VERSION:
        raise RuntimeError("Downloaded WordPress core version differs")
    apps.run(*COMPOSE, "stop")
    backup = WORK / ("www-before-" + WWW_VERSION)
    if backup.exists() and not (backup / "backup-complete").is_file():
        raise RuntimeError("Incomplete www upgrade backup; inspect before retrying")
    if not backup.exists():
        backup.mkdir(mode=0o700)
        shutil.copytree(target, backup / "wordpress", symlinks=True)
        with (backup / "wordpress_mxtracks.sql").open("xb") as dump:
            apps.run("docker", "exec", DATABASE, "mysqldump",
                     "--defaults-file=/run/restore-client.cnf", "--protocol=socket",
                     "--no-tablespaces", "--single-transaction", "wordpress_mxtracks",
                     stdout=dump)
        (backup / "backup-complete").touch()
    for name in ("wp-admin", "wp-includes"):
        apps.run("rsync", "-a", "--delete", str(staging / name) + "/",
                 str(target / name) + "/")
    for source in staging.iterdir():
        if source.is_file() and source.name != "wp-config.php":
            shutil.copy2(source, target / source.name)
    if core_version(target) != WWW_VERSION:
        raise RuntimeError("Installed www core version differs")
    print("Copied www core upgraded to " + WWW_VERSION +
          "; original plugins/theme/uploads retained and pre-upgrade backup saved.", flush=True)


def verify(upgrade_www=False):
    apps.run(*COMPOSE, "up", "-d")
    apps.COMPOSE = COMPOSE
    apps.run(*COMPOSE, "exec", "-T", "apache", "apache2ctl", "configtest")
    report = {}
    for domain, subdir, schema in SITES:
        wp = [*COMPOSE, "exec", "-T", "--user", "www-data", "apache", "wp",
              "--path=/var/www/html/" + subdir + "/wordpress", "--url=https://" + domain + "/wordpress",
              "--skip-plugins", "--skip-themes"]
        if subdir == "mxdocs" and upgrade_www:
            apps.run(*wp, "core", "update-db")
        probe = f"""
global $wpdb;
if (DB_HOST !== '{DATABASE}' || DB_NAME !== '{schema}' || !DISABLE_WP_CRON) {{
    WP_CLI::error('Unexpected WordPress staging configuration');
}}
if ((int) $wpdb->get_var("SELECT COUNT(*) FROM {{$wpdb->posts}}") < 1) {{
    WP_CLI::error('Expected restored WordPress posts');
}}
"""
        apps.run(*wp, "eval", probe)
        urls = {key: apps.capture(*wp, "option", "get", key) for key in ("home", "siteurl")}
        expected = "https://" + domain + "/wordpress"
        if any(value.rstrip("/") != expected for value in urls.values()):
            raise RuntimeError("Original WordPress URLs differ for " + domain + ": inspect privately")
        apps.wait_http("apache", ["curl", "--fail", "--silent", "--max-time", "20",
                                  "-H", "Host: " + domain, "http://127.0.0.1/wordpress/wp-login.php"])
        for route in ("/wordpress/", "/wordpress/wp-login.php"):
            response = apps.capture(
                *COMPOSE, "exec", "-T", "apache", "curl", "--silent", "--show-error",
                "--max-time", "30", "-H", "Host: " + domain,
                "--write-out", "\n%{http_code}", "http://127.0.0.1" + route)
            body, status = response.rsplit("\n", 1)
            if status != "200" or "<?php" in body or not any(
                    marker in body for marker in ("wp-content/", "wp-includes/")):
                raise RuntimeError("WordPress HTTP content check failed: " + domain + route)
        report[domain] = {"database": schema, "posts": "nonempty",
                          "urls": urls, "core": apps.capture(*wp, "core", "version"),
                          "http": "homepage and login returned WordPress HTML"}
        if subdir == "mxdocs":
            active = [arg for arg in wp if arg not in ("--skip-plugins", "--skip-themes")]
            apps.run(*active, "eval", """
if (get_option('active_plugins') !== ['wp-piwik/wp-piwik.php']
    || get_stylesheet() !== 'twentyfourteen') {
    WP_CLI::error('Unexpected www active plugin or theme');
}
echo "Original Matomo plugin and Twenty Fourteen loaded successfully\\n";
""")
            report[domain]["compatibility"] = "Active WP-Piwik and Twenty Fourteen loaded; HTTP checked"
        print("Original WordPress database and HTTP checked: " + domain, flush=True)
    apps.write_json(WORK / "wordpress-report.json", {"sites": report, "production_ready": False})
    print("Original WordPress sites checked in Docker. TLS, proxies and final sync remain pending.",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-isolated", action="store_true", required=True)
    parser.add_argument("--upgrade-www-core", action="store_true",
                        help="Back up and upgrade only the isolated www core to " + WWW_VERSION)
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
        raise RuntimeError("WordPress must remain on the internal test network")
    if shutil.disk_usage(apps.RUNTIME).free < 20 * 1024**3:
        raise RuntimeError("At least 20 GiB free space is required")
    if WORK.is_symlink():
        raise RuntimeError("WordPress test root must not be a symlink")
    WORK.mkdir(mode=0o700, exist_ok=True)
    with (WORK / "start.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        image = apps.capture("docker", "image", "inspect", "beelink-source-apache:20261116",
                             "--format", "{{.Id}}")
        prepare(image)
        try:
            if args.upgrade_www_core:
                upgrade_www_core(image)
            elif core_version(WORK / "www/mxdocs/wordpress") != WWW_VERSION:
                raise RuntimeError("Legacy www core requires the approved --upgrade-www-core step")
            repair_permissions(WORK / "www")
            verify(args.upgrade_www_core)
        except BaseException:
            print("ERROR: WordPress staging failed; stopping only its test Apache.", flush=True)
            apps.run(*COMPOSE, "stop")
            raise


if __name__ == "__main__":
    main()
