#!/usr/bin/env python3
"""Reconcile only isolated beelink GitLab from an existing, verified backup."""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tarfile
import time


SPEC = importlib.util.spec_from_file_location(
    "beelink_control", Path(__file__).with_name("control-beelink-staging.py"))
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)
WORK = control.RUNTIME / "apps"
CONTAINER = "beelink-app-test-gitlab-1"
PENDING = WORK / "gitlab-reconciliation-pending.json"
REPORT = WORK / "gitlab-reconciliation-report.json"
PROTECTED = ("homeassistant", "camera-postgres-db-1")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def capture(*args):
    return subprocess.check_output(args, text=True).strip()


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def verify_configuration(archive, current_secrets):
    with tarfile.open(archive, "r:gz") as source:
        values = {}
        for name in ("gitlab.rb", "gitlab-secrets.json"):
            member = source.getmember(name)
            if not member.isfile() or not 0 < member.size <= 2 * 1024 * 1024:
                raise RuntimeError("Unexpected GitLab configuration archive member: " + name)
            values[name] = source.extractfile(member).read()
    if json.loads(values["gitlab-secrets.json"]) != json.loads(current_secrets.read_bytes()):
        raise RuntimeError("Source GitLab encryption secrets differ; refusing restore")


def verify_backup(path, expected):
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise RuntimeError("Expected an explicit SHA256 checksum")
    if digest(path) != expected:
        raise RuntimeError("GitLab backup checksum mismatch")
    with tarfile.open(path) as source:
        member = source.getmember("backup_information.yml")
        if not member.isfile() or member.size > 65536:
            raise RuntimeError("Unexpected GitLab backup metadata")
        metadata = source.extractfile(member).read().decode()
    if not re.search(r"^:gitlab_version: ['\"]?19\.5\.3['\"]?\s*$", metadata, re.MULTILINE):
        raise RuntimeError("GitLab backup version must be 19.5.3")


def stop_writers():
    # gitlab-ctl accepts one service, not a list of service names.
    for service in ("nginx", "puma", "sshd"):
        run("docker", "exec", CONTAINER, "gitlab-ctl", "stop", service)


def reconcile(archive, expected):
    if not re.fullmatch(r"beelink_cutover_[A-Za-z0-9_]+_gitlab_backup\.tar", archive.name):
        raise RuntimeError("Expected an existing cutover backup, not a seed")
    if archive.is_symlink() or not archive.is_file():
        raise RuntimeError("Expected a regular existing backup")
    if PENDING.exists():
        raise RuntimeError("Previous GitLab reconciliation incomplete; inspect before retrying")
    if REPORT.exists():
        previous = json.loads(REPORT.read_text())
        if previous["sha256"] == expected and previous["backup"] == archive.name:
            print("This GitLab backup was already restored; no changes made.", flush=True)
            return
        raise RuntimeError("A different final GitLab backup was already restored")
    verify_configuration(archive.parent / "gitlab-configuration.tar.gz",
                         WORK / "gitlab/config/gitlab-secrets.json")
    if capture("docker", "exec", CONTAINER, "cat",
               "/opt/gitlab/embedded/service/gitlab-rails/VERSION") != "19.5.3":
        raise RuntimeError("Destination GitLab must be version 19.5.3")
    configuration = (WORK / "gitlab/config/gitlab.rb").read_text()
    for setting in ("sidekiq['enable'] = false", "gitlab_rails['smtp_enable'] = false",
                    "gitlab_rails['gitlab_email_enabled'] = false",
                    "letsencrypt['enable'] = false"):
        if setting not in configuration:
            raise RuntimeError("Required staging restriction missing: " + setting)
    destination = WORK / "gitlab/data/backups" / archive.name
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("Destination archive already exists; inspect before retrying")
    protected = {name: control.container_identity(name) for name in PROTECTED}
    if not all(item["running"] for item in protected.values()):
        raise RuntimeError("Protected services must be running")
    os.link(archive, destination)
    # Verify the linked inode, not a path still replaceable by the transfer user.
    verify_backup(destination, expected)
    backup_id = archive.name.removesuffix("_gitlab_backup.tar")
    record = {"backup": archive.name, "sha256": expected,
              "started": datetime.now(timezone.utc).isoformat()}
    PENDING.write_text(json.dumps(record, indent=2) + "\n")
    succeeded = False
    try:
        stop_writers()
        remote = "/var/opt/gitlab/backups/" + archive.name
        run("docker", "exec", CONTAINER, "chown", "git:git", remote)
        run("docker", "exec", CONTAINER, "chmod", "0600", remote)
        run("docker", "exec", CONTAINER, "gitlab-backup", "restore",
            "BACKUP=" + backup_id, "force=yes")
        run("docker", "exec", CONTAINER, "gitlab-ctl", "reconfigure")
        for service in ("puma", "nginx"):
            run("docker", "exec", CONTAINER, "gitlab-ctl", "start", service)
        for _ in range(180):
            probe = subprocess.run(
                ["docker", "exec", CONTAINER, "curl", "--fail", "--silent",
                 "--max-time", "10", "http://127.0.0.1/-/readiness"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if probe.returncode == 0:
                break
            time.sleep(5)
        else:
            raise RuntimeError("Restored GitLab readiness timed out")
        run("docker", "exec", CONTAINER, "gitlab-rake", "gitlab:doctor:secrets")
        if (WORK / "gitlab/config/gitlab.rb").read_text() != configuration:
            raise RuntimeError("Staging GitLab configuration changed")
        control.preflight()
        for name, previous in protected.items():
            if control.container_identity(name) != previous:
                raise RuntimeError("Protected service changed: " + name)
        record.update({"completed": datetime.now(timezone.utc).isoformat(),
                       "protected_unchanged": protected, "isolated": True})
        REPORT.write_text(json.dumps(record, indent=2) + "\n")
        PENDING.unlink()
        succeeded = True
        print("Existing GitLab backup restored and checked; still isolated, "
              "no source, router, certificate renewal or schedule changes.", flush=True)
    finally:
        if not succeeded:
            print("ERROR: Reconciliation incomplete; stopping GitLab access. "
                  "Inspect the journal and pending marker before retrying.", flush=True)
            stop_writers()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    with Path("/run/lock/beelink-staging-control.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        control.preflight()
        if capture("systemctl", "is-active", "beelink-staging.service") != "active":
            raise RuntimeError("Managed isolated staging must be active")
        if not args.backup.resolve().is_relative_to("/daten/latitude-migration"):
            raise RuntimeError("Backup must be in the private migration transfer directory")
        reconcile(args.backup, args.sha256)


if __name__ == "__main__":
    main()
