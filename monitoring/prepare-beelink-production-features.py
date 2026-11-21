#!/usr/bin/env python3
"""Migrate existing Beelink Compose configuration while the managed stack is stopped."""

import argparse
from contextlib import ExitStack
from datetime import datetime
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket


ROOT = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare():
    control = load("feature_control", "control-beelink-staging.py")
    apps = load("feature_apps", "start-beelink-apps.py")
    wordpress = load("feature_wordpress", "start-beelink-wordpress.py")
    artifacts = load("feature_artifacts", "start-beelink-artifacts.py")
    dns = load("feature_dns", "start-beelink-dns.py")
    control.check_mount()
    if (control.RUNTIME.is_symlink() or control.RUNTIME.stat().st_uid != 0
            or control.RUNTIME.stat().st_mode & 0o077):
        raise RuntimeError("Expected private root-owned runtime")
    state = control.capture("systemctl", "show", "beelink-production.service",
                            "-p", "ActiveState", "--value")
    if state != "inactive":
        raise RuntimeError("Stop beelink-production.service before preparing features")
    for project, _, services in control.STACKS:
        for service in services:
            name = project + "-" + service + "-1"
            if control.capture("docker", "inspect", "--format", "{{.State.Running}}", name) != "false":
                raise RuntimeError("Managed container must be stopped: " + name)

    image = control.capture("docker", "image", "inspect", artifacts.IMAGE,
                            "--format", "{{.Id}}")
    control.run("docker", "run", "--rm", "--network", "none", "--entrypoint", "python",
                image, "-c",
                "from pathlib import Path; s=Path('/app/main.py').read_text(); "
                "assert 'def artifact_path(name):' in s and "
                "'@basic_auth_required\\ndef get():' in s")
    source = control.RUNTIME / "host-configuration/srv/gitlab/config/gitlab.rb"
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("Missing source GitLab configuration")
    apps.gitlab_config(source.read_text())
    paths = [
        control.RUNTIME / "apps/compose.json",
        control.RUNTIME / "apps/gitlab/config/gitlab.rb",
        control.RUNTIME / "wordpress/compose.json",
        *(control.RUNTIME / "wordpress/www" / subdir / "wordpress/wp-config.php"
          for _, subdir, _ in wordpress.SITES),
        control.RUNTIME / "artifacts/compose.json",
        control.RUNTIME / "dns/compose.json",
    ]
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Missing or unsafe existing configuration: " + str(path))
    backups = control.RUNTIME / "production-feature-backups"
    if backups.is_symlink():
        raise RuntimeError("Unsafe feature backup directory")
    backups.mkdir(mode=0o700, exist_ok=True)
    backup = backups / datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup.mkdir(mode=0o700)
    for path in paths:
        destination = backup / path.relative_to(control.RUNTIME)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    print("Private rollback configuration saved at " + str(backup), flush=True)

    existing = json.loads(paths[0].read_text())
    desired = json.loads(json.dumps(existing))
    desired["services"]["gitlab"]["networks"] = ["isolated", "egress"]
    desired["services"]["gitlab"]["ports"] = [{
        "target": 22, "published": 40022, "host_ip": "192.168.178.142", "protocol": "tcp"}]
    desired["networks"]["egress"] = {"driver": "bridge"}
    if existing != desired:
        apps.upgrade_restricted_config(existing, desired)
    wordpress_image = json.loads((control.RUNTIME / "wordpress/compose.json").read_text())[
        "services"]["apache"]["image"]
    wordpress.prepare(wordpress_image)
    artifacts.prepare(image)
    dns_image = json.loads((control.RUNTIME / "dns/compose.json").read_text())[
        "services"]["dns"]["image"]
    dns.prepare(dns_image)
    for project, path, services in control.STACKS:
        control.validate_config(project, json.loads(path.read_text()), services)
    (backup / "prepared").write_text("Configuration migration complete; containers not started.\n")
    print("Feature configurations prepared. Install the controller before starting the stack.",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-stopped", required=True, action="store_true")
    parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    jobs = load("feature_jobs", "beelink-jobs.py")
    with ExitStack() as stack:
        for path in [Path("/run/lock/beelink-staging-control.lock"),
                     *(jobs.STATE / (name + ".lock") for name in jobs.JOBS)]:
            lock = stack.enter_context(path.open("w"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prepare()


if __name__ == "__main__":
    main()
