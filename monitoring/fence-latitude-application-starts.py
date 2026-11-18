#!/usr/bin/env python3
"""Persistently prevent retired Latitude applications from restarting."""

import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess


BACKUP_ROOT = Path("/var/backups/latitude-application-fence")
LOCK = Path("/run/lock/latitude-application-fence.lock")
SERVICES = ("apache2.service", "php8.3-fpm.service", "php8.4-fpm.service",
            "glassfish.service", "wildfly.service", "githubArtifactsPy.service")
CONTAINERS = ("apache-apache-1", "owncloud-staging-owncloud-1", "gitlab", "gitlab-runner")


def run(*args):
    subprocess.run(args, check=True)


def capture(*args):
    return subprocess.check_output(args, text=True).strip()


def unit_enabled(name):
    result = subprocess.run(["systemctl", "is-enabled", name], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode not in (0, 1) or not result.stdout.strip():
        raise RuntimeError("Cannot read Latitude unit enablement state: " + name)
    return result.stdout.strip()


def inspect_service(name):
    load = capture("systemctl", "show", name, "-p", "LoadState", "--value")
    if load != "loaded":
        raise RuntimeError("Latitude application service is not installed: " + name)
    return {
        "enabled": unit_enabled(name),
        "active": subprocess.run(["systemctl", "is-active", "--quiet", name]).returncode == 0,
    }


def inspect_container(name):
    details = json.loads(capture("docker", "inspect", name))[0]
    policy = details["HostConfig"]["RestartPolicy"]
    return {
        "running": details["State"]["Running"],
        "restart": policy["Name"],
        "maximum_retry_count": policy.get("MaximumRetryCount", 0),
    }


def write_report(path, report):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def fence():
    if BACKUP_ROOT.is_symlink():
        raise RuntimeError("Latitude fence backup root must not be a symlink")
    BACKUP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if BACKUP_ROOT.stat().st_uid != 0 or BACKUP_ROOT.stat().st_mode & 0o077:
        raise RuntimeError("Latitude fence backup root must be root-private")
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup = BACKUP_ROOT / stamp
    backup.mkdir(mode=0o700)
    report_path = backup / "fence.json"
    report = {
        "status": "pending",
        "services": {name: inspect_service(name) for name in SERVICES},
        "containers": {name: inspect_container(name) for name in CONTAINERS},
    }
    write_report(report_path, report)
    # Disable Docker's automatic restart before stopping each migrated app.
    for name in CONTAINERS:
        run("docker", "update", "--restart=no", name)
        if report["containers"][name]["running"]:
            run("docker", "stop", "--time", "60", name)
    # Persistent masks also block indirect systemd starts and survive reboot.
    run("systemctl", "mask", "--now", *SERVICES)
    run("systemctl", "daemon-reload")
    for name in SERVICES:
        if unit_enabled(name) != "masked":
            raise RuntimeError("Latitude service is not persistently masked: " + name)
        if subprocess.run(["systemctl", "is-active", "--quiet", name]).returncode == 0:
            raise RuntimeError("Latitude application service remains active: " + name)
    for name in CONTAINERS:
        details = json.loads(capture("docker", "inspect", name))[0]
        if (details["State"]["Running"]
                or details["HostConfig"]["RestartPolicy"]["Name"] != "no"):
            raise RuntimeError("Latitude container remains restartable or running: " + name)
    report["status"] = "complete"
    write_report(report_path, report)
    print("Latitude application starts fenced; databases, DNS and unrelated services unchanged. "
          "Private rollback inventory: " + str(backup), flush=True)


def rollback(backup):
    root = BACKUP_ROOT.resolve()
    if (backup.is_symlink() or not backup.is_dir() or not backup.resolve().is_relative_to(root)
            or backup.stat().st_uid != 0 or backup.stat().st_mode & 0o077):
        raise RuntimeError("Expected a root-private Latitude application fence inventory")
    path = backup / "fence.json"
    if (path.is_symlink() or not path.is_file() or path.stat().st_uid != 0
            or path.stat().st_mode & 0o077):
        raise RuntimeError("Latitude fence rollback inventory is missing or unsafe")
    report = json.loads(path.read_text())
    if report.get("status") not in ("pending", "complete") \
            or set(report.get("services", {})) != set(SERVICES) \
            or set(report.get("containers", {})) != set(CONTAINERS):
        raise RuntimeError("Latitude fence rollback inventory has unexpected targets")
    for name in CONTAINERS:
        previous = report["containers"][name]
        if previous["restart"] == "on-failure" and previous["maximum_retry_count"]:
            policy = "on-failure:" + str(previous["maximum_retry_count"])
        else:
            policy = previous["restart"]
        run("docker", "update", "--restart=" + policy, name)
        if previous["running"]:
            run("docker", "start", name)
    for name in SERVICES:
        if report["services"][name]["enabled"] != "masked":
            run("systemctl", "unmask", name)
    run("systemctl", "daemon-reload")
    for name in SERVICES:
        previous = report["services"][name]
        if previous["enabled"] == "masked":
            continue
        if previous["enabled"] == "enabled":
            run("systemctl", "enable", name)
        elif previous["enabled"] == "disabled":
            run("systemctl", "disable", name)
        elif previous["enabled"] == "enabled-runtime":
            run("systemctl", "enable", "--runtime", name)
        elif previous["enabled"] == "disabled-runtime":
            run("systemctl", "disable", "--runtime", name)
        elif previous["enabled"] == "masked-runtime":
            run("systemctl", "mask", "--runtime", name)
        if previous["active"] and previous["enabled"] != "masked-runtime":
            run("systemctl", "start", name)
    report["status"] = "rolled-back"
    write_report(path, report)
    print("Latitude application start state restored from " + str(backup), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--fence", action="store_true")
    group.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "latitude":
        raise RuntimeError("Run as root directly on Latitude")
    with LOCK.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.fence:
            fence()
        else:
            rollback(args.rollback)


if __name__ == "__main__":
    main()
