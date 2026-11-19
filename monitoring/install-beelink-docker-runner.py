#!/usr/bin/env python3
"""Install a separately registered, mount-bound Docker GitLab runner."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import time


ROOT = Path("/etc/beelink-docker-runner")
TOKEN = ROOT / "token"
CONFIG = ROOT / "config.toml"
COMPOSE = ROOT / "compose.json"
UNIT = Path("/etc/systemd/system/beelink-docker-runner.service")
CACHE = Path("/daten/beelink-runtime/gitlab-runner/cache")
IMAGE = "gitlab/gitlab-runner:v19.5.3"
NAME = "beelink-docker-runner"
LOCK = Path("/run/lock/beelink-staging-control.lock")
UUID = "c0c42461-ae4c-4edb-a993-bba94a05ed6c"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def runner_config(token):
    if not re.fullmatch(r"glrt-[A-Za-z0-9_.-]{10,256}", token):
        raise RuntimeError("Expected a GitLab runner authentication token")
    return f"""concurrent = 1
check_interval = 3

[[runners]]
  name = "beelink-docker"
  url = "https://git.mxtracks.info/"
  token = "{token}"
  executor = "docker"
  [runners.docker]
    image = "alpine:3.21"
    privileged = false
    extra_hosts = ["git.mxtracks.info:192.168.178.142"]
    volumes = ["{CACHE}:/cache"]
"""


def compose_config():
    return {
        "name": NAME,
        "services": {"runner": {
            "image": IMAGE,
            "restart": "no",
            "extra_hosts": ["git.mxtracks.info:192.168.178.142"],
            "volumes": [
                {"type": "bind", "source": str(ROOT), "target": "/etc/gitlab-runner",
                 "bind": {"create_host_path": False}},
                {"type": "bind", "source": "/var/run/docker.sock",
                 "target": "/var/run/docker.sock", "bind": {"create_host_path": False}},
            ],
        }},
    }


UNIT_TEXT = f"""[Unit]
Description=Beelink GitLab Docker runner
Requires=beelink-production.service docker.service
After=beelink-production.service docker.service daten.mount
BindsTo=beelink-production.service daten.mount
PartOf=beelink-production.service

[Service]
Type=simple
UMask=0077
ExecStartPre=/bin/sh -c 'test "$(/usr/bin/findmnt -rn -M /daten -t ext4 -o UUID)" = {UUID}'
ExecStart=/usr/bin/docker compose -f {COMPOSE} up --no-build --pull never --abort-on-container-exit
ExecStop=/usr/bin/docker compose -f {COMPOSE} stop
Restart=always
RestartSec=10
TimeoutStartSec=5min
TimeoutStopSec=2min

[Install]
WantedBy=multi-user.target
"""


def write_exact(path, contents, mode):
    if path.is_symlink() or (path.exists() and
                             (not path.is_file() or path.read_text() != contents)):
        raise RuntimeError("Existing runner configuration differs: " + str(path))
    if not path.exists():
        path.write_text(contents)
    os.chown(path, 0, 0)
    path.chmod(mode)


def install():
    if ROOT.is_symlink() or not ROOT.is_dir() or ROOT.stat().st_uid != 0 \
            or stat.S_IMODE(ROOT.stat().st_mode) & 0o077:
        raise RuntimeError("Expected private root-owned runner configuration directory")
    if TOKEN.is_symlink() or not TOKEN.is_file() or TOKEN.stat().st_uid != 0 \
            or stat.S_IMODE(TOKEN.stat().st_mode) & 0o077:
        raise RuntimeError("Expected a private root-owned runner token file")
    config = runner_config(TOKEN.read_text().strip())
    os.listdir("/daten")
    mount = subprocess.check_output(
        ["findmnt", "-rn", "-M", "/daten", "-t", "ext4", "-o", "UUID"],
        text=True).strip()
    if mount != UUID:
        raise RuntimeError("Expected encrypted T7 is not mounted")
    run("systemctl", "is-active", "--quiet", "beelink-production.service")
    runtime = CACHE.parent.parent
    if runtime.is_symlink() or runtime.stat().st_uid != 0 or runtime.stat().st_mode & 0o077:
        raise RuntimeError("Expected private root-owned runtime")
    if CACHE.parent.is_symlink() or CACHE.is_symlink():
        raise RuntimeError("Unsafe runner cache path")
    CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
    run("docker", "pull", IMAGE)
    write_exact(CONFIG, config, 0o600)
    write_exact(COMPOSE, json.dumps(compose_config(), indent=2) + "\n", 0o600)
    write_exact(UNIT, UNIT_TEXT, 0o644)
    run("systemd-analyze", "verify", str(UNIT), env={
        **os.environ, "SYSTEMD_UNIT_PATH":
        "/etc/systemd/system:/run/systemd/generator.early:"
        "/run/systemd/generator:/run/systemd/generator.late:"})
    run("systemctl", "daemon-reload")
    run("systemctl", "start", NAME + ".service")
    for _ in range(60):
        state = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE), "ps", "--status", "running", "--quiet"],
            check=True, stdout=subprocess.PIPE, text=True).stdout.strip()
        if state:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Docker runner container did not start")
    run("docker", "compose", "-f", str(COMPOSE), "exec", "-T",
        "runner", "gitlab-runner", "verify")
    run("systemctl", "enable", NAME + ".service")
    print("Docker runner installed and registration verified; a CI job must still be tested.",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", required=True)
    parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    with LOCK.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        install()


if __name__ == "__main__":
    main()
