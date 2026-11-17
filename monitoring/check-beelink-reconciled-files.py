#!/usr/bin/env python3
"""Repair copied file permissions and verify isolated apps after final file sync."""

import argparse
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import socket


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


control = load("files_control", "control-beelink-staging.py")
wordpress = load("files_wordpress", "start-beelink-wordpress.py")
matomo = load("files_matomo", "start-beelink-matomo.py")
apps = load("files_apps", "start-beelink-apps.py")
artifacts = load("files_artifacts", "start-beelink-artifacts.py")
TRANSFER = Path("/daten/latitude-migration/cutover-20261117")
REPORT = control.RUNTIME / "files-reconciliation-report.json"
SYNC_WORKERS = ("beelink-file-reconcile", "beelink-file-reconcile-lock")
STOPPED_APPS = (
    "beelink-frontend-test-frontend-1", "beelink-app-test-owncloud-app-1",
    "beelink-wordpress-test-apache-1", "beelink-matomo-test-matomo-1",
    "beelink-java-web-glassfish-1", "beelink-java-web-wildfly-1",
    "beelink-artifacts-test-artifacts-1",
)


def verify():
    if REPORT.exists():
        raise RuntimeError("Files were already verified; inspect before repeating")
    if (TRANSFER / "files-sync-pending.json").exists():
        raise RuntimeError("Source file transfer incomplete")
    record = json.loads((TRANSFER / "files-sync-complete.json").read_text())
    for name in SYNC_WORKERS:
        if control.capture("docker", "ps", "--quiet", "--filter", "name=^/" + name + "$"):
            raise RuntimeError("File sync is still active; wait for its successful exit: " + name)
    if control.capture("systemctl", "is-active", "beelink-staging.service") != "active":
        raise RuntimeError("Managed isolated staging must remain active")
    for name in STOPPED_APPS:
        if control.container_identity(name)["running"]:
            raise RuntimeError("Expected app stopped after file sync: " + name)
    if not (control.RUNTIME / "database-reconciliation/report.json").is_file():
        raise RuntimeError("Final application databases must be verified first")
    protected = record["protected"]
    expected = {"homeassistant", "camera-postgres-db-1", "beelink-app-test-gitlab-1"}
    if set(protected) != expected:
        raise RuntimeError("Unexpected protected-container inventory")
    for name, previous in protected.items():
        if control.container_identity(name) != previous:
            raise RuntimeError("Protected container changed during file sync: " + name)
    succeeded = False
    try:
        for _, subdir, _ in wordpress.SITES:
            wordpress.relocate_links(wordpress.WORK / "www" / subdir,
                                     Path("/srv/www/vhosts") / subdir)
        wordpress.repair_permissions(wordpress.WORK / "www")
        matomo.repair_permissions(matomo.WORK / "www")
        if wordpress.core_version(wordpress.WORK / "www/mxdocs/wordpress") != wordpress.WWW_VERSION:
            raise RuntimeError("Upgraded WordPress core was overwritten")
        wordpress.verify()
        matomo.verify()
        apps.start_owncloud()
        control.run("docker", "start", "beelink-java-web-glassfish-1",
                    "beelink-java-web-wildfly-1")
        control.wait_applications()
        artifacts.verify()
        control.preflight()
        for name, previous in protected.items():
            if control.container_identity(name) != previous:
                raise RuntimeError("Protected container changed: " + name)
        record.update({"completed": datetime.now(timezone.utc).isoformat(),
                       "isolated": True, "camera_reconciliation_pending": True,
                       "comparison": "rsync size/mtime/metadata, not a full content hash"})
        REPORT.write_text(json.dumps(record, indent=2) + "\n")
        succeeded = True
        print("Reconciled files and isolated app/TLS checks passed. "
              "Public activation and camera reconciliation still pending.", flush=True)
    finally:
        if not succeeded:
            print("ERROR: File checks incomplete; stopping the isolated frontend.", flush=True)
            control.run("docker", "stop", "beelink-frontend-test-frontend-1")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-isolated", action="store_true", required=True)
    parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    with Path("/run/lock/beelink-staging-control.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        control.preflight()
        verify()


if __name__ == "__main__":
    main()
