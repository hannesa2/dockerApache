#!/usr/bin/env python3
"""Prepare Beelink ownCloud and GitLab with the approved production features."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tarfile
import time


RUNTIME = Path("/daten/beelink-runtime")
SEED = Path("/daten/latitude-migration")
APPS = RUNTIME / "apps"
ROOTFS = RUNTIME / "rootfs"
GITLAB_IMAGE = "sha256:99dd108e798f1f2b04b70949104b29bedcdd79b252145c64d61d11aa5e58d552"
OWNCLOUD_IMAGE = "owncloud/server:11.1.0"
COMPOSE = ["docker", "compose", "-p", "beelink-apps", "-f", str(APPS / "compose.json")]

OWNCLOUD_CONFIG = r"""<?php
$CONFIG = [];
require '/source/config.php';
if (empty($CONFIG['installed']) || ($CONFIG['dbtype'] ?? '') !== 'pgsql'
    || ($CONFIG['dbname'] ?? '') !== 'owncloud_stage') {
    fwrite(STDERR, "Unexpected source ownCloud installation.\n");
    exit(1);
}
$CONFIG['dbhost'] = 'beelink-databases-owncloud-1';
$CONFIG['datadirectory'] = '/mnt/data/files';
$CONFIG['trusted_domains'] = ['localhost', '127.0.0.1', 'beelink'];
$CONFIG['overwrite.cli.url'] = 'http://localhost';
$CONFIG['overwritehost'] = 'localhost';
$CONFIG['overwriteprotocol'] = 'http';
$CONFIG['overwritewebroot'] = '';
$CONFIG['htaccess.RewriteBase'] = '/';
$CONFIG['logfile'] = '/mnt/data/owncloud.log';
$CONFIG['redis'] = ['host' => 'redis'];
$CONFIG['maintenance'] = false;
unset($CONFIG['trusted_proxies']);
$target = '/target/config.php';
if (file_exists($target)) {
    fwrite(STDERR, "Refusing to overwrite an existing test configuration.\n");
    exit(1);
}
if (file_put_contents($target, "<?php\n\$CONFIG = " . var_export($CONFIG, true) . ";\n") === false
    || !chmod($target, 0640) || !chown($target, 33) || !chgrp($target, 0)) {
    fwrite(STDERR, "Could not write private ownCloud test configuration.\n");
    exit(1);
}
"""

RESTRICTED_GITLAB_CONFIG = """external_url 'https://git.mxtracks.info'
letsencrypt['enable'] = false
nginx['listen_port'] = 80
nginx['listen_https'] = false
gitlab_rails['gitlab_shell_ssh_port'] = 40022
gitlab_rails['smtp_enable'] = false
gitlab_rails['gitlab_email_enabled'] = false
gitlab_rails['incoming_email_enabled'] = false
gitlab_rails['usage_ping_enabled'] = false
gitlab_rails['backup_path'] = '/var/opt/gitlab/backups'
sidekiq['enable'] = false
prometheus_monitoring['enable'] = false
"""
GITLAB_CONFIG = """external_url 'https://git.mxtracks.info'
letsencrypt['enable'] = false
nginx['listen_port'] = 80
nginx['listen_https'] = false
gitlab_rails['gitlab_shell_ssh_port'] = 40022
gitlab_rails['smtp_enable'] = true
gitlab_rails['gitlab_email_enabled'] = true
gitlab_rails['incoming_email_enabled'] = false
gitlab_rails['usage_ping_enabled'] = false
gitlab_rails['backup_path'] = '/var/opt/gitlab/backups'
sidekiq['enable'] = true
prometheus_monitoring['enable'] = false
"""


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def capture(*args):
    return subprocess.check_output(args, text=True).strip()


def dc(*args, **kwargs):
    return run(*COMPOSE, *args, **kwargs)


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def mount(source, target, readonly=False):
    return {"type": "bind", "source": str(source), "target": target,
            "read_only": readonly, "bind": {"create_host_path": False}}


def compose_config(owncloud_environment):
    environment = dict(owncloud_environment)
    environment.update({
        "OWNCLOUD_DOMAIN": "localhost",
        "OWNCLOUD_TRUSTED_DOMAINS": "localhost,127.0.0.1,beelink",
        "OWNCLOUD_DB_HOST": "beelink-databases-owncloud-1",
        "OWNCLOUD_REDIS_ENABLED": "true", "OWNCLOUD_REDIS_HOST": "redis",
        "OWNCLOUD_CROND_ENABLED": "false", "OWNCLOUD_BACKGROUND_MODE": "webcron",
        "OWNCLOUD_LOG_FILE": "/mnt/data/owncloud.log",
        "OWNCLOUD_SKIP_CHOWN": "true", "OWNCLOUD_SKIP_CHMOD": "true",
        "OWNCLOUD_APPS_INSTALL": "", "OWNCLOUD_APPS_ENABLE": "",
        "OWNCLOUD_APPS_DISABLE": "", "OWNCLOUD_APPS_UNINSTALL": "",
    })
    return {
        "name": "beelink-apps",
        "services": {
            "redis": {
                "image": "redis:7-alpine", "restart": "no", "networks": ["isolated"],
                "volumes": [mount(APPS / "redis", "/data")],
            },
            "owncloud-app": {
                "image": OWNCLOUD_IMAGE, "restart": "no", "networks": ["isolated"],
                "environment": environment,
                "volumes": [
                    mount(APPS / "owncloud/volume", "/mnt/data"),
                    mount(ROOTFS / "daten/owncloud/data", "/mnt/data/files"),
                ],
            },
            "gitlab": {
                "image": GITLAB_IMAGE, "hostname": "git.mxtracks.info",
                "restart": "no", "networks": ["isolated", "egress"],
                "ports": [{"target": 22, "published": 40022,
                           "host_ip": "192.168.178.142", "protocol": "tcp"}],
                "shm_size": "256m",
                "volumes": [
                    mount(APPS / "gitlab/config", "/etc/gitlab"),
                    mount(APPS / "gitlab/logs", "/var/log/gitlab"),
                    mount(APPS / "gitlab/data", "/var/opt/gitlab"),
                ],
            },
        },
        "networks": {
            "isolated": {"external": True, "name": "beelink-databases_isolated"},
            "egress": {"driver": "bridge"},
        },
    }


def gitlab_config(source):
    source = source.replace("\r\n", "\n").rstrip()
    settings = {}
    for key in ("smtp_address", "smtp_port"):
        matches = re.findall(
            rf"""(?m)^[ \t]*gitlab_rails\[['"]{key}['"]\][ \t]*=[ \t]*"""
            r"""(?:"([^"\n]*)"|'([^'\n]*)'|([0-9]+))[ \t]*(?:#.*)?$""",
            source)
        if len(matches) != 1:
            raise RuntimeError("Latitude GitLab SMTP relay settings are incomplete or ambiguous")
        value = next((value for value in matches[0] if value != ""), None)
        if value is None:
            raise RuntimeError("Latitude GitLab SMTP relay settings are incomplete")
        settings[key] = value
    if not settings["smtp_address"].strip() or any(
            char.isspace() for char in settings["smtp_address"]):
        raise RuntimeError("Latitude GitLab SMTP relay address is invalid")
    try:
        port = int(settings["smtp_port"])
    except ValueError as error:
        raise RuntimeError("Latitude GitLab SMTP relay port is invalid") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("Latitude GitLab SMTP relay port is invalid")
    config = source + "\n\n" + GITLAB_CONFIG
    if port == 465:
        config += ("gitlab_rails['smtp_tls'] = true\n"
                   "gitlab_rails['smtp_enable_starttls_auto'] = false\n")
    return config


def wait_http(service, command, attempts=180):
    for _ in range(attempts):
        ids = capture(*COMPOSE, "ps", "--all", "--quiet", service).splitlines()
        if len(ids) != 1:
            raise RuntimeError("Expected one application container: " + service)
        state = json.loads(capture("docker", "inspect", "--format", "{{json .State}}", ids[0]))
        if not state["Running"]:
            raise RuntimeError(
                f"Application container stopped: {service} (exit {state['ExitCode']}); "
                f"inspect docker logs {ids[0]}")
        result = subprocess.run([*COMPOSE, "exec", "-T", service, *command],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return
        time.sleep(5)
    raise RuntimeError("HTTP readiness timed out: " + service)


def upgrade_restricted_config(existing, desired):
    previous = json.loads(json.dumps(desired))
    gitlab = previous["services"]["gitlab"]
    gitlab.pop("ports")
    gitlab["networks"] = ["isolated"]
    previous["networks"].pop("egress")
    for volume in previous["services"]["owncloud-app"]["volumes"]:
        if volume["target"] == "/mnt/data/files":
            volume["read_only"] = True
    legacy = json.loads(json.dumps(previous))
    legacy["services"]["owncloud-app"]["volumes"][1]["read_only"] = False
    if existing not in (previous, legacy):
        raise RuntimeError("Existing application configuration differs; inspect before proceeding")
    ids = capture(*COMPOSE, "ps", "--all", "--quiet").splitlines()
    for container_id in ids:
        if capture("docker", "inspect", "--format", "{{.State.Running}}", container_id) != "false":
            raise RuntimeError("Stop every beelink application before enabling production features")
    config_path = APPS / "gitlab/config/gitlab.rb"
    if config_path.is_symlink() or not config_path.is_file() \
            or config_path.read_text() != RESTRICTED_GITLAB_CONFIG:
        raise RuntimeError("Existing GitLab configuration differs; inspect before proceeding")
    backup = config_path.with_name("gitlab.rb.before-production-features")
    if backup.exists() or backup.is_symlink():
        raise RuntimeError("GitLab configuration backup already exists; inspect before proceeding")
    source = RUNTIME / "host-configuration/srv/gitlab/config/gitlab.rb"
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("Missing original Latitude GitLab configuration")
    new_gitlab_config = gitlab_config(source.read_text())
    shutil.copyfile(config_path, backup)
    backup.chmod(0o600)
    temporary = config_path.with_suffix(".rb.tmp")
    temporary.write_text(new_gitlab_config)
    temporary.chmod(0o600)
    temporary.replace(config_path)
    write_json(APPS / "compose.json", desired)
    print("GitLab workers, SMTP and SSH configuration prepared; no services were started.",
          flush=True)


def prepare():
    inventory = json.loads((SEED / "metadata/docker-inspect.json").read_text())
    source = next(c for c in inventory if c["Name"] == "/owncloud-staging-owncloud-1")
    source_environment = dict(value.split("=", 1) for value in source["Config"]["Env"])
    environment = {key: value for key, value in source_environment.items()
                   if key.startswith("OWNCLOUD_")}
    if source["Config"]["Image"] != OWNCLOUD_IMAGE:
        raise RuntimeError("Unexpected ownCloud source image")
    for image in (OWNCLOUD_IMAGE, "redis:7-alpine", GITLAB_IMAGE):
        run("docker", "image", "inspect", image, stdout=subprocess.DEVNULL)
    config = compose_config(environment)
    config_path = APPS / "compose.json"
    if config_path.exists():
        if not (APPS / "prepared").is_file():
            raise RuntimeError("Previous preparation incomplete; inspect before retrying")
        existing = json.loads(config_path.read_text())
        if existing != config:
            upgrade_restricted_config(existing, config)
        return
    source_gitlab_config = RUNTIME / "host-configuration/srv/gitlab/config/gitlab.rb"
    if source_gitlab_config.is_symlink() or not source_gitlab_config.is_file():
        raise RuntimeError("Missing original Latitude GitLab configuration")
    prepared_gitlab_config = gitlab_config(source_gitlab_config.read_text())
    if any((APPS / name).exists() for name in ("owncloud", "gitlab", "redis")):
        raise RuntimeError("Unmanaged application directories exist; inspect before retrying")
    for path in ("owncloud/volume/config", "owncloud/volume/sessions",
                 "gitlab/config", "gitlab/logs", "gitlab/data/backups", "redis"):
        (APPS / path).mkdir(parents=True, mode=0o750)
    volume = APPS / "owncloud/volume"
    shutil.copytree(ROOTFS / "daten/owncloud/apps", volume / "apps", symlinks=True)
    for root, directories, files in os.walk(volume):
        os.chown(root, 33, 0)
        for name in directories + files:
            os.chown(os.path.join(root, name), 33, 0, follow_symlinks=False)
    run("docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", "php",
        "--mount", f"type=bind,source={ROOTFS / 'daten/owncloud/config'},target=/source,readonly",
        "--mount", f"type=bind,source={volume / 'config'},target=/target",
        OWNCLOUD_IMAGE, input=OWNCLOUD_CONFIG, text=True)
    secrets = RUNTIME / "host-configuration/srv/gitlab/config/gitlab-secrets.json"
    if secrets.is_symlink() or not secrets.is_file() or not json.loads(secrets.read_text()):
        raise RuntimeError("GitLab encryption secrets missing")
    shutil.copyfile(secrets, APPS / "gitlab/config/gitlab-secrets.json")
    os.chmod(APPS / "gitlab/config/gitlab-secrets.json", 0o600)
    gitlab_rb = APPS / "gitlab/config/gitlab.rb"
    gitlab_rb.write_text(prepared_gitlab_config)
    gitlab_rb.chmod(0o600)
    write_json(config_path, config)
    (APPS / "prepared").touch()


def start_owncloud():
    dc("up", "-d", "redis", "owncloud-app")
    wait_http("owncloud-app", ["curl", "--fail", "--silent", "--max-time", "5",
                              "http://127.0.0.1:8080/status.php"])
    status = json.loads(capture(*COMPOSE, "exec", "-T", "owncloud-app",
                               "curl", "--fail", "--silent", "http://127.0.0.1:8080/status.php"))
    if (not status.get("installed") or status.get("maintenance")
            or status.get("needsDbUpgrade") or status.get("versionstring") != "11.1.0"):
        raise RuntimeError("Unexpected ownCloud application status")
    # A status page alone does not demonstrate a working database connection.
    dc("exec", "-T", "--user", "www-data", "owncloud-app", "occ", "user:list",
       stdout=subprocess.DEVNULL)
    write_json(APPS / "owncloud-report.json", status)
    print("ownCloud application and database connection checked; "
          "writes are confined to the isolated beelink copy.", flush=True)


def start_gitlab():
    completed = APPS / "gitlab-restored"
    started = APPS / "gitlab-restore-started"
    if started.exists() and not completed.exists():
        raise RuntimeError("Previous GitLab restore incomplete; inspect before retrying")
    expected = (SEED / "metadata/gitlab-backup.sha256").read_text().strip()
    archives = sorted((SEED / "gitlab").glob("*_gitlab_backup.tar"), reverse=True)
    if not archives:
        raise RuntimeError("GitLab backup is missing")
    archive = archives[0]
    backup_id = archive.name.removesuffix("_gitlab_backup.tar")
    if not re.fullmatch(r"beelink_seed_\d{8}T\d{6}Z", backup_id):
        raise RuntimeError("Unexpected GitLab backup identifier")
    if not completed.exists():
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise RuntimeError("GitLab backup checksum mismatch")
        with tarfile.open(archive) as source:
            info = source.extractfile("backup_information.yml")
            if info is None or not re.search(
                    r"^:gitlab_version: ['\"]?19\.5\.3['\"]?\s*$",
                    info.read().decode(), re.MULTILINE):
                raise RuntimeError("GitLab backup version must be 19.5.3")
        destination = APPS / "gitlab/data/backups" / archive.name
        if not destination.exists():
            # A hardlink avoids duplicating the 14 GiB archive. Ownership is
            # restored on the source inode too; the staging parent stays private.
            os.link(archive, destination)
    dc("up", "-d", "gitlab")
    wait_http("gitlab", ["curl", "--fail", "--silent", "--max-time", "10",
                        "-H", "Host: git.mxtracks.info", "http://127.0.0.1/-/readiness"],
              attempts=360)
    version = capture(*COMPOSE, "exec", "-T", "gitlab", "cat",
                      "/opt/gitlab/embedded/service/gitlab-rails/VERSION")
    if version != "19.5.3":
        raise RuntimeError("GitLab image must be version 19.5.3")
    if not completed.exists():
        dc("exec", "-T", "gitlab", "chown", "git:git",
           "/var/opt/gitlab/backups/" + archive.name)
        dc("exec", "-T", "gitlab", "chmod", "0600",
           "/var/opt/gitlab/backups/" + archive.name)
        dc("exec", "-T", "gitlab", "gitlab-ctl", "stop", "puma")
        dc("exec", "-T", "gitlab", "gitlab-ctl", "stop", "nginx")
        started.write_text(backup_id + "\n")
        dc("exec", "-T", "gitlab", "gitlab-backup", "restore",
           "BACKUP=" + backup_id, "force=yes")
        dc("exec", "-T", "gitlab", "gitlab-ctl", "reconfigure")
        dc("exec", "-T", "gitlab", "gitlab-ctl", "start", "puma")
        dc("exec", "-T", "gitlab", "gitlab-ctl", "start", "nginx")
        wait_http("gitlab", ["curl", "--fail", "--silent", "--max-time", "10",
                            "-H", "Host: git.mxtracks.info", "http://127.0.0.1/-/readiness"])
        dc("exec", "-T", "gitlab", "gitlab-rake", "gitlab:doctor:secrets",
           stdout=subprocess.DEVNULL)
        completed.write_text(backup_id + "\n")
    sidekiq = capture(*COMPOSE, "exec", "-T", "gitlab", "gitlab-ctl", "status", "sidekiq")
    if not sidekiq.startswith("run: sidekiq:"):
        raise RuntimeError("GitLab Sidekiq is not running")
    print("GitLab restore and readiness checked in isolation; Sidekiq and SMTP are enabled, "
          "and the host shell runner is prepared separately.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-isolated", action="store_true", required=True)
    parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    os.listdir("/daten")
    if capture("findmnt", "-rn", "-M", "/daten", "-t", "ext4", "-o", "UUID") != \
            "c0c42461-ae4c-4edb-a993-bba94a05ed6c":
        raise RuntimeError("Expected encrypted T7 is not mounted")
    if (RUNTIME.is_symlink() or RUNTIME.stat().st_uid != 0
            or RUNTIME.stat().st_mode & 0o077
            or (RUNTIME / ".beelink-runtime").read_text().strip() != "beelink-runtime-v1"
            or not (RUNTIME / "files-prepared").is_file()):
        raise RuntimeError("Prepare the private runtime files first")
    if not Path("/daten/beelink-staging/database-report.json").is_file():
        raise RuntimeError("Verify all isolated database restores first")
    if shutil.disk_usage(RUNTIME).free < 100 * 1024**3:
        raise RuntimeError("At least 100 GiB of free space is required for application staging")
    network = json.loads(capture("docker", "network", "inspect", "beelink-databases_isolated"))[0]
    if not network["Internal"]:
        raise RuntimeError("Application network must be isolated from production and internet")
    if APPS.is_symlink():
        raise RuntimeError("Application root must not be a symlink")
    APPS.mkdir(mode=0o700, exist_ok=True)
    with (APPS / "start.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prepare()
        succeeded = False
        try:
            start_owncloud()
            start_gitlab()
            succeeded = True
        finally:
            if not succeeded:
                print("ERROR: Staging incomplete; stopping test applications. "
                      "Inspect the journal before retrying.", flush=True)
                dc("stop", "owncloud-app", "gitlab")
        (APPS / "applications-checked").touch()
        print("Isolated application checks complete. Production, forwarding and schedules unchanged.")


if __name__ == "__main__":
    main()
